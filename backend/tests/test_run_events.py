from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from backend.runs.broker import EventBroker
from backend.runs.events import BufferedRunEventSink, PersistedRunEventSink


class RecordingSink:
    def __init__(self) -> None:
        self.timeline: list[tuple[str, str, dict[str, Any]]] = []
        self.batches: list[list[tuple[str, dict[str, Any]]]] = []

    async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self.timeline.append(("persisted", event_type, payload))

    async def emit_transient(
        self,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        self.timeline.append(("live", event_type, payload))

    async def emit_batch(
        self,
        events: list[tuple[str, dict[str, Any]]],
    ) -> None:
        self.batches.append(events)
        self.timeline.extend(
            ("persisted", event_type, payload)
            for event_type, payload in events
        )


class SequenceRepository:
    def __init__(self) -> None:
        self.writes: list[tuple[int, list[tuple[str, dict[str, Any]]]]] = []

    def next_event_sequence(self, _run_id: str) -> int:
        return 7

    def add_events(
        self,
        _run_id: str,
        events: list[tuple[str, dict[str, Any]]],
        *,
        start_sequence: int,
    ) -> list[Any]:
        self.writes.append((start_sequence, events))
        return [
            SimpleNamespace(
                sequence=start_sequence + offset,
                event_type=event_type,
                payload_json=payload,
                created_at=datetime.now(UTC),
            )
            for offset, (event_type, payload) in enumerate(events)
        ]


class RecordingBroker:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def publish(self, _run_id: str, event: dict[str, Any]) -> None:
        self.events.append(event)


@pytest.mark.anyio
async def test_persisted_and_transient_events_share_in_memory_sequence() -> None:
    repository = SequenceRepository()
    broker = RecordingBroker()
    sink = PersistedRunEventSink(
        "run-1",
        repository,
        broker,
    )

    await sink.emit("run.started", {})
    await sink.emit_transient("model.stream", {"delta": "live"})
    await sink.emit_batch(
        [
            ("model.stream", {"delta": "snapshot-1"}),
            ("model.stream", {"delta": "snapshot-2"}),
        ]
    )

    assert [event["sequence"] for event in broker.events] == [7, 8, 9, 10]
    assert [start for start, _ in repository.writes] == [7, 9]


@pytest.mark.anyio
async def test_model_deltas_are_coalesced_without_reordering_events() -> None:
    downstream = RecordingSink()
    sink = BufferedRunEventSink(
        downstream,
        max_delta_chars=6,
        max_delay_seconds=10,
    )

    await sink.emit(
        "model.stream",
        {"raw_type": "response.output_text.delta", "delta": "first"},
    )
    await sink.emit(
        "model.stream",
        {"raw_type": "response.output_text.delta", "delta": " second"},
    )
    await sink.emit("model.completed", {"usage": {}})

    assert downstream.timeline == [
        (
            "live",
            "model.stream",
            {"raw_type": "response.output_text.delta", "delta": "first"},
        ),
        (
            "live",
            "model.stream",
            {"raw_type": "response.output_text.delta", "delta": " second"},
        ),
        (
            "persisted",
            "model.stream",
            {
                "raw_type": "response.output_text.delta",
                "delta": "first second",
                "snapshot": True,
            },
        ),
        ("persisted", "model.completed", {"usage": {}}),
    ]
    assert len(downstream.batches) == 1


@pytest.mark.anyio
async def test_model_delta_buffer_flushes_on_size_and_delay() -> None:
    downstream = RecordingSink()
    now = 0.0
    sink = BufferedRunEventSink(
        downstream,
        max_delta_chars=3,
        max_delay_seconds=0.05,
        clock=lambda: now,
    )

    await sink.emit(
        "model.stream",
        {"raw_type": "response.reasoning_summary_text.delta", "delta": "a"},
    )
    await sink.emit(
        "model.stream",
        {"raw_type": "response.reasoning_summary_text.delta", "delta": "b"},
    )
    await sink.emit(
        "model.stream",
        {"raw_type": "response.reasoning_summary_text.delta", "delta": "cd"},
    )
    now = 0.1
    await sink.emit(
        "model.stream",
        {"raw_type": "response.reasoning_summary_text.delta", "delta": "e"},
    )
    await sink.emit(
        "model.stream",
        {"raw_type": "response.reasoning_summary_text.delta", "delta": "f"},
    )
    await sink.flush()

    assert [
        payload["delta"]
        for delivery, event_type, payload in downstream.timeline
        if delivery == "live"
        and event_type == "model.stream"
        and "delta" in payload
    ] == [
        "a",
        "bcd",
        "ef",
    ]


@pytest.mark.anyio
async def test_new_response_emits_its_first_delta_immediately() -> None:
    downstream = RecordingSink()
    sink = BufferedRunEventSink(
        downstream,
        max_delta_chars=100,
        max_delay_seconds=10,
    )

    delta_type = "response.output_text.delta"
    await sink.emit("model.stream", {"raw_type": delta_type, "delta": "one"})
    await sink.emit("model.stream", {"raw_type": delta_type, "delta": " buffered"})
    await sink.emit("model.stream", {"raw_type": "response.created"})
    await sink.emit("model.stream", {"raw_type": delta_type, "delta": "two"})

    assert [
        (event_type, payload)
        for delivery, event_type, payload in downstream.timeline
        if delivery == "live"
    ] == [
        ("model.stream", {"raw_type": delta_type, "delta": "one"}),
        ("model.stream", {"raw_type": delta_type, "delta": " buffered"}),
        ("model.stream", {"raw_type": "response.created"}),
        ("model.stream", {"raw_type": delta_type, "delta": "two"}),
    ]


@pytest.mark.anyio
async def test_response_completion_persists_cumulative_snapshots_in_one_batch() -> None:
    downstream = RecordingSink()
    sink = BufferedRunEventSink(downstream)

    await sink.emit("model.stream", {"raw_type": "response.created"})
    await sink.emit(
        "model.stream",
        {"raw_type": "response.reasoning_summary_text.delta", "delta": "Think"},
    )
    await sink.emit(
        "model.stream",
        {"raw_type": "response.output_text.delta", "delta": "Answer"},
    )
    await sink.emit("model.stream", {"raw_type": "response.completed"})

    assert downstream.batches == [
        [
            (
                "model.stream",
                {
                    "raw_type": "response.reasoning_summary_text.delta",
                    "delta": "Think",
                    "snapshot": True,
                },
            ),
            (
                "model.stream",
                {
                    "raw_type": "response.output_text.delta",
                    "delta": "Answer",
                    "snapshot": True,
                },
            ),
        ]
    ]
    assert downstream.timeline[-1] == (
        "live",
        "model.stream",
        {"raw_type": "response.completed"},
    )


@pytest.mark.anyio
async def test_stream_performance_uses_reported_tokens_and_measured_phases() -> None:
    downstream = RecordingSink()
    now = 10.0
    sink = BufferedRunEventSink(downstream, clock=lambda: now)

    await sink.emit("model.started", {"input_character_count": 400})
    now = 12.0
    await sink.emit(
        "model.stream",
        {"raw_type": "response.output_text.delta", "delta": "Answer"},
    )
    now = 14.0
    await sink.emit(
        "model.completed",
        {"usage": {"input_tokens": 200, "output_tokens": 80}},
    )

    assert sink.performance() == {
        "model_calls": 1,
        "input_tokens": 200,
        "output_tokens": 80,
        "input_tokens_estimated": False,
        "output_tokens_estimated": False,
        "prompt_seconds": 2.0,
        "generation_seconds": 2.0,
        "prompt_tokens_per_second": 100.0,
        "generation_tokens_per_second": 40.0,
    }


@pytest.mark.anyio
async def test_stream_performance_labels_token_estimates_when_usage_is_missing() -> None:
    downstream = RecordingSink()
    now = 1.0
    sink = BufferedRunEventSink(downstream, clock=lambda: now)

    await sink.emit("model.started", {"input_character_count": 20})
    now = 2.0
    await sink.emit(
        "model.stream",
        {"raw_type": "response.reasoning_text.delta", "delta": "12345678"},
    )
    now = 3.0
    await sink.emit("model.completed", {"usage": {}})

    assert sink.performance()["input_tokens"] == 5
    assert sink.performance()["output_tokens"] == 2
    assert sink.performance()["input_tokens_estimated"] is True
    assert sink.performance()["output_tokens_estimated"] is True


@pytest.mark.anyio
async def test_resumed_stream_snapshot_keeps_pre_pause_text() -> None:
    downstream = RecordingSink()
    sink = BufferedRunEventSink(
        downstream,
        initial_reasoning="Before pause. ",
        initial_assistant="Partial answer. ",
    )

    await sink.emit("model.stream", {"raw_type": "response.created"})
    await sink.emit(
        "model.stream",
        {"raw_type": "response.reasoning_summary_text.delta", "delta": "After pause."},
    )
    await sink.emit(
        "model.stream",
        {"raw_type": "response.output_text.delta", "delta": "Finished."},
    )
    await sink.emit("model.stream", {"raw_type": "response.completed"})

    assert downstream.batches[-1][0][1]["delta"] == "Before pause. After pause."
    assert downstream.batches[-1][1][1]["delta"] == "Partial answer. Finished."


@pytest.mark.anyio
async def test_event_broker_replays_live_history_until_run_finishes() -> None:
    broker = EventBroker()
    await broker.publish(
        "run-1",
        {"sequence": 3, "event_type": "model.stream", "payload": {"delta": "one"}},
    )
    await broker.publish(
        "run-1",
        {"sequence": 4, "event_type": "model.stream", "payload": {"delta": "two"}},
    )

    assert [
        event["sequence"] for event in await broker.events_after("run-1", 3)
    ] == [4]

    await broker.publish(
        "run-1",
        {"sequence": 5, "event_type": "run.completed", "payload": {}},
    )
    assert await broker.events_after("run-1", -1) == []
