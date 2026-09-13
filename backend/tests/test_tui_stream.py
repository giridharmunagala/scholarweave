from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from backend.runs.schemas import RunEventResponse, RunResponse
from scholarweave_tui.client import StreamEvent
from scholarweave_tui.stream import LiveRun

STAMP = "2026-09-12T12:00:00Z"


def event(sequence: int, kind: str, **payload: Any) -> StreamEvent:
    return StreamEvent(sequence=sequence, event_type=kind, payload=payload)


def output(sequence: int, text: str, **payload: Any) -> StreamEvent:
    return event(sequence, "model.stream", raw_type="response.output_text.delta", delta=text, **payload)


def message(text: str, agent_name: str = "Main") -> dict[str, Any]:
    return {
        "type": "message_output_item", "agent_name": agent_name,
        "content": text, "raw_item": {"role": "assistant", "content": text},
    }


def run_data(**overrides: Any) -> RunResponse:
    return RunResponse.model_validate({
        "id": "run-1", "conversation_id": "chat-1", "agent_name": "Main",
        "status": "running", "input": "Question", "final_output": None,
        "last_agent_name": None, "usage": {}, "error": None, "cancel_requested": False,
        "created_at": STAMP, "started_at": STAMP, "finished_at": None,
        "items": [], "events": [], "epochs": [], "tool_attempts": [], "goal_state": None,
        **overrides,
    })


def persisted(*events: StreamEvent) -> list[dict[str, Any]]:
    return [{**item.model_dump(), "created_at": STAMP} for item in events]


def test_projection_starts_empty_and_does_not_share_mutable_defaults() -> None:
    first, second = LiveRun(), LiveRun()
    assert first.cursor == -1
    assert first.assistant == ""
    assert first.status == "running"
    assert first.goal_state is None
    first.usage["input_tokens"] = 3
    first.activity.append("first")
    assert second.activity == []
    assert second.usage == {}


def test_output_deltas_snapshots_and_replayed_cursors_do_not_duplicate_text() -> None:
    live = LiveRun()
    assert live.apply(output(0, "Hello "))
    assert live.apply(output(2, "world"))
    assert not live.apply(output(2, "world"))
    assert not live.apply(output(1, "stale"))
    assert live.apply(output(3, "Hello world!", snapshot=True))
    assert live.apply(output(4, " Next."))
    assert live.assistant == "Hello world! Next."
    assert live.cursor == 4
    assert live.activity == []


def test_reasoning_arguments_delegated_text_and_telemetry_are_not_assistant_output() -> None:
    live = LiveRun()
    for item in [
        output(0, "Main"),
        event(1, "model.stream", raw_type="response.reasoning_text.delta", delta="Secret"),
        event(2, "model.stream", raw_type="response.reasoning_summary_text.delta", delta="Thought"),
        event(3, "model.stream", raw_type="response.function_call_arguments.delta", delta="{}"),
        output(4, "Worker", delegated=True, snapshot=True),
        event(5, "agent.stream", raw_type="response.output_text.delta", delta="Worker"),
        event(6, "model.telemetry", output_tokens=2),
        event(7, "model.phase", phase="thinking"),
        event(8, "run.item", item=message("Worker"), delegated=True),
        event(9, "agent.completed", delegated=True, agent_name="Worker", output="Worker answer"),
    ]:
        assert live.apply(item)
    assert live.assistant == "Main"
    assert live.usage == {}
    assert live.activity == ["agent.completed: Worker"]


def test_retry_retracts_unicode_characters_and_snapshots_replace_retried_text() -> None:
    live = LiveRun()
    for item in [
        output(0, "Earlier. "),
        output(1, "Partial 📚"),
        event(2, "model.retry", discarded_text_characters=9),
        output(3, "Earlier. ", snapshot=True),
        event(4, "model.retry", discarded_text_characters=9, delegated=True),
        output(5, "Complete."),
    ]:
        live.apply(item)
    assert live.assistant == "Earlier. Complete."
    live.apply(event(6, "model.retry", discarded_text_characters=1000))
    assert live.assistant == ""


@pytest.mark.parametrize("discarded", [None, True, -1, 0, 1.5, "3"])
def test_malformed_retry_counts_do_not_retract_output(discarded: Any) -> None:
    live = LiveRun()
    live.apply(output(0, "Answer"))
    live.apply(event(1, "model.retry", discarded_text_characters=discarded))
    assert live.assistant == "Answer"


def test_message_items_are_fallbacks_not_duplicate_deltas_or_worker_output() -> None:
    live = LiveRun()
    live.apply(event(0, "agent.started", agent_name="Main"))
    live.apply(event(1, "run.item", item=message("Working")))
    live.apply(event(2, "run.item", item=message("Working")))
    live.apply(event(3, "run.item", item=message("Worker", "Worker")))
    live.apply(event(4, "run.item", item=message("Answer")))
    assert live.assistant == "Working\n\nAnswer"
    live.apply(output(5, "WorkingAnswer", snapshot=True))
    live.apply(event(6, "run.item", item=message("Answer")))
    assert live.assistant == "WorkingAnswer"


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_terminal_events_set_status_and_reported_usage(status: str) -> None:
    live = LiveRun()
    live.apply(output(0, "Partial"))
    live.apply(event(
        1, f"run.{status}", final_output="Final answer",
        usage={"input_tokens": 7, "output_tokens": 3}, error="Problem",
    ))
    assert live.status == status
    assert live.assistant == "Final answer"
    assert live.usage == {"input_tokens": 7, "output_tokens": 3}
    assert "Problem" in live.activity[-1]


def test_completion_can_supply_final_output_without_any_deltas() -> None:
    live = LiveRun()
    live.apply(event(0, "run.completed", final_output="Final answer"))
    assert live.assistant == "Final answer"
    assert live.status == "completed"
    live.apply(event(1, "run.completed", final_output={"structured": "not chat text"}))
    assert live.assistant == "Final answer"


def test_usage_snapshots_replace_not_add_and_keep_performance_when_terminal_usage_omits_it() -> None:
    live = LiveRun()
    live.apply(event(0, "usage.updated", performance={"model_calls": 2, "input_tokens": 20}))
    live.apply(event(1, "usage.updated", performance={"model_calls": 3, "input_tokens": 40}))
    live.apply(event(2, "usage.updated", performance={"model_calls": 1, "input_tokens": 10}))
    live.apply(event(3, "run.completed", usage={"input_tokens": 40, "output_tokens": 5}))
    assert live.usage == {
        "input_tokens": 40, "output_tokens": 5,
        "performance": {"model_calls": 3, "input_tokens": 40},
    }
    assert all("usage" not in entry for entry in live.activity)


def test_backend_event_reconciliation_exposes_real_live_token_counts() -> None:
    live = LiveRun()
    live.apply(RunEventResponse(
        sequence=0, event_type="usage.updated", created_at=STAMP,
        payload={"performance": {"model_calls": 1, "input_tokens": 12, "output_tokens": 4}},
    ))
    assert live.usage["input_tokens"] == 12
    assert live.usage["output_tokens"] == 4
    live.apply(event(
        1, "usage.updated", performance={"model_calls": 0, "input_tokens": 0, "output_tokens": 0},
    ))
    assert live.usage["input_tokens"] == 12
    assert live.usage["output_tokens"] == 4
    live.apply(event(
        2, "usage.updated", performance={"model_calls": 2, "input_tokens": None},
    ))
    assert live.usage["input_tokens"] is None


def test_progress_keeps_completed_stages_and_tracks_context_headroom() -> None:
    started = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)

    def timed(sequence: int, kind: str, seconds: float, **payload: Any) -> StreamEvent:
        return StreamEvent(
            sequence=sequence,
            event_type=kind,
            payload=payload,
            created_at=started + timedelta(seconds=seconds),
        )

    live = LiveRun(started_at=started, context_window_tokens=32_768)
    for item in [
        timed(0, "run.started", 0),
        timed(
            1,
            "context.prepared",
            0.4,
            estimated_input_tokens=8_000,
            context_window_tokens=32_768,
        ),
        timed(2, "tool.started", 1, tool_name="search_research_notes", tool_call_id="call-1"),
        timed(3, "tool.completed", 3.5, tool_name="search_research_notes", tool_call_id="call-1"),
        timed(4, "model.phase", 4, model_call_id="model-1", phase="writing"),
        timed(
            5,
            "model.telemetry",
            6,
            model_call_id="model-1",
            context_scope="main",
            usage={"input_tokens": 8_200, "output_tokens": 300},
        ),
        timed(6, "run.completed", 7),
    ]:
        live.apply(item)

    assert [(step.label, step.status) for step in live.progress] == [
        ("Context prepared", "completed"),
        ("Search research notes", "completed"),
        ("Writing the answer", "completed"),
    ]
    assert live.progress[1].seconds() == 2.5
    assert live.context_tokens == 8_200
    assert live.context_window_tokens == 32_768
    assert live.active_progress_label() is None


def test_restore_does_not_replace_durable_totals_with_partial_performance_counts() -> None:
    live = LiveRun.restore(run_data(
        usage={
            "input_tokens": 100, "output_tokens": 20,
            "performance": {"model_calls": 2, "input_tokens": 60, "output_tokens": 10},
        },
    ))
    assert live.usage["input_tokens"] == 100
    assert live.usage["output_tokens"] == 20
    assert live.usage["performance"]["input_tokens"] == 60


def test_versioned_goal_events_and_current_work_plan_tool_results_are_projected() -> None:
    live = LiveRun()
    pending = {"id": "a", "title": "Read", "status": "pending"}
    done = {**pending, "status": "completed", "summary": "Evidence read"}
    live.apply(event(0, "goal.plan.updated", version=3, items=[pending]))
    live.apply(event(1, "goal.plan.updated", version=1, items=[]))
    assert live.goal_state == {"version": 3, "items": [pending]}
    live.apply(event(2, "goal.completed", version=4, goal_state={"items": [done]}))
    assert live.goal_state == {"version": 4, "items": [done]}
    assert live.status == "running"
    live.apply(event(
        3, "tool.completed", tool_name="update_work_item",
        result={"items": [done], "pending": [], "complete": True},
    ))
    assert live.goal_state == {"items": [done], "pending": [], "complete": True}
    live.apply(event(
        4, "tool.completed", tool_name="create_work_plan", result={"items": []}, delegated=True,
    ))
    assert live.goal_state["items"] == [done]


def test_activity_is_bounded_and_excludes_stream_and_telemetry_noise() -> None:
    live = LiveRun()
    for sequence in range(90):
        live.apply(event(sequence, "tool.started", tool_name=f"tool-{sequence}"))
    assert len(live.activity) == 80
    assert live.activity[0] == "tool.started: tool-10"
    assert live.activity[-1] == "tool.started: tool-89"
    for sequence, kind in enumerate([
        "context.compacted", "steering.queued", "steering.applied",
        "agent.failed", "run.epoch.completed", "run.recovered",
    ], start=90):
        live.apply(event(sequence, kind, reason="Actual reason"))
        assert kind in live.activity[-1]
    assert len(live.activity) == 80


def test_restore_replays_sorted_events_deduplicates_and_prefers_final_output() -> None:
    run = run_data(
        status="completed", final_output="Final",
        items=[message("First"), message("Final"), message("Final")],
        events=persisted(
            output(2, "FirstFinal", snapshot=True), output(0, "First"),
            output(1, "Final"), output(1, "Final"),
            event(3, "run.completed", final_output="Final"),
        ),
    )
    live = LiveRun.restore(run)
    assert live.assistant == "Final"
    assert live.status == "completed"
    assert live.cursor == 3
    assert not live.apply(output(3, "duplicate"))


def test_restore_nonstring_final_output_uses_unique_main_message_items() -> None:
    live = LiveRun.restore(run_data(
        status="completed", final_output={"structured": "result"},
        items=[
            message("First"), message("Final"), message("Final"),
            message("Worker handoff", "Worker"),
            {"type": "reasoning_item", "agent_name": "Main", "raw_item": "Private"},
        ],
        events=persisted(event(0, "run.item", item=message("First"))),
    ))
    assert live.assistant == "First\n\nFinal"
    assert "structured" not in live.assistant
    assert live.status == "completed"


def test_restore_preserves_streams_without_appending_persisted_message_items() -> None:
    live = LiveRun.restore(run_data(
        final_output=None, items=[message("Answer"), message("Answer")],
        events=persisted(output(5, "Answer", snapshot=True)),
    ))
    assert live.assistant == "Answer"
    assert live.cursor == 5
    assert LiveRun.restore(run_data(final_output="", items=[message("Old")])).assistant == ""


def test_restore_keeps_latest_goal_performance_and_authoritative_status_and_totals() -> None:
    run = run_data(
        status="cancelled",
        usage={"input_tokens": 60, "performance": {"model_calls": 3, "input_tokens": 60}},
        goal_state={"version": 3, "items": [{"id": "a", "status": "completed"}]},
        events=persisted(
            event(1, "run.started"),
            event(2, "goal.plan.updated", version=1, items=[]),
            event(3, "usage.updated", performance={"model_calls": 1, "input_tokens": 10}),
            event(4, "run.completed", usage={"input_tokens": 10}),
        ),
    )
    original = deepcopy(run.model_dump())
    live = LiveRun.restore(run)
    assert live.status == "cancelled"
    assert live.goal_state == run.goal_state
    assert live.usage == run.usage
    live.usage["performance"]["input_tokens"] = 999
    live.goal_state["items"].clear()
    assert run.model_dump() == original


def test_restore_reads_latest_unversioned_work_plan_from_actual_tool_events() -> None:
    live = LiveRun.restore(run_data(
        goal_state=None,
        events=persisted(
            event(0, "tool.completed", tool_name="create_work_plan", result={"items": [
                {"id": "a", "title": "Read", "status": "pending", "summary": ""},
            ]}),
            event(1, "tool.completed", tool_name="update_work_item", result={"items": [
                {"id": "a", "title": "Read", "status": "blocked", "summary": "Unavailable"},
            ], "complete": True, "pending": []}),
        ),
    ))
    assert live.goal_state["items"][0]["status"] == "blocked"
    assert live.goal_state["complete"] is True
