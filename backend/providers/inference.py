from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import AsyncIterator, Iterator, Literal

import httpx

from backend.core.errors import ConflictError

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
    server_url: str | None
    priority: InferencePriority
    owner: object | None
    group: object | None
    limit: int


class InferenceLease:
    def __init__(self, scheduler: InferenceScheduler, profile_id: str | None = None,
                 model: str | None = None, priority: InferencePriority | None = None,
                 server_url: str | None = None) -> None:
        self._scheduler = scheduler
        self._profile_id = profile_id
        self._model = model
        self._priority = priority
        self._server_url = server_url
        self._acquired = False
        self._released = False
        self._parallel_group: object | None = None

    async def __aenter__(self) -> InferenceLease:
        self._parallel_group = await self._scheduler._acquire_request(
            self._profile_id, self._model, self._priority, self._server_url,
        )
        self._acquired = True
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.release()

    async def release(self) -> None:
        if not self._acquired or self._released:
            return
        self._released = True
        await self._scheduler._release(self._parallel_group)


class InferenceScheduler:
    """Request-boundary fairness and opt-in, manually confirmed single-GPU residency."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._active_requests = 0
        self._waiters: list[_Request] = []
        self._interactive_streak = 0
        self.profile_id: str | None = None
        self._server_url: str | None = None
        self.resident_model: str | None = None
        self.confirmed = False
        self.paused = False
        self.session_mode: Literal["interactive", "batch"] = "interactive"
        self._active_parallel_group: object | None = None
        self._exclusive_active: object | None = None
        self._exclusive_waiters: deque[object] = deque()
        self._exclusive_owner: ContextVar[object | None] = ContextVar(
            f"inference_exclusive_owner_{id(self)}",
            default=None,
        )
        self._parallel_request_group: ContextVar[tuple[object, int] | None] = ContextVar(
            f"inference_parallel_group_{id(self)}",
            default=None,
        )

    def request(self, *, profile_id: str | None = None, model: str | None = None,
                priority: InferencePriority | None = None,
                server_url: str | None = None) -> InferenceLease:
        return InferenceLease(self, profile_id, model, priority, server_url)

    def restore(self, profile_id: str, model: str | None,
                session_mode: Literal["interactive", "batch"] = "interactive",
                server_url: str | None = None) -> None:
        if self.profile_id not in {None, profile_id}:
            raise ConflictError("Only one local GPU profile can have residency protection enabled.")
        if self._active_requests or self._exclusive_active or self._exclusive_waiters:
            raise ConflictError("Wait for active local operations to finish before enabling protection.")
        self.profile_id = profile_id
        self._server_url = (
            str(httpx.URL(server_url).copy_with(query=None, fragment=None)).rstrip("/")
            if server_url is not None else None
        )
        self.resident_model = model
        self.session_mode = session_mode
        self.confirmed = False
        self.paused = True

    async def begin_switch(self, profile_id: str) -> None:
        async with self._condition:
            if self.profile_id != profile_id:
                raise ConflictError("Enable residency protection for this profile first.")
            if self._exclusive_active is not None:
                raise ConflictError("Wait for the current exclusive operation to finish.")
            # Remain paused if the caller disconnects: resuming unknown weights is unsafe.
            self.paused = True
            self.confirmed = False
            self._condition.notify_all()
            await self._condition.wait_for(lambda: self._active_requests == 0)

    async def confirm(self, profile_id: str, model: str,
                      session_mode: Literal["interactive", "batch"]) -> None:
        async with self._condition:
            if self.profile_id != profile_id or not self.paused or self._active_requests:
                raise ConflictError("Drain and pause inference before confirming a resident model.")
            self.resident_model = model
            self.session_mode = session_mode
            self.confirmed = True
            self.paused = False
            self._condition.notify_all()

    async def disable(self, profile_id: str) -> None:
        async with self._condition:
            if self.profile_id != profile_id or self._active_requests or self._waiters:
                raise ConflictError("Drain requests and cancel queued work before disabling protection.")
            self.profile_id = None
            self._server_url = None
            self.resident_model = None
            self.confirmed = False
            self.paused = False
            self._condition.notify_all()

    def snapshot(self) -> dict:
        queued = [
            {"profile_id": item.profile_id, "model": item.model, "priority": item.priority,
             "blocked_by_residency": not self._resident_matches(item)}
            for item in self._waiters
        ]
        return {
            "profile_id": self.profile_id, "enabled": self.profile_id is not None,
            "resident_model": self.resident_model, "confirmed": self.confirmed,
            "paused": self.paused, "active_requests": self._active_requests,
            "session_mode": self.session_mode, "queue": queued,
            "interactive_queued": sum(item.priority == "interactive" for item in self._waiters),
            "background_queued": sum(item.priority == "background" for item in self._waiters),
        }

    def _resident_matches(self, item: _Request) -> bool:
        return self.profile_id is None or (
            self.confirmed and not self.paused
            and item.profile_id == self.profile_id and item.model == self.resident_model
            and (self._server_url is None or item.server_url == self._server_url)
        )

    @asynccontextmanager
    async def parallel(self, group: object, limit: int) -> AsyncIterator[None]:
        if limit < 2:
            raise ValueError("Parallel inference limit must be at least two.")
        token = self._parallel_request_group.set((group, limit))
        try:
            yield
        finally:
            self._parallel_request_group.reset(token)

    @asynccontextmanager
    async def exclusive(self) -> AsyncIterator[None]:
        if self.profile_id is not None:
            # Protected batches lock model identity, not the entire inference lane.
            yield
            return
        if self._exclusive_owner.get() is not None:
            yield
            return

        waiter = object()
        async with self._condition:
            self._exclusive_waiters.append(waiter)
            try:
                await self._condition.wait_for(
                    lambda: self._active_requests == 0
                    and self._exclusive_active is None
                    and self._exclusive_waiters
                    and self._exclusive_waiters[0] is waiter
                )
            except BaseException:
                self._exclusive_waiters.remove(waiter)
                self._condition.notify_all()
                raise
            self._exclusive_waiters.popleft()
            self._exclusive_active = waiter

        token = self._exclusive_owner.set(waiter)
        try:
            yield
        finally:
            self._exclusive_owner.reset(token)
            async with self._condition:
                await self._condition.wait_for(lambda: self._active_requests == 0)
                self._exclusive_active = None
                self._condition.notify_all()

    async def _acquire_request(self, profile_id: str | None = None,
                               model: str | None = None,
                               priority: InferencePriority | None = None,
                               server_url: str | None = None) -> object | None:
        owner = self._exclusive_owner.get()
        parallel = self._parallel_request_group.get()
        group = parallel[0] if parallel is not None else None
        limit = parallel[1] if parallel is not None else 1

        item = _Request(profile_id, model, server_url, priority or _priority.get(), owner, group, limit)

        def eligible(candidate: _Request) -> bool:
            if not self._resident_matches(candidate):
                return False
            if candidate.owner is None:
                lane_available = (
                    self._exclusive_active is None
                    and not self._exclusive_waiters
                )
            else:
                lane_available = self._exclusive_active is candidate.owner
            if not lane_available:
                return False
            if candidate.group is None or self.profile_id is not None:
                return self._active_requests == 0
            return (
                self._active_requests < candidate.limit
                and self._active_parallel_group in {None, candidate.group}
            )

        def available() -> bool:
            candidates = [candidate for candidate in self._waiters if eligible(candidate)]
            if not candidates:
                return False
            preferred = "background" if self._interactive_streak >= 3 else "interactive"
            chosen = next((candidate for candidate in candidates
                           if candidate.priority == preferred), candidates[0])
            return chosen is item

        async with self._condition:
            self._waiters.append(item)
            try:
                await self._condition.wait_for(available)
            finally:
                self._waiters.remove(item)
                self._condition.notify_all()
            self._interactive_streak = (
                self._interactive_streak + 1 if item.priority == "interactive" else 0
            )
            self._active_requests += 1
            if group is not None:
                self._active_parallel_group = group
        return group

    async def _release(self, parallel_group: object | None) -> None:
        async with self._condition:
            self._active_requests -= 1
            if self._active_requests < 0:
                raise RuntimeError("Inference scheduler released an inactive request.")
            if self._active_requests == 0:
                self._active_parallel_group = None
            elif parallel_group is None:
                raise RuntimeError("A serial inference request overlapped another request.")
            self._condition.notify_all()
