from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import AsyncIterator


class InferenceLease:
    def __init__(self, scheduler: InferenceScheduler) -> None:
        self._scheduler = scheduler
        self._acquired = False
        self._released = False
        self._parallel_group: object | None = None

    async def __aenter__(self) -> InferenceLease:
        self._parallel_group = await self._scheduler._acquire_request()
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
    """Single-flight inference gate with FIFO, summary-prioritized exclusive work."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._active_requests = 0
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

    def request(self) -> InferenceLease:
        return InferenceLease(self)

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

    async def _acquire_request(self) -> object | None:
        owner = self._exclusive_owner.get()
        parallel = self._parallel_request_group.get()
        group = parallel[0] if parallel is not None else None
        limit = parallel[1] if parallel is not None else 1

        def available() -> bool:
            if owner is None:
                lane_available = (
                    self._exclusive_active is None
                    and not self._exclusive_waiters
                )
            else:
                lane_available = self._exclusive_active is owner
            if not lane_available:
                return False
            if group is None:
                return self._active_requests == 0
            return (
                self._active_requests < limit
                and self._active_parallel_group in {None, group}
            )

        async with self._condition:
            await self._condition.wait_for(available)
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
