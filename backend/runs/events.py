from __future__ import annotations

import asyncio
from typing import Any

from backend.runs.broker import EventBroker
from backend.runs.repository import RunRepository


class PersistedRunEventSink:
    def __init__(
        self,
        run_id: str,
        repository: RunRepository,
        broker: EventBroker,
    ) -> None:
        self._run_id = run_id
        self._repository = repository
        self._broker = broker
        self._lock = asyncio.Lock()

    async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        async with self._lock:
            event = self._repository.add_event(self._run_id, event_type, payload)
            await self._broker.publish(
                self._run_id,
                {
                    "sequence": event.sequence,
                    "event_type": event.event_type,
                    "payload": event.payload_json,
                    "created_at": event.created_at.isoformat(),
                },
            )
