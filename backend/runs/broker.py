"""In-process fan-out and short replay history for SDK run events."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any


class SubscriberLagged:
    """Signals that durable replay must resume from the subscriber's last cursor."""


SUBSCRIBER_LAGGED = SubscriberLagged()


class EventBroker:
    def __init__(self, *, subscriber_queue_size: int = 256) -> None:
        if subscriber_queue_size < 1:
            raise ValueError("Subscriber queue size must be positive.")
        self._subscriber_queue_size = subscriber_queue_size
        self._queues: dict[
            str, set[asyncio.Queue[dict[str, Any] | SubscriberLagged]]
        ] = defaultdict(set)
        self._history: dict[str, deque[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    async def publish(self, run_id: str, event: dict[str, Any]) -> None:
        async with self._lock:
            history = self._history.setdefault(run_id, deque(maxlen=512))
            history.append(event)
            queues = list(self._queues.get(run_id, set()))
            if event.get("event_type") in {
                "run.completed",
                "run.failed",
                "run.cancelled",
            }:
                self._history.pop(run_id, None)
            for queue in queues:
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    self._queues[run_id].discard(queue)
                    while not queue.empty():
                        queue.get_nowait()
                    queue.put_nowait(SUBSCRIBER_LAGGED)
            if run_id in self._queues and not self._queues[run_id]:
                self._queues.pop(run_id, None)

    async def events_after(
        self,
        run_id: str,
        sequence: int,
    ) -> list[dict[str, Any]]:
        async with self._lock:
            return [
                event
                for event in self._history.get(run_id, ())
                if int(event["sequence"]) > sequence
            ]

    @asynccontextmanager
    async def subscribe(
        self, run_id: str
    ) -> AsyncIterator[asyncio.Queue[dict[str, Any] | SubscriberLagged]]:
        queue: asyncio.Queue[dict[str, Any] | SubscriberLagged] = asyncio.Queue(
            maxsize=self._subscriber_queue_size
        )
        async with self._lock:
            self._queues[run_id].add(queue)
        try:
            yield queue
        finally:
            async with self._lock:
                queues = self._queues.get(run_id)
                if queues is not None:
                    queues.discard(queue)
                    if not queues:
                        self._queues.pop(run_id, None)
