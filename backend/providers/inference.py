from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock
from typing import Iterator, Literal

InferencePriority = Literal["interactive", "background"]
_priority: ContextVar[InferencePriority] = ContextVar("inference_priority", default="interactive")


@contextmanager
def inference_priority(priority: InferencePriority) -> Iterator[None]:
    token = _priority.set(priority)
    try:
        yield
    finally:
        _priority.reset(token)


@dataclass(eq=False)
class _Request:
    profile_id: str | None
    model: str | None
    priority: InferencePriority


@dataclass
class _ProviderQueue:
    serialize_model_switches: bool = False
    active: dict[str | None, int] = field(default_factory=dict)


class InferenceLease:
    def __init__(self, scheduler: InferenceScheduler, profile_id: str | None = None,
                 model: str | None = None, priority: InferencePriority | None = None,
                 server_url: str | None = None) -> None:
        self._scheduler = scheduler
        self._profile_id = profile_id
        self._model = model
        self._priority = priority
        self._request: _Request | None = None
        self._released = False

    async def __aenter__(self) -> InferenceLease:
        self._request = await self._scheduler._acquire_request(
            self._profile_id, self._model, self._priority,
        )
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.release()

    async def release(self) -> None:
        if self._request is None or self._released:
            return
        # No await between changing lease ownership and returning capacity. Cleanup
        # remains atomic even when the caller is closing a cancelled response stream.
        self._scheduler._release(self._request)
        self._released = True


class InferenceScheduler:
    """One application-wide inference lane, held only for a model response."""

    def __init__(self) -> None:
        self._providers: dict[str | None, _ProviderQueue] = {}
        self._policies: dict[str, bool] = {}
        self._policy_lock = Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._waiters: list[_Request] = []
        self._active: _Request | None = None
        self._interactive_streak = 0
        self._changed = asyncio.Event()

    def _notify(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()

    def _next_request(self) -> _Request | None:
        if self._active is not None or not self._waiters:
            return None
        preferred = "background" if self._interactive_streak >= 3 else "interactive"
        return next(
            (item for item in self._waiters if item.priority == preferred),
            self._waiters[0],
        )

    def configure(self, profile_id: str, *, serialize_model_switches: bool) -> None:
        with self._policy_lock:
            self._policies[profile_id] = serialize_model_switches
            loop = self._loop
        if loop is None:
            return
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is loop:
            self._refresh_policy(profile_id)
        else:
            # Provider routes run in FastAPI's thread pool. Only the owning event
            # loop may mutate active queues or wake their asyncio waiters.
            try:
                loop.call_soon_threadsafe(self._refresh_policy, profile_id)
            except RuntimeError:
                if not loop.is_closed():
                    raise

    def _refresh_policy(self, profile_id: str) -> None:
        with self._policy_lock:
            policy = self._policies[profile_id]
        state = self._providers.get(profile_id)
        if state is not None:
            state.serialize_model_switches = policy

    def request(self, *, profile_id: str | None = None, model: str | None = None,
                priority: InferencePriority | None = None,
                server_url: str | None = None) -> InferenceLease:
        return InferenceLease(self, profile_id, model, priority, server_url)

    def snapshot(self) -> dict:
        queued = [
            {"profile_id": item.profile_id, "model": item.model, "priority": item.priority}
            for item in self._waiters
        ]
        return {
            "active_requests": sum(
                sum(state.active.values()) for state in self._providers.values()
            ),
            "queue": queued,
            "interactive_queued": sum(item["priority"] == "interactive" for item in queued),
            "background_queued": sum(item["priority"] == "background" for item in queued),
            "providers": {
                profile_id: {
                    "serialize_model_switches": state.serialize_model_switches,
                    "active_models": dict(state.active),
                }
                for profile_id, state in self._providers.items()
            },
        }

    async def _acquire_request(self, profile_id: str | None, model: str | None,
                               priority: InferencePriority | None) -> _Request:
        with self._policy_lock:
            self._loop = asyncio.get_running_loop()
            policy = self._policies.get(profile_id, False) if profile_id is not None else False
        state = self._providers.setdefault(profile_id, _ProviderQueue())
        state.serialize_model_switches = policy
        item = _Request(profile_id, model, priority or _priority.get())
        self._waiters.append(item)
        try:
            while self._next_request() is not item:
                await self._changed.wait()
            self._active = item
            state.active[model] = state.active.get(model, 0) + 1
            self._interactive_streak = (
                min(self._interactive_streak + 1, 3) if item.priority == "interactive" else 0
            )
        finally:
            self._waiters.remove(item)
            self._notify()
        return item

    def _release(self, item: _Request) -> None:
        state = self._providers[item.profile_id]
        state.active[item.model] -= 1
        if state.active[item.model] == 0:
            del state.active[item.model]
        self._active = None
        self._notify()
