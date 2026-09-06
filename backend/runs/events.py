from __future__ import annotations

import asyncio
import math
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Protocol

from backend.runs.repository import RunLease, RunRepository
from backend.utils import as_utc


class SubscriberLagged:
    """Signals that durable replay must resume from the subscriber's last cursor."""


SUBSCRIBER_LAGGED = SubscriberLagged()


class EventBroker:
    """Fan out run events in process while retaining a short replay window."""

    def __init__(self, *, subscriber_queue_size: int = 256) -> None:
        if subscriber_queue_size < 1:
            raise ValueError("Subscriber queue size must be positive.")
        self._subscriber_queue_size = subscriber_queue_size
        self._queues: dict[
            str, set[asyncio.Queue[dict[str, Any] | SubscriberLagged]]
        ] = defaultdict(set)
        self._history: dict[str, deque[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()
        self._sink_locks: dict[str, asyncio.Lock] = {}
        self._next_sequences: dict[str, int] = {}

    def sink_lock(self, run_id: str) -> asyncio.Lock:
        return self._sink_locks.setdefault(run_id, asyncio.Lock())

    def take_sequences(self, run_id: str, minimum: int, count: int) -> int:
        start = max(minimum, self._next_sequences.get(run_id, 0))
        self._next_sequences[run_id] = start + count
        return start

    async def publish(self, run_id: str, event: dict[str, Any]) -> None:
        """Publish an event and disconnect subscribers whose queues have overflowed."""
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
        """Return buffered events newer than a subscriber's sequence cursor."""
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
        """Register a bounded event queue for the lifetime of the context manager."""
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
    cumulative_telemetry = True

    def __init__(
        self,
        run_id: str,
        repository: RunRepository,
        broker: EventBroker,
        lease: Callable[[], RunLease | None] | None = None,
    ) -> None:
        self._run_id = run_id
        self._repository = repository
        self._broker = broker
        self._lease = lease
        self._lock = (
            broker.sink_lock(run_id) if hasattr(broker, "sink_lock") else asyncio.Lock()
        )
        self._next_sequence = repository.next_event_sequence(run_id)
        self._telemetry = ModelTelemetry()
        self._telemetry_cursor = -1
        self._native_telemetry_seen = False

    def current_lease(self) -> RunLease | None:
        return self._lease() if self._lease is not None else None

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
        if any(kind in {"run.completed", "run.failed", "run.cancelled"} for kind, _ in events):
            self._refresh_telemetry()
            if self._telemetry.performance()["model_calls"]:
                usage = self._repository.get_usage(self._run_id)
                events = [
                    (kind, {**payload, "usage": usage})
                    if kind in {"run.completed", "run.failed", "run.cancelled"}
                    else (kind, payload)
                    for kind, payload in events
                ]
        start_sequence = self._take_sequences(len(events))
        lease = self._lease() if self._lease is not None else None
        records = (
            self._repository.add_events_owned(
                lease,
                events,
                start_sequence=start_sequence,
            )
            if lease is not None
            else self._repository.add_events(
                self._run_id,
                events,
                start_sequence=start_sequence,
            )
        )
        for event in records:
            await self._broker.publish(
                self._run_id,
                {
                    "sequence": event.sequence,
                    "event_type": event.event_type,
                    "payload": event.payload_json,
                    "created_at": as_utc(event.created_at).isoformat(),
                },
            )
        if any(kind == "model.telemetry" for kind, _ in events):
            self._refresh_telemetry()
            await self._emit_batch([
                ("usage.updated", {"performance": self._telemetry.performance()}),
            ])
        elif any(kind in {"run.completed", "run.failed", "run.cancelled",
                          "run.epoch.completed"} for kind, _ in events):
            self._refresh_telemetry()

    def _refresh_telemetry(self) -> None:
        for event in self._repository.events_after(self._run_id, self._telemetry_cursor):
            self._telemetry_cursor = max(self._telemetry_cursor, event.sequence)
            if event.event_type == "model.telemetry":
                self._native_telemetry_seen = True
                self._telemetry.record(event.payload_json)
            elif event.event_type == "model.completed" and not self._native_telemetry_seen:
                # Runs resumed after an upgrade can have legacy lifecycle usage.
                # Never reinterpret their wall-clock speeds as server measurements.
                payload = event.payload_json
                usage = payload.get("usage")
                self._telemetry.record({
                    **payload,
                    "model_call_id": f"legacy:{event.sequence}",
                    "usage_complete": isinstance(usage, dict) and any(
                        _token_count(usage.get(key)) > 0
                        for key in ("input_tokens", "output_tokens")
                    ),
                    "timings": {},
                })
        performance = self._telemetry.performance()
        if not performance["model_calls"]:
            return
        usage = self._repository.get_usage(self._run_id)
        usage.update({
            "requests": performance["model_calls"],
            "input_tokens": performance["input_tokens"],
            "output_tokens": performance["output_tokens"],
            "total_tokens": performance["input_tokens"] + performance["output_tokens"],
            "performance": performance,
        })
        lease = self.current_lease()
        if lease is None:
            self._repository.update_usage(self._run_id, usage)
        else:
            self._repository.update_usage_owned(lease, usage)

    def _take_sequences(self, count: int) -> int:
        if hasattr(self._broker, "take_sequences"):
            self._next_sequence = self._broker.take_sequences(
                self._run_id, self._next_sequence, count,
            )
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
            "created_at": datetime.now(UTC).isoformat(),
        }


class ModelTelemetry:
    """Idempotent spend and server active-time accounting, also used during replay."""

    def __init__(self) -> None:
        self._seen_model_calls: set[str] = set()
        self._model_calls = 0
        self._main_model_calls = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._delegated_model_calls = 0
        self._delegated_input_tokens = 0
        self._delegated_output_tokens = 0
        self._usage_complete = True
        self._timed_prompt_tokens = 0.0
        self._timed_output_tokens = 0.0
        self._prompt_seconds = 0.0
        self._generation_seconds = 0.0
        self._prompt_timed_calls = 0
        self._generation_timed_calls = 0

    def performance(self) -> dict[str, Any]:
        prompt_rate = (
            self._timed_prompt_tokens / self._prompt_seconds
            if self._prompt_seconds > 0
            else None
        )
        generation_rate = (
            self._timed_output_tokens / self._generation_seconds
            if self._generation_seconds > 0
            else None
        )
        return {
            "model_calls": self._model_calls,
            "main_model_calls": self._main_model_calls,
            "input_tokens": self._input_tokens,
            "output_tokens": self._output_tokens,
            "delegated_model_calls": self._delegated_model_calls,
            "delegated_input_tokens": self._delegated_input_tokens,
            "delegated_output_tokens": self._delegated_output_tokens,
            "input_tokens_estimated": False,
            "output_tokens_estimated": False,
            "usage_complete": self._usage_complete,
            "timing_source": "server",
            "rate_units": "tokens/s",
            "prompt_timed_calls": self._prompt_timed_calls,
            "generation_timed_calls": self._generation_timed_calls,
            "timed_prompt_tokens": self._timed_prompt_tokens,
            "timed_output_tokens": self._timed_output_tokens,
            "prompt_seconds": self._prompt_seconds,
            "generation_seconds": self._generation_seconds,
            "prompt_tokens_per_second": round(prompt_rate, 3)
            if prompt_rate is not None
            else None,
            "generation_tokens_per_second": round(generation_rate, 3)
            if generation_rate is not None
            else None,
        }

    def record(self, payload: dict[str, Any]) -> bool:
        call_id = payload.get("model_call_id")
        if not isinstance(call_id, str) or not call_id or call_id in self._seen_model_calls:
            return False
        self._seen_model_calls.add(call_id)
        usage = payload.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        input_tokens = _token_count(usage.get("input_tokens"))
        output_tokens = _token_count(usage.get("output_tokens"))
        self._usage_complete = self._usage_complete and payload.get("usage_complete") is True
        self._input_tokens += input_tokens
        self._output_tokens += output_tokens
        self._model_calls += 1
        if payload.get("delegated") is True or payload.get("context_scope") == "delegate":
            self._delegated_model_calls += 1
            self._delegated_input_tokens += input_tokens
            self._delegated_output_tokens += output_tokens
        elif payload.get("context_scope", "main") == "main":
            self._main_model_calls += 1
        timings = payload.get("timings")
        timings = timings if isinstance(timings, dict) else {}
        prompt = _server_phase(timings, "prompt_n", "prompt_ms")
        generation = _server_phase(timings, "predicted_n", "predicted_ms")
        if prompt is not None:
            self._prompt_timed_calls += 1
            self._timed_prompt_tokens += prompt[0]
            self._prompt_seconds += prompt[1]
        if generation is not None:
            self._generation_timed_calls += 1
            self._timed_output_tokens += generation[0]
            self._generation_seconds += generation[1]
        return True

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
        self._telemetry = ModelTelemetry()

    def current_lease(self) -> RunLease | None:
        current_lease = getattr(self._downstream, "current_lease", None)
        return current_lease() if current_lease is not None else None

    async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        async with self._lock:
            await self._emit(event_type, payload)

    async def flush(self) -> None:
        async with self._lock:
            await self._flush_live()
            await self._emit_snapshot()

    def performance(self) -> dict[str, Any]:
        return self._telemetry.performance()

    async def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        raw_type = str(payload.get("raw_type") or "")
        delta = payload.get("delta")
        usage_updated = False
        delegated = payload.get("delegated") is True
        is_delta = (
            event_type == "model.stream"
            and raw_type.endswith(".delta")
            and isinstance(delta, str)
        )

        if event_type == "model.telemetry":
            usage_updated = self._telemetry.record(payload)

        if event_type == "model.stream":
            if raw_type == "response.created":
                await self._flush_live()
                await self._emit_snapshot()
                self._seen_delta_types.clear()
                await self._downstream.emit_transient(event_type, payload)
                return
            if is_delta:
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
        if event_type == "model.retry" and not delegated:
            discarded = payload.get("discarded_text_characters")
            if isinstance(discarded, int) and discarded > 0:
                self._assistant_parts = ["".join(self._assistant_parts)[:-discarded]]
                self._snapshot_dirty = True
                await self._emit_snapshot()
        if usage_updated and not getattr(self._downstream, "cumulative_telemetry", False):
            await self._downstream.emit(
                "usage.updated",
                {"performance": self.performance()},
            )

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


def _token_count(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


def _server_phase(
    timings: dict[str, Any], tokens_key: str, milliseconds_key: str,
) -> tuple[float, float] | None:
    tokens, milliseconds = timings.get(tokens_key), timings.get(milliseconds_key)
    if not all(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(value)
        for value in (tokens, milliseconds)
    ):
        return None
    if tokens < 0 or milliseconds <= 0:
        return None
    return float(tokens), float(milliseconds) / 1000
