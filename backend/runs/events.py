from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from typing import Any, Protocol

from backend.runs.broker import EventBroker
from backend.runs.repository import RunRepository


class RunEventSink(Protocol):
    async def emit(self, event_type: str, payload: dict[str, Any]) -> None: ...

    async def emit_transient(
        self,
        event_type: str,
        payload: dict[str, Any],
    ) -> None: ...

    async def emit_batch(
        self,
        events: list[tuple[str, dict[str, Any]]],
    ) -> None: ...


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
        self._next_sequence = repository.next_event_sequence(run_id)

    async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        async with self._lock:
            await self._emit_batch([(event_type, payload)])

    async def emit_transient(
        self,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        async with self._lock:
            sequence = self._take_sequences(1)
            await self._broker.publish(
                self._run_id,
                self._published_event(sequence, event_type, payload),
            )

    async def emit_batch(
        self,
        events: list[tuple[str, dict[str, Any]]],
    ) -> None:
        if not events:
            return
        async with self._lock:
            await self._emit_batch(events)

    async def _emit_batch(
        self,
        events: list[tuple[str, dict[str, Any]]],
    ) -> None:
        start_sequence = self._take_sequences(len(events))
        records = self._repository.add_events(
            self._run_id,
            events,
            start_sequence=start_sequence,
        )
        for event in records:
            await self._broker.publish(
                self._run_id,
                {
                    "sequence": event.sequence,
                    "event_type": event.event_type,
                    "payload": event.payload_json,
                    "created_at": event.created_at.isoformat(),
                },
            )

    def _take_sequences(self, count: int) -> int:
        start = self._next_sequence
        self._next_sequence += count
        return start

    @staticmethod
    def _published_event(
        sequence: int,
        event_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "sequence": sequence,
            "event_type": event_type,
            "payload": payload,
        }


class BufferedRunEventSink:
    """Streams model deltas from memory and persists one snapshot per model call."""

    def __init__(
        self,
        downstream: RunEventSink,
        *,
        max_delta_chars: int = 512,
        max_delay_seconds: float = 0.05,
        clock: Callable[[], float] = time.monotonic,
        initial_reasoning: str = "",
        initial_assistant: str = "",
    ) -> None:
        self._downstream = downstream
        self._max_delta_chars = max_delta_chars
        self._max_delay_seconds = max_delay_seconds
        self._clock = clock
        self._lock = asyncio.Lock()
        self._pending_raw_type: str | None = None
        self._pending_parts: list[str] = []
        self._pending_chars = 0
        self._pending_since = 0.0
        self._seen_delta_types: set[str] = set()
        self._reasoning_parts: list[str] = [initial_reasoning] if initial_reasoning else []
        self._assistant_parts: list[str] = [initial_assistant] if initial_assistant else []
        self._snapshot_dirty = False
        self._model_started_at: float | None = None
        self._first_generated_at: float | None = None
        self._model_input_chars = 0
        self._model_generated_chars = 0
        self._model_calls = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._input_tokens_estimated = False
        self._output_tokens_estimated = False
        self._prompt_seconds = 0.0
        self._generation_seconds = 0.0

    async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        async with self._lock:
            await self._emit(event_type, payload)

    async def flush(self) -> None:
        async with self._lock:
            await self._flush_live()
            await self._emit_snapshot()

    def performance(self) -> dict[str, Any]:
        prompt_rate = (
            self._input_tokens / self._prompt_seconds
            if self._input_tokens and self._prompt_seconds > 0
            else None
        )
        generation_rate = (
            self._output_tokens / self._generation_seconds
            if self._output_tokens and self._generation_seconds > 0
            else None
        )
        return {
            "model_calls": self._model_calls,
            "input_tokens": self._input_tokens,
            "output_tokens": self._output_tokens,
            "input_tokens_estimated": self._input_tokens_estimated,
            "output_tokens_estimated": self._output_tokens_estimated,
            "prompt_seconds": round(self._prompt_seconds, 6),
            "generation_seconds": round(self._generation_seconds, 6),
            "prompt_tokens_per_second": round(prompt_rate, 3)
            if prompt_rate is not None
            else None,
            "generation_tokens_per_second": round(generation_rate, 3)
            if generation_rate is not None
            else None,
        }

    async def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        raw_type = str(payload.get("raw_type") or "")
        delta = payload.get("delta")
        is_delta = (
            event_type == "model.stream"
            and raw_type.endswith(".delta")
            and isinstance(delta, str)
        )

        if event_type == "model.started":
            self._start_model_call(payload)
        elif event_type == "model.completed":
            self._finish_model_call(payload)

        if event_type == "model.stream":
            if raw_type == "response.created":
                await self._flush_live()
                await self._emit_snapshot()
                self._seen_delta_types.clear()
                await self._downstream.emit_transient(event_type, payload)
                return
            if is_delta:
                self._record_generated_delta(delta)
                self._append_snapshot_delta(raw_type, delta)
                await self._emit_live_delta(raw_type, delta)
                return
            await self._flush_live()
            if raw_type == "response.completed":
                await self._emit_snapshot()
            await self._downstream.emit_transient(event_type, payload)
            return

        await self._flush_live()
        await self._emit_snapshot()
        await self._downstream.emit(event_type, payload)

    def _start_model_call(self, payload: dict[str, Any]) -> None:
        self._model_started_at = self._clock()
        self._first_generated_at = None
        self._model_generated_chars = 0
        input_chars = payload.get("input_character_count")
        self._model_input_chars = input_chars if isinstance(input_chars, int) else 0

    def _record_generated_delta(self, delta: str) -> None:
        if self._model_started_at is not None and self._first_generated_at is None:
            self._first_generated_at = self._clock()
        self._model_generated_chars += len(delta)

    def _finish_model_call(self, payload: dict[str, Any]) -> None:
        if self._model_started_at is None:
            return
        finished_at = self._clock()
        first_generated_at = self._first_generated_at or finished_at
        self._prompt_seconds += max(0.0, first_generated_at - self._model_started_at)
        self._generation_seconds += max(0.0, finished_at - first_generated_at)

        usage = payload.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if not isinstance(input_tokens, int) or input_tokens <= 0:
            input_tokens = _estimated_tokens(self._model_input_chars)
            self._input_tokens_estimated = True
        if not isinstance(output_tokens, int) or output_tokens <= 0:
            output_tokens = _estimated_tokens(self._model_generated_chars)
            self._output_tokens_estimated = True
        self._input_tokens += input_tokens
        self._output_tokens += output_tokens
        self._model_calls += 1
        self._model_started_at = None
        self._first_generated_at = None
        self._model_input_chars = 0
        self._model_generated_chars = 0

    async def _emit_live_delta(self, raw_type: str, delta: str) -> None:
        if raw_type not in self._seen_delta_types:
            await self._flush_live()
            self._seen_delta_types.add(raw_type)
            await self._downstream.emit_transient(
                "model.stream",
                {"raw_type": raw_type, "delta": delta},
            )
            return

        if self._pending_raw_type != raw_type:
            await self._flush_live()
            self._pending_raw_type = raw_type
            self._pending_since = self._clock()

        self._pending_parts.append(delta)
        self._pending_chars += len(delta)
        if (
            self._pending_chars >= self._max_delta_chars
            or self._clock() - self._pending_since >= self._max_delay_seconds
        ):
            await self._flush_live()

    async def _flush_live(self) -> None:
        if self._pending_raw_type is None:
            return
        raw_type = self._pending_raw_type
        delta = "".join(self._pending_parts)
        self._pending_raw_type = None
        self._pending_parts = []
        self._pending_chars = 0
        self._pending_since = 0.0
        await self._downstream.emit_transient(
            "model.stream",
            {"raw_type": raw_type, "delta": delta},
        )

    def _append_snapshot_delta(self, raw_type: str, delta: str) -> None:
        if raw_type in {
            "response.reasoning_text.delta",
            "response.reasoning_summary_text.delta",
        }:
            self._reasoning_parts.append(delta)
            self._snapshot_dirty = True
        elif raw_type == "response.output_text.delta":
            self._assistant_parts.append(delta)
            self._snapshot_dirty = True

    async def _emit_snapshot(self) -> None:
        if not self._snapshot_dirty:
            return
        events: list[tuple[str, dict[str, Any]]] = []
        if self._reasoning_parts:
            events.append(
                (
                    "model.stream",
                    {
                        "raw_type": "response.reasoning_summary_text.delta",
                        "delta": "".join(self._reasoning_parts),
                        "snapshot": True,
                    },
                )
            )
        if self._assistant_parts:
            events.append(
                (
                    "model.stream",
                    {
                        "raw_type": "response.output_text.delta",
                        "delta": "".join(self._assistant_parts),
                        "snapshot": True,
                    },
                )
            )
        await self._downstream.emit_batch(events)
        self._snapshot_dirty = False


def _estimated_tokens(character_count: int) -> int:
    return math.ceil(character_count / 4) if character_count > 0 else 0
