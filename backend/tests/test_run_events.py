from __future__ import annotations

import asyncio

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from backend.runs.events import (
    BufferedRunEventSink,
    EventBroker,
    PersistedRunEventSink,
    SubscriberLagged,
)
from backend.utils import merge_usage


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


@pytest.mark.anyio
@pytest.mark.parametrize("prefix", ["", "Earlier answer. "])
async def test_retry_retracts_partial_answer_from_persistent_snapshots(prefix: str) -> None:
    downstream = RecordingSink()
    sink = BufferedRunEventSink(downstream, initial_assistant=prefix)
    await sink.emit("model.stream", {
        "raw_type": "response.output_text.delta", "delta": "Partial.",
    })
    await sink.emit("model.stream", {"raw_type": "response.completed"})
    await sink.emit("model.retry", {"discarded_text_characters": len("Partial.")})
    snapshots = [
        payload["delta"] for mode, kind, payload in downstream.timeline
        if mode == "persisted" and kind == "model.stream"
        and payload.get("raw_type") == "response.output_text.delta"
    ]
    assert snapshots[-1] == prefix
    await sink.emit("model.retry", {"discarded_text_characters": 99, "delegated": True})
    await sink.emit("model.stream", {
        "raw_type": "response.output_text.delta", "delta": "Complete.",
    })
    await sink.flush()
    assert downstream.batches[-1][-1][1]["delta"] == prefix + "Complete."


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


class TelemetryRepository(SequenceRepository):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[Any] = []
        self.usage: dict[str, Any] = {}

    def next_event_sequence(self, _run_id: str) -> int:
        return max((event.sequence for event in self.records), default=-1) + 1

    def add_events(self, run_id, events, *, start_sequence):
        records = super().add_events(run_id, events, start_sequence=start_sequence)
        self.records.extend(records)
        return records

    def events_after(self, _run_id, sequence):
        return [event for event in self.records if event.sequence > sequence]

    def get_usage(self, _run_id):
        return dict(self.usage)

    def update_usage(self, _run_id, usage):
        self.usage = usage


@pytest.mark.anyio
async def test_telemetry_persists_across_epochs_replay_and_detached_sinks() -> None:
    repository = TelemetryRepository()
    broker = EventBroker()
    persisted = PersistedRunEventSink("run-1", repository, broker)
    detached = PersistedRunEventSink("run-1", repository, broker)
    payload = {
        "model_call_id": "main", "usage_complete": True,
        "usage": {"input_tokens": 100, "output_tokens": 10},
        "timings": {"prompt_n": 50, "prompt_ms": 100,
                    "predicted_n": 10, "predicted_ms": 500},
    }
    await BufferedRunEventSink(persisted).emit("model.telemetry", payload)
    await persisted.emit_transient("model.stream", {"delta": "not durable"})
    await BufferedRunEventSink(persisted).emit("model.telemetry", {
        **payload, "model_call_id": "second",
    })
    await persisted.emit("run.completed", {})
    await detached.emit("model.telemetry", {
        **payload, "model_call_id": "worker", "delegated": True,
    })
    # A newly reconstructed sink replays durable telemetry, including partial epochs.
    resumed = PersistedRunEventSink("run-1", repository, EventBroker())
    await resumed.emit("model.telemetry", {**payload, "model_call_id": "third"})
    await resumed.emit("model.telemetry", payload)
    performance = repository.usage["performance"]
    assert performance["model_calls"] == 4
    assert performance["main_model_calls"] == 3
    assert performance["input_tokens"] == 400
    assert performance["output_tokens"] == 40
    assert performance["generation_tokens_per_second"] == 20
    assert repository.usage["total_tokens"] == 440
    updates = [event.payload_json["performance"] for event in repository.records
               if event.event_type == "usage.updated"]
    assert [update["model_calls"] for update in updates] == [1, 2, 3, 4, 4]
    assert updates[-1] == performance
    sequences = [event.sequence for event in repository.records]
    assert sequences == sorted(set(sequences))


def test_usage_merging_weights_server_rates_by_active_time() -> None:
    merged = merge_usage(
        {"performance": {"timing_source": "server", "timed_prompt_tokens": 100,
                         "prompt_seconds": 0.1, "prompt_tokens_per_second": 1000,
                         "usage_complete": False}},
        {"performance": {"timing_source": "server", "timed_prompt_tokens": 200,
                         "prompt_seconds": 2, "prompt_tokens_per_second": 100,
                         "usage_complete": True}},
    )["performance"]
    assert merged["prompt_tokens_per_second"] == round(300 / 2.1, 3)
    assert merged["usage_complete"] is False
    with_legacy = merge_usage(
        {"input_tokens": 10000, "prompt_seconds": 100, "prompt_tokens_per_second": 100},
        merged,
    )
    assert with_legacy["prompt_seconds"] == 2.1
    assert with_legacy["prompt_tokens_per_second"] == round(300 / 2.1, 3)


@pytest.mark.anyio
async def test_resuming_legacy_usage_keeps_spend_but_not_wall_clock_rates() -> None:
    repository = TelemetryRepository()
    repository.add_events("run-1", [
        ("model.completed", {"usage": {"input_tokens": 50, "output_tokens": 10}}),
        ("model.completed", {"delegated": True,
                             "usage": {"input_tokens": 20, "output_tokens": 5}}),
    ], start_sequence=0)
    sink = PersistedRunEventSink("run-1", repository, EventBroker())
    await sink.emit("model.telemetry", {
        "model_call_id": "new", "usage_complete": True,
        "usage": {"input_tokens": 30, "output_tokens": 5},
    })
    await sink.emit("model.completed", {"usage": {"input_tokens": 30, "output_tokens": 5}})
    assert repository.usage["input_tokens"] == 100
    assert repository.usage["output_tokens"] == 20
    assert repository.usage["performance"]["model_calls"] == 3
    assert repository.usage["performance"]["prompt_tokens_per_second"] is None


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
async def test_stream_performance_uses_server_active_time_not_wall_clock() -> None:
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
        "model.telemetry",
        {"model_call_id": "main", "usage_complete": True,
         "usage": {"input_tokens": 200, "output_tokens": 80},
         "timings": {"prompt_n": 100, "prompt_ms": 100, "predicted_n": 80, "predicted_ms": 500}},
    )

    assert sink.performance() == {
        "model_calls": 1,
        "main_model_calls": 1,
        "input_tokens": 200,
        "output_tokens": 80,
        "delegated_model_calls": 0,
        "delegated_input_tokens": 0,
        "delegated_output_tokens": 0,
        "input_tokens_estimated": False,
        "output_tokens_estimated": False,
        "usage_complete": True,
        "timing_source": "server",
        "timed_prompt_tokens": 100.0,
        "timed_output_tokens": 80.0,
        "prompt_seconds": 0.1,
        "generation_seconds": 0.5,
        "prompt_tokens_per_second": 1000.0,
        "generation_tokens_per_second": 160.0,
    }
    assert downstream.timeline[-1] == (
        "persisted",
        "usage.updated",
        {"performance": sink.performance()},
    )


@pytest.mark.anyio
async def test_stream_performance_emits_cumulative_usage_after_each_model_call() -> None:
    downstream = RecordingSink()
    sink = BufferedRunEventSink(downstream)

    await sink.emit("model.started", {"input_character_count": 40})
    await sink.emit(
        "model.telemetry",
        {"model_call_id": "one", "usage_complete": True,
         "usage": {"input_tokens": 10, "output_tokens": 4}},
    )
    await sink.emit("model.started", {"input_character_count": 80})
    await sink.emit(
        "model.telemetry",
        {"model_call_id": "two", "usage_complete": True,
         "usage": {"input_tokens": 20, "output_tokens": 6}},
    )

    updates = [
        payload["performance"]
        for _, event_type, payload in downstream.timeline
        if event_type == "usage.updated"
    ]
    assert [update["model_calls"] for update in updates] == [1, 2]
    assert updates[-1]["input_tokens"] == 30
    assert updates[-1]["output_tokens"] == 10


@pytest.mark.anyio
async def test_stream_performance_never_fabricates_missing_usage_or_timings() -> None:
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
    await sink.emit("model.telemetry", {"model_call_id": "missing", "usage": {}})

    assert sink.performance()["input_tokens"] == 0
    assert sink.performance()["output_tokens"] == 0
    assert sink.performance()["usage_complete"] is False
    assert sink.performance()["prompt_tokens_per_second"] is None
    assert sink.performance()["generation_tokens_per_second"] is None


@pytest.mark.anyio
async def test_telemetry_counts_retries_delegates_and_compaction_exactly_once() -> None:
    downstream = RecordingSink()
    sink = BufferedRunEventSink(downstream)
    for call_id, scope in [("main", "main"), ("retry", "main"), ("worker", "delegate"),
                           ("compact", "compaction")]:
        payload = {
            "model_call_id": call_id, "context_scope": scope, "usage_complete": True,
            "usage": {"input_tokens": 1000, "output_tokens": 10},
            "timings": {"prompt_n": 100, "prompt_ms": 200,
                        "predicted_n": 10, "predicted_ms": 500},
        }
        await sink.emit("model.telemetry", payload)
        await sink.emit("model.telemetry", payload)
        await sink.emit("model.completed", {"usage": payload["usage"]})
    # A slow worker runs long after the main turn, without diluting speed by idle time.
    await sink.emit("model.telemetry", {
        "model_call_id": "detached", "delegated": True, "usage_complete": True,
        "usage": {"input_tokens": 2000, "output_tokens": 60},
        "timings": {"prompt_n": 300, "prompt_ms": 1000,
                    "predicted_n": 60, "predicted_ms": 4000},
    })
    performance = sink.performance()
    assert performance["model_calls"] == 5
    assert performance["main_model_calls"] == 2
    assert performance["input_tokens"] == 6000
    assert performance["output_tokens"] == 100
    assert performance["delegated_model_calls"] == 2
    assert performance["prompt_tokens_per_second"] == round(700 / 1.8, 3)
    assert performance["generation_tokens_per_second"] == round(100 / 6, 3)
    assert len([event for event in downstream.timeline if event[1] == "usage.updated"]) == 5


@pytest.mark.anyio
async def test_missing_or_invalid_timing_does_not_pollute_measured_rate() -> None:
    sink = BufferedRunEventSink(RecordingSink())
    for call_id, timings in [
        ("valid", {"prompt_n": 0, "prompt_ms": 100, "predicted_n": 2, "predicted_ms": 100}),
        ("missing", {}),
        ("invalid", {"prompt_n": 200, "prompt_ms": 0, "predicted_n": 3, "predicted_ms": float("nan")}),
    ]:
        await sink.emit("model.telemetry", {
            "model_call_id": call_id, "usage_complete": True,
            "usage": {"input_tokens": 1000, "output_tokens": 200}, "timings": timings,
        })
    assert sink.performance()["input_tokens"] == 3000
    assert sink.performance()["prompt_tokens_per_second"] == 0
    assert sink.performance()["generation_tokens_per_second"] == 20


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


@pytest.mark.anyio
async def test_event_broker_evicts_slow_subscriber_without_blocking_publishers() -> None:
    broker = EventBroker(subscriber_queue_size=1)

    async with broker.subscribe("run-1") as queue:
        await broker.publish(
            "run-1",
            {"sequence": 0, "event_type": "model.stream", "payload": {}},
        )
        await asyncio.wait_for(
            broker.publish(
                "run-1",
                {"sequence": 1, "event_type": "model.stream", "payload": {}},
            ),
            timeout=0.1,
        )
        assert isinstance(queue.get_nowait(), SubscriberLagged)
        assert "run-1" not in broker._queues
        assert [
            event["sequence"] for event in await broker.events_after("run-1", -1)
        ] == [0, 1]

    assert "run-1" not in broker._queues
