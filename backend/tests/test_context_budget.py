from __future__ import annotations

import json
import math
from dataclasses import replace
from typing import Any

import pytest

from backend.agents.context import ScholarWeaveContext, ToolReceipt
from backend.agents.context_budget import (
    CHECKPOINT_MESSAGE_PREFIX,
    ContextBudgetPolicy,
    _replace_checkpointed_paper_reads,
    model_context_budget,
)
from backend.agents.harness import (
    AgentDefinition, FunctionTool, HarnessError, ModelBinding, ModelSettings, RunPolicyViolation,
)
from backend.conversations.steering import (
    SteeringInbox,
    SteeringMessage,
    steering_message_id,
)
from backend.core.config import Settings
from backend.runs.hooks import start_agent_invocation
from backend.tests.harness_support import FakeClient


class Runtime:
    def __init__(self) -> None:
        self.bounded: list[tuple[str, object]] = []
        self.histories: dict[str, list[dict[str, Any]]] = {}
        self.unavailable_batches: list[dict[str, Any]] = []

    def store_context_history(self, items, context):
        del context
        ref = f"history-{len(self.histories) + 1}"
        self.histories[ref] = json.loads(json.dumps(items))
        return {"result_ref": ref, "size_bytes": len(json.dumps(items).encode())}

    async def invoke(self, catalog_id, arguments, context):
        raise AssertionError("No tool invocation was expected.")

    def mark_paper_summary_batch_unavailable(self, result, context):
        self.unavailable_batches.append(json.loads(json.dumps(result)))

    async def bound_tool_result(
        self,
        catalog_id,
        result,
        context,
        *,
        max_tokens=None,
    ):
        self.bounded.append((catalog_id, result))
        return {
            "truncated": True,
            "result_ref": "retained-result",
            "preview": {"references": ["https://example.com/source"]},
        }


class CheckpointRuntime(Runtime):
    def __init__(self) -> None:
        super().__init__()
        self.stored: list[dict] = []

    def store_context_checkpoint(self, checkpoint, context):
        del context
        self.stored.append(json.loads(json.dumps(checkpoint)))
        return {"artifact_id": "checkpoint-1"}


class Sink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, payload))


@pytest.mark.anyio
@pytest.mark.parametrize("window", [8000, 80000, 131072])
async def test_dedicated_summary_reserves_model_aware_input_output_and_safety(tmp_path, window):
    settings = Settings(data_dir=tmp_path)
    definition = agent()
    definition.binding = replace(definition.binding, context_window_tokens=window)
    sink = Sink()
    context = ScholarWeaveContext(
        run_id="summary-budget", tool_runtime=Runtime(), event_sink=sink,
        metadata={"paper_summary_document_id": "paper-1"},
    )
    prepared = await ContextBudgetPolicy(settings).prepare(
        definition, [{"role": "user", "content": "Summarize the supplied source."}],
        definition.instructions, context, turn_index=0,
    )
    input_tokens, output_tokens, safety = model_context_budget(window)
    metrics = next(payload for kind, payload in sink.events if kind == "context.prepared")
    assert input_tokens + output_tokens + safety == window
    assert metrics["input_budget_tokens"] == input_tokens
    assert metrics["safety_headroom_tokens"] == safety
    assert prepared.response_max_tokens == output_tokens
    assert prepared.safety_headroom_tokens == safety
    assert prepared.response_retry_max_tokens == window - prepared.estimated_input_tokens - safety
    assert metrics["request_overhead_tokens"] > 0
    if window == 80000:
        assert (input_tokens, output_tokens, safety) == (52000, 20000, 8000)


@pytest.mark.anyio
@pytest.mark.parametrize("calibration", [1.0, 1.25])
async def test_summary_uses_full_input_allowance_without_second_high_water_reduction(tmp_path, calibration):
    settings = Settings(data_dir=tmp_path, agent_context_high_water_ratio=0.5)
    definition = agent()
    definition.binding = replace(definition.binding, context_window_tokens=80000)
    sink = Sink()
    context = ScholarWeaveContext(
        run_id="summary-full-allowance", tool_runtime=Runtime(), event_sink=sink,
        metadata={
            "paper_summary_document_id": "paper-1",
            "_context_token_ratios": {definition.id: calibration},
        },
    )
    items = [
        {"role": "user", "content": "Summarize this paper."},
        {"role": "assistant", "content": "Evidence sentence. " * int(10000 / calibration)},
        *recent_rounds(),
    ]
    prepared = await ContextBudgetPolicy(settings).prepare(
        definition, items, definition.instructions, context, turn_index=0,
    )
    metrics = next(payload for kind, payload in sink.events if kind == "context.prepared")
    assert 47000 < prepared.estimated_input_tokens <= 52000
    assert prepared.items == items
    assert metrics["compacted"] is False
    assert metrics["input_budget_tokens"] == 52000
    assert metrics["token_estimate_ratio"] == calibration
    assert prepared.response_max_tokens == 20000
    assert prepared.response_retry_max_tokens + prepared.estimated_input_tokens + 8000 == 80000


@pytest.mark.anyio
@pytest.mark.parametrize("outcome", ["timed", "untimed", "failed", "cancelled"])
async def test_compaction_reports_one_telemetry_attempt_without_narrative(tmp_path, outcome):
    import asyncio
    from openai import OpenAIError

    class Completion(_Completion):
        def model_dump(self):
            payload = super().model_dump()
            payload["usage"] = {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}
            if outcome == "timed":
                payload["timings"] = {
                    "prompt_n": 80, "prompt_ms": 100, "predicted_n": 30, "predicted_ms": 200,
                }
            return payload

    async def create(**parameters):
        if outcome == "failed":
            raise OpenAIError("Provider failed.")
        if outcome == "cancelled":
            raise asyncio.CancelledError
        return Completion("Exact preserved evidence.")

    definition = agent(client=FakeClient(create))
    sink = Sink()
    context = ScholarWeaveContext(run_id="compaction-telemetry", tool_runtime=Runtime(), event_sink=sink)
    call = ContextBudgetPolicy(Settings(data_dir=tmp_path))._request_summary(
        definition, definition.binding,
        [{"role": "assistant", "content": "Earlier exact evidence."}],
        {"checkpoint_id": "checkpoint-telemetry", "caveats": []},
        context_window_tokens=8000, max_tokens=512, context=context,
    )
    if outcome in {"failed", "cancelled"}:
        with pytest.raises(OpenAIError if outcome == "failed" else asyncio.CancelledError):
            await call
    else:
        assert await call == "Exact preserved evidence."
    assert [kind for kind, _ in sink.events] == ["model.telemetry"]
    payload = sink.events[0][1]
    assert payload["model_call_id"]
    assert payload["context_scope"] == "compaction"
    assert payload["agent_id"].endswith(":compaction")
    assert payload["usage"]["requests"] == 1
    assert payload["completed"] == (outcome not in {"failed", "cancelled"})
    assert payload["usage_complete"] == payload["completed"]
    if payload["completed"]:
        assert payload["usage"]["input_tokens"] == 120
        assert payload["usage"]["output_tokens"] == 30
    assert bool(payload["timings"]) == (outcome == "timed")


class Session:
    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []

    async def add_items(self, items: list[dict[str, str]]) -> None:
        self.items.extend(items)

    async def get_items(self) -> list[dict[str, str]]:
        return list(self.items)


def summarizing_client(text: str = "The research objective remains active; prior evidence was retained.") -> FakeClient:
    async def create(**parameters: Any):
        client.requests.append(parameters)
        return _Completion(text)

    client = FakeClient(create)
    return client


class _Completion:
    def __init__(self, text: str) -> None:
        self._text = text

    def model_dump(self) -> dict[str, Any]:
        return {"choices": [{"message": {"role": "assistant", "content": self._text}}]}


def agent(
    name: str = "Worker",
    *,
    client: FakeClient | None = None,
    agent_id: str = "worker",
) -> AgentDefinition:
    async def read_history(*args):
        raise AssertionError("This test does not execute model tool calls.")

    return AgentDefinition(
        id=agent_id,
        name=name,
        instructions="Research carefully.",
        binding=ModelBinding(
            client=client or summarizing_client(),
            model_name="stub-model",
            provider_kind="ollama",
        ),
        model_settings=ModelSettings(),
        tools=[FunctionTool(
            "read_tool_result", "Read retained exact history.",
            {"type": "object", "properties": {
                "result_ref": {"type": "string"},
                "offset": {"type": "integer"},
                "limit": {"type": "integer"},
            }, "required": ["result_ref", "offset", "limit"], "additionalProperties": False},
            read_history,
        )],
    )


def policy(settings: Settings, **overrides: Any) -> ContextBudgetPolicy:
    return ContextBudgetPolicy(settings, **overrides)


def recent_rounds() -> list[dict[str, Any]]:
    return [
        {"role": "assistant", "content": "Recent complete round one."},
        {"role": "assistant", "content": "Recent complete round two."},
    ]


async def prepare(
    settings: Settings,
    definition: AgentDefinition,
    items: list[dict[str, Any]],
    context: ScholarWeaveContext,
    *,
    instructions: str = "",
    turn_index: int = 1,
    **overrides: Any,
):
    return await policy(settings, **overrides).prepare(
        definition,
        items,
        instructions,
        context,
        turn_index=turn_index,
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_steering_is_injected_at_the_next_model_call_boundary(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
    )
    sink = Sink()
    context = ScholarWeaveContext(
        run_id="run-1",
        conversation_id="conversation-1",
        tool_runtime=Runtime(),
        event_sink=sink,
    )
    inbox = SteeringInbox()
    session = Session()
    inbox.bind_session(session)
    context.metadata["_steering_inbox"] = inbox
    definition = agent()
    first_input = [{"role": "user", "content": "Research the topic."}]

    first = await prepare(settings, definition, list(first_input), context)
    message = inbox.queue("Answer directly with the evidence already found.")
    second = await prepare(
        settings,
        definition,
        [*first_input, {"role": "assistant", "content": "I will search first."}],
        context,
    )

    steering_item = {
        "role": "user",
        "content": "Answer directly with the evidence already found.",
    }
    assert steering_item not in first.items
    assert second.items[-1] == steering_item
    assert second.working_items is not None
    assert steering_item not in second.working_items
    assert len(session.items) == 1
    assert steering_message_id(session.items[0]) == message.id
    assert (
        "steering.applied",
        {"message_id": message.id, "content": message.content},
    ) in sink.events


@pytest.mark.anyio
async def test_summary_checkpoint_replaces_raw_paper_batch_on_next_turn(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=80_000,
    )
    context = ScholarWeaveContext(run_id="summary-run", tool_runtime=Runtime())
    raw_pages = "raw paper page content " * 1_000
    understanding = "Pages 1-10 understanding with key methods and results [p.4]."
    prepared = await prepare(
        settings,
        agent("Paper Summarizer"),
        [
            {
                "type": "function_call",
                "name": "paper_summary_checkpoint",
                "call_id": "checkpoint-read-1",
                "arguments": json.dumps(
                    {
                        "document_id": "paper-1",
                        "action": "read",
                        "content": None,
                        "offset": 0,
                        "limit": 8000,
                    }
                ),
            },
            {
                "type": "function_call_output",
                "call_id": "checkpoint-read-1",
                "output": json.dumps(
                    {
                        "status": "available",
                        "checkpoint_path": "checkpoint.md",
                        "content": "prior checkpoint evidence",
                    }
                ),
            },
            {
                "type": "function_call",
                "name": "read_paper_summary_batch",
                "call_id": "read-1",
                "arguments": '{"document_id":"paper-1","action":"pages","start":1}',
            },
            {
                "type": "function_call_output",
                "call_id": "read-1",
                "output": raw_pages,
            },
            {
                "type": "function_call",
                "name": "paper_summary_checkpoint",
                "call_id": "checkpoint-1",
                "arguments": json.dumps(
                    {
                        "document_id": "paper-1",
                        "action": "append",
                        "content": understanding,
                        "offset": None,
                        "limit": None,
                    }
                ),
            },
            {
                "type": "function_call_output",
                "call_id": "checkpoint-1",
                "output": json.dumps(
                    {
                        "status": "appended",
                        "checkpoint_path": "checkpoint.md",
                        "checkpointed_batch": 1,
                        "coverage": {"kind": "pages", "start": 1, "end": 10},
                        "final_checkpoint": "complete compact evidence [p.4]",
                    }
                ),
            },
        ],
        context,
    )

    checkpoint_read_output = json.loads(prepared.items[1]["output"])
    read_output = json.loads(prepared.items[3]["output"])
    assert checkpoint_read_output["status"] == "replaced_by_summary_checkpoint"
    assert read_output["status"] == "replaced_by_summary_checkpoint"
    assert read_output["checkpoint_path"] == "checkpoint.md"
    assert read_output["checkpointed_batch"] == 1
    assert read_output["coverage"] == {"kind": "pages", "start": 1, "end": 10}
    assert raw_pages not in json.dumps(prepared.items)
    assert "prior checkpoint evidence" not in json.dumps(prepared.items)
    assert understanding not in json.dumps(prepared.items)
    assert "complete compact evidence [p.4]" in prepared.items[5]["output"]
    assert json.loads(prepared.items[4]["arguments"])["content"] is None
    assert prepared.working_items == prepared.items


@pytest.mark.anyio
async def test_steering_session_persistence_is_idempotent() -> None:
    inbox = SteeringInbox()
    session = Session()
    inbox.bind_session(session)
    message = inbox.queue("Answer directly.")
    pending = inbox.take_pending_or_close()

    await inbox.persist(pending)
    await inbox.persist(pending)

    assert session.items == [message.session_item()]


@pytest.mark.anyio
async def test_recovered_steering_already_in_session_is_not_replayed_twice(
    tmp_path,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
    )
    context = ScholarWeaveContext(
        run_id="run-1",
        conversation_id="conversation-1",
        tool_runtime=Runtime(),
    )
    inbox = SteeringInbox()
    session = Session()
    message = SteeringMessage(id="steering-1", content="Answer directly.")
    await session.add_items([message.session_item()])
    inbox.bind_session(session)
    inbox.restore(message.id, message.content)
    context.metadata["_steering_inbox"] = inbox

    prepared = await prepare(
        settings,
        agent(),
        [
            message.session_item(),
            {"role": "user", "content": "Resume after restart."},
        ],
        context,
    )

    assert prepared.items.count(message.input_item()) == 1
    replayed = await prepare(
        settings, agent(), list(prepared.working_items), context, turn_index=2
    )
    assert replayed.items.count(message.input_item()) == 1


@pytest.mark.anyio
async def test_high_water_compaction_replaces_raw_history_with_checkpoint(
    tmp_path,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=4_096,
        agent_context_high_water_ratio=0.7,
        agent_context_compaction_target_tokens=1_024,
        tool_result_max_tokens=16_000,
    )
    settings = settings.model_copy(update={"agent_context_model_summary_enabled": True})
    sink = Sink()
    context = ScholarWeaveContext(
        run_id="run-1",
        tool_runtime=Runtime(),
        event_sink=sink,
        metadata={
            "work_plan": [
                {
                    "id": "evidence",
                    "title": "Collect evidence",
                    "status": "in_progress",
                }
            ],
            "paper_activity": [
                {
                    "action": "acquired",
                    "document_id": "paper-1",
                    "title": "Durable Research",
                }
            ],
        },
    )
    context.receipts.append(
        ToolReceipt(
            kind="file",
            title="Created papers/paper-1/notes.md",
            href="/workspace?path=papers/paper-1/notes.md",
        )
    )
    invocation_id = await start_agent_invocation(context, "Worker")
    large_result = {
        "type": "function_call_output",
        "call_id": "call-1",
        "output": {
            "result_ref": "artifact-1",
            "citation": "https://example.com/source",
            "text": "Evidence excerpt.",
        },
    }
    summarizer = summarizing_client()
    definition = agent(client=summarizer)
    original_user_message = {
        "role": "user",
        "content": "Research the topic exactly as requested — keep this punctuation!",
    }

    compacted = await prepare(
        settings,
        definition,
        [
            original_user_message,
            {"role": "assistant", "content": "Prior reasoning " * 500},
            large_result,
            *recent_rounds(),
        ],
        context,
        instructions="Use cited evidence.",
    )

    serialized = json.dumps(compacted.items)
    assert "Prior reasoning " * 500 not in serialized
    assert original_user_message in compacted.items
    assert "artifact-1" in serialized
    assert "The research objective remains active" in serialized
    assert len(summarizer.requests) == 1
    assert summarizer.requests[0].get("stream") is None
    checkpoint = context.metadata["context_checkpoints"][0]
    assert checkpoint["summary_method"] == "model"
    assert "history-1" in checkpoint["references"]
    assert checkpoint["work_state"]["paper_activity"][0]["document_id"] == "paper-1"
    lifecycle = [
        (event_type, payload)
        for event_type, payload in sink.events
        if event_type.startswith("agent.")
    ]
    assert lifecycle == [
        ("agent.started", {"agent_name": "Worker", "invocation_id": invocation_id})
    ]
    compaction_events = [
        event_type
        for event_type, _ in sink.events
        if event_type.startswith("context.compact")
    ]
    assert compaction_events == ["context.compaction_started", "context.compacted"]
    assert compacted.working_items is not None
    assert compacted.working_items[0]["content"].startswith(CHECKPOINT_MESSAGE_PREFIX)
    assert compacted.working_items[0]["_scholarweave_context_policy_version"] == 2


@pytest.mark.anyio
async def test_compaction_falls_back_when_the_summarizer_fails(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=4_096,
        agent_context_high_water_ratio=0.7,
        agent_context_compaction_target_tokens=1_024,
        tool_result_max_tokens=16_000,
    )
    settings = settings.model_copy(update={"agent_context_model_summary_enabled": True})
    sink = Sink()
    context = ScholarWeaveContext(
        run_id="run-1",
        tool_runtime=Runtime(),
        event_sink=sink,
    )
    from openai import APIError

    failing = FakeClient.failing(
        APIError("provider down", request=None, body=None)  # type: ignore[arg-type]
    )

    compacted = await prepare(
        settings,
        agent(client=failing),
        [
            {"role": "user", "content": "Research the topic."},
            {"role": "assistant", "content": "Prior reasoning " * 500},
            *recent_rounds(),
        ],
        context,
    )

    assert context.metadata["context_checkpoints"][0]["summary_method"] == (
        "structured_fallback"
    )
    assert "context.compaction_failed" in [event_type for event_type, _ in sink.events]
    assert compacted.items[0]["content"].startswith(CHECKPOINT_MESSAGE_PREFIX)


@pytest.mark.anyio
async def test_compacted_history_is_adopted_as_the_working_set(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=4_096,
        agent_context_high_water_ratio=0.7,
        agent_context_compaction_target_tokens=1_024,
        tool_result_max_tokens=16_000,
    )
    context = ScholarWeaveContext(run_id="run-1", tool_runtime=Runtime())
    definition = agent()
    budget = policy(settings)
    initial_input = [
        {"role": "user", "content": "Research the topic."},
        {"role": "assistant", "content": "Prior reasoning " * 1_000},
        *recent_rounds(),
    ]

    first = await budget.prepare(definition, list(initial_input), "", context, turn_index=1)
    working = [*first.working_items, {"role": "assistant", "content": "New turn."}]
    second = await budget.prepare(definition, working, "", context, turn_index=2)

    assert len(json.dumps(second.items)) < len(json.dumps(initial_input)) // 2
    assert not any(item.get("content") == initial_input[1]["content"] for item in second.items)
    assert {"role": "assistant", "content": "New turn."} in second.items
    assert len(context.metadata["context_checkpoints"]) == 1
    assert first.items[0]["content"].startswith(CHECKPOINT_MESSAGE_PREFIX)


@pytest.mark.anyio
async def test_compaction_keeps_tool_call_and_output_in_one_recent_chunk(
    tmp_path,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=4_096,
        agent_context_high_water_ratio=0.7,
        agent_context_compaction_target_tokens=1_024,
        tool_result_max_tokens=16_000,
    )
    context = ScholarWeaveContext(run_id="run-1", tool_runtime=Runtime())
    call = {
        "type": "function_call",
        "name": "search_web",
        "call_id": "call-1",
        "arguments": '{"query":"evidence"}',
    }
    output = {
        "type": "function_call_output",
        "call_id": "call-1",
        "output": '{"result":"found"}',
    }

    compacted = await prepare(
        settings,
        agent(),
        [
            {"role": "user", "content": "Research the topic."},
            {"role": "assistant", "content": "Prior reasoning " * 1_000},
            *recent_rounds(),
            call,
            output,
        ],
        context,
    )

    assert call in compacted.items
    assert output in compacted.items


@pytest.mark.anyio
async def test_compaction_preserves_the_newest_user_request(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=4_096,
        agent_context_high_water_ratio=0.7,
        agent_context_compaction_target_tokens=1_024,
        tool_result_max_tokens=16_000,
    )
    context = ScholarWeaveContext(run_id="run-1", tool_runtime=Runtime())
    latest = {"role": "user", "content": "LATEST REQUEST"}

    compacted = await prepare(
        settings,
        agent(),
        [
            {"role": "assistant", "content": "Old reasoning " * 1_000},
            {"role": "assistant", "content": "Large search analysis " * 500},
            *recent_rounds(),
            latest,
        ],
        context,
    )

    assert latest in compacted.items


@pytest.mark.anyio
async def test_stored_checkpoint_includes_model_summary(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=4_096,
        agent_context_high_water_ratio=0.7,
        agent_context_compaction_target_tokens=1_024,
        tool_result_max_tokens=16_000,
    )
    settings = settings.model_copy(update={"agent_context_model_summary_enabled": True})
    runtime = CheckpointRuntime()
    context = ScholarWeaveContext(run_id="run-1", tool_runtime=runtime)

    await prepare(
        settings,
        agent(),
        [
            {"role": "user", "content": "Research the topic."},
            {"role": "assistant", "content": "Prior reasoning " * 500},
            *recent_rounds(),
        ],
        context,
    )

    assert runtime.stored[0]["summary_method"] == "model"
    assert "prior evidence was retained" in runtime.stored[0]["model_summary"]


@pytest.mark.anyio
async def test_new_tool_outputs_are_not_rebounded_when_the_request_fits(
    tmp_path,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        tool_result_max_tokens=512,
    )
    runtime = Runtime()
    context = ScholarWeaveContext(run_id="run-1", tool_runtime=runtime)

    prepared = await prepare(
        settings,
        agent(),
        [
            {
                "type": "function_call_output",
                "name": "custom_large_tool",
                "call_id": "call-1",
                "output": "large " * 2_000,
            }
        ],
        context,
    )

    assert runtime.bounded == []
    assert prepared.items[0]["call_id"] == "call-1"
    assert prepared.items[0]["output"] == "large " * 2_000


@pytest.mark.anyio
async def test_legacy_task_budget_does_not_limit_the_configured_model_window(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=128_000,
        agent_context_use_model_window=False,
        agent_context_model_summary_enabled=False,
    )
    definition = agent()
    sink = Sink()
    context = ScholarWeaveContext(run_id="run-1", tool_runtime=Runtime(), event_sink=sink)
    original = {"role": "user", "content": "NEVER drop this exact constraint."}
    result = await prepare(
        settings, definition,
        [original, {"role": "assistant", "content": "Old analysis. " * 10000},
         *recent_rounds()], context,
    )
    assert original in result.items
    assert not definition.binding.client.requests
    assert not context.metadata.get("context_checkpoints")
    metrics = [payload for name, payload in sink.events if name == "context.prepared"][-1]
    assert metrics["working_context_tokens"] == 128000 - settings.agent_context_response_reserve_tokens
    assert metrics["compacted"] is False
    assert metrics["estimated_input_tokens"] > settings.agent_working_context_tokens
    assert metrics["estimated_input_tokens"] + metrics["response_headroom_tokens"] <= 128_000
    assert metrics["request_overhead_tokens"] > 0
    assert result.response_max_tokens == 2_048


@pytest.mark.anyio
@pytest.mark.parametrize("oversized", ["user", "instructions", "tools"])
async def test_impossible_context_fails_before_any_provider_call(tmp_path, oversized) -> None:
    settings = Settings(data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace")
    definition = agent()
    items = [{"role": "user", "content": "Exact constraint"}]
    instructions = ""
    if oversized == "user":
        items[0]["content"] = "原文 constraint " * 20000
    elif oversized == "instructions":
        instructions = "System instruction " * 20000
    elif oversized == "tools":
        async def invoke(*args):
            return None
        definition.tools = [FunctionTool(
            "large_tool", "Tool guidance " * 20000,
            {"type": "object", "properties": {}}, invoke,
        )]
    context = ScholarWeaveContext(run_id="run-1", tool_runtime=Runtime())
    with pytest.raises(RunPolicyViolation) as failure:
        await prepare(settings, definition, items, context, instructions=instructions)
    assert failure.value.policy == "working_context"
    assert not definition.binding.client.requests


@pytest.mark.anyio
async def test_compaction_preserves_checkpoint_looking_user_text_and_developer_constraints(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace")
    protected = [
        {"role": "user", "content": CHECKPOINT_MESSAGE_PREFIX + "\nThis is actual user text."},
        {"role": "developer", "content": "Do not alter this exact developer instruction."},
    ]
    context = ScholarWeaveContext(run_id="run-1", tool_runtime=Runtime())
    budget = policy(settings)
    first = await budget.prepare(
        agent(), [*protected, {"role": "assistant", "content": "Older work. " * 10000},
                  *recent_rounds()],
        "", context, turn_index=0,
    )
    second = await budget.prepare(
        agent(), [*first.working_items, {"role": "assistant", "content": "More work. " * 10000},
                  *recent_rounds()],
        "", context, turn_index=1,
    )
    for item in protected:
        assert second.items.count(item) == 1


@pytest.mark.anyio
async def test_large_queued_steering_cannot_bypass_context_budget(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace")
    definition = agent()
    session = Session()
    inbox = SteeringInbox()
    inbox.bind_session(session)
    text = "Preserve this exact steering instruction. " * 10000
    message = inbox.queue(text)
    context = ScholarWeaveContext(
        run_id="run-1", tool_runtime=Runtime(), metadata={"_steering_inbox": inbox}
    )
    with pytest.raises(RunPolicyViolation):
        await prepare(settings, definition, [{"role": "user", "content": "Research."}], context)
    assert session.items == [message.session_item()]
    assert not definition.binding.client.requests


@pytest.mark.anyio
async def test_immutable_constraints_may_exceed_soft_target_but_not_hard_budget(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace")
    context = ScholarWeaveContext(run_id="run-1", tool_runtime=Runtime())
    item = {"role": "user", "content": "Keep this exact constraint. " * 1300}
    result = await prepare(settings, agent(), [item], context)
    assert result.items == [item]


@pytest.mark.anyio
async def test_superseded_internal_epoch_prompts_do_not_accumulate_as_user_constraints(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace")
    context = ScholarWeaveContext(run_id="run-1", tool_runtime=Runtime())
    user = {"role": "user", "content": "Real original user constraint."}
    old = {
        "role": "user", "content": "Obsolete plan state. " * 10000,
        "_scholarweave_internal_continuation": True,
    }
    current = {
        "role": "user", "content": "Current next action.",
        "_scholarweave_internal_continuation": True,
    }
    result = await prepare(settings, agent(), [user, old, current], context)
    assert user in result.items
    assert old not in result.working_items
    assert current in result.working_items


@pytest.mark.anyio
async def test_structured_compaction_keeps_json_tool_references_across_recompaction(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_model_summary_enabled=False,
    )
    context = ScholarWeaveContext(run_id="run-1", tool_runtime=Runtime())
    definition = agent()
    items = [
        {"role": "user", "content": "Research."},
        {"type": "function_call", "name": "read", "call_id": "read-1", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "read-1",
         "output": json.dumps({"result_ref": "evidence-ref", "citation": "https://example.com/paper"})},
        {"role": "assistant", "content": "Old analysis. " * 10000},
        *recent_rounds(),
    ]
    first = await prepare(settings, definition, items, context)
    second = await prepare(
        settings, definition,
        [*first.working_items, {"role": "assistant", "content": "New analysis. " * 10000},
         *recent_rounds()],
        context,
    )
    assert "evidence-ref" in json.dumps(second.items)
    assert "https://example.com/paper" in json.dumps(second.items)
    assert not definition.binding.client.requests


def _pending_paper_batch(document_id: str, batch: int, content: str) -> list[dict[str, Any]]:
    call_id = f"{document_id}-{batch}"
    return [
        {"type": "function_call", "name": "read_paper_summary_batch", "call_id": call_id,
         "arguments": json.dumps({"document_id": document_id, "action": "pages", "start": batch})},
        {"type": "function_call_output", "call_id": call_id,
         "output": {"checkpoint_required": True, "summary_batch": batch,
                    "checkpoint_path": f"{document_id}/evidence/index.json", "content": content}},
    ]


@pytest.mark.parametrize("status", ["appended", "reconciled"])
@pytest.mark.parametrize(
    ("read_batch_id", "append_batch_id", "ordinal", "should_replace"),
    [
        ("source-a", "source-a", 1, True),
        ("source-a", "source-b", 1, False),
        ("source-a", None, 1, False),
        (None, "source-a", 1, True),
        (None, None, 1, True),
        ("source-a", "source-a", 2, True),
    ],
)
def test_paper_checkpoint_replacement_prefers_verified_batch_identity(
    status, read_batch_id, append_batch_id, ordinal, should_replace,
) -> None:
    read = _pending_paper_batch("paper-1", 1, "Exact raw source")
    if read_batch_id is not None:
        read[1]["output"]["batch_id"] = read_batch_id
    append_result = {
        "status": status,
        "document_id": "paper-1",
        "checkpointed_batch": ordinal,
        "checkpoint_path": "paper-1/evidence/index.json",
    }
    if append_batch_id is not None:
        append_result["batch_id"] = append_batch_id
    append = [
        {"type": "function_call", "name": "paper_summary_checkpoint", "call_id": "append",
         "arguments": '{"document_id":"paper-1","action":"append","content":"Verified evidence"}'},
        {"type": "function_call_output", "call_id": "append", "output": append_result},
    ]
    result = _replace_checkpointed_paper_reads([*read, *append])
    if should_replace:
        assert result[1]["output"]["status"] == "replaced_by_summary_checkpoint"
        assert result[1]["output"]["batch_id"] == append_batch_id
    else:
        assert result[1] == read[1]


def test_paper_checkpoint_replacement_is_scoped_to_verified_document_and_batch() -> None:
    first = _pending_paper_batch("paper-1", 1, "checkpointed raw")
    second = _pending_paper_batch("paper-1", 2, "uncheckpointed raw")
    unrelated = _pending_paper_batch("paper-2", 1, "other paper raw")
    generic = [
        {"type": "function_call", "name": "read_tool_result", "call_id": "generic", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "generic", "output": "unrelated result"},
    ]
    append = [
        {"type": "function_call", "name": "paper_summary_checkpoint", "call_id": "append",
         "arguments": '{"document_id":"paper-1","action":"append","content":"Verified evidence"}'},
        {"type": "function_call_output", "call_id": "append",
         "output": {"status": "appended", "checkpointed_batch": 1,
                    "checkpoint_path": "paper-1/evidence/index.json",
                    "next_start": 2, "next_offset": 640, "complete": False}},
    ]
    result = _replace_checkpointed_paper_reads([*first, *second, *unrelated, *generic, *append])
    assert result[1]["output"]["status"] == "replaced_by_summary_checkpoint"
    assert result[1]["output"]["next_start"] == 2
    assert result[1]["output"]["next_offset"] == 640
    assert result[1]["output"]["complete"] is False
    assert result[2:8] == [*second, *unrelated, *generic]
    first[1]["output"]["coverage"] = {"start": 1, "end": 1}
    append[-1]["output"]["coverage"] = {"start": 2, "end": 2}
    assert _replace_checkpointed_paper_reads([*first, *append])[1] == first[1]
    append[-1]["output"]["status"] = "failed"
    unchanged = [*first, *append]
    assert _replace_checkpointed_paper_reads(unchanged) == unchanged


@pytest.mark.anyio
async def test_compaction_preserves_pending_paper_batch_verbatim_until_durable_append(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace")
    runtime = Runtime()
    context = ScholarWeaveContext(run_id="run-1", tool_runtime=runtime)
    batch = _pending_paper_batch("paper-1", 1, "Exact uncheckpointed source text. " * 80)
    items = [*batch, {"role": "assistant", "content": "Old analysis. " * 10000},
             *recent_rounds()]
    result = await prepare(settings, agent(), items, context)
    assert all(item in result.items for item in batch)
    assert not runtime.bounded
    oversized = _pending_paper_batch("paper-1", 2, "Uncheckpointed text. " * 10000)
    paged = await prepare(settings, agent(), oversized, context)
    assert paged.items[0] == oversized[0]
    receipt = paged.items[1]["output"]
    assert receipt["checkpoint_required"] is True
    assert runtime.histories[receipt["result_ref"]] == [oversized[1]]
    assert not runtime.bounded


@pytest.mark.anyio
async def test_optional_compaction_model_call_has_background_priority(tmp_path) -> None:
    from backend.providers.inference import _priority, inference_priority

    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_model_summary_enabled=True,
        agent_context_window_tokens=24000,
    )
    client = summarizing_client()
    original_create = client.chat.completions.create
    observed = []

    async def create(**parameters):
        observed.append(_priority.get())
        return await original_create(**parameters)

    client.chat.completions.create = create
    context = ScholarWeaveContext(run_id="run-1", tool_runtime=Runtime())
    with inference_priority("interactive"):
        await prepare(
            settings, agent(client=client),
            [{"role": "assistant", "content": "Older analysis. " * 5200},
             *recent_rounds()], context,
        )
        assert _priority.get() == "interactive"
    assert observed == ["background"]


@pytest.mark.anyio
async def test_model_compaction_retains_specific_understanding_across_later_compaction(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
                        agent_context_window_tokens=16000)
    assert settings.agent_context_model_summary_enabled is True
    finding = "Accuracy was 91% on 120 samples [p.2]; independent replication is still unverified."
    summaries = iter([finding, "New work progressed; continue using prior findings."])

    async def create(**parameters):
        client.requests.append(parameters)
        return _Completion(next(summaries))

    client = FakeClient(create)
    definition = agent(client=client)
    context = ScholarWeaveContext(run_id="run-1", tool_runtime=Runtime())
    original = [
        {"role": "user", "content": "Keep the exact evaluation result and its caveat."},
        {"role": "assistant", "content": "Background detail. " * 2800 + finding},
        *recent_rounds(),
    ]
    first = await prepare(settings, definition, original, context)
    assert finding in client.requests[0]["messages"][-1]["content"]
    assert not context.metadata["context_checkpoints"][0].get("summary_input_truncated")
    second = await prepare(
        settings, definition,
        [*first.working_items, {"role": "assistant", "content": "New background detail. " * 2400},
         *recent_rounds()],
        context,
    )
    assert finding in client.requests[1]["messages"][-1]["content"]
    assert finding in json.dumps(second.items)


@pytest.mark.anyio
async def test_summary_skips_oversized_prefix_instead_of_sampling_recent_history(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=4096, agent_context_compaction_target_tokens=1024,
    )
    context = ScholarWeaveContext(run_id="run-1", tool_runtime=Runtime())
    result = await prepare(
        settings, agent(),
        [{"role": "assistant", "content": "Long history. " * 10000},
         *recent_rounds()], context,
    )
    checkpoint = context.metadata["context_checkpoints"][0]
    assert checkpoint["summary_input_truncated"] is False
    assert checkpoint["summary_method"] == "structured_input_limit"
    retained = json.loads(result.items[0]["content"].split("\n\n")[-1])
    assert retained["summary_input_truncated"] is False


@pytest.mark.anyio
async def test_new_output_fitting_selected_model_window_is_not_truncated(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=128_000,
        agent_context_compaction_target_tokens=8_192,
        tool_result_max_tokens=3_000,
    )
    runtime = Runtime()
    context = ScholarWeaveContext(run_id="run-1", tool_runtime=runtime)
    definition = agent(agent_id="small-local-model")

    prepared = await prepare(
        settings,
        definition,
        [
            {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": "large " * 1_000,
            }
        ],
        context,
        context_window_tokens_by_agent={"small-local-model": 4_096},
    )

    assert not runtime.bounded
    assert prepared.items[0]["output"] == "large " * 1_000


def research_round(index: int, characters: int = 18000) -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": f"Constraint {index}: preserve punctuation — exactly!"},
        {"role": "assistant", "content": f"Round {index} analysis remains exact."},
        {"type": "function_call", "name": "read_workspace_file", "call_id": f"read-{index}",
         "arguments": json.dumps({"path": f"papers/source-{index}.md"})},
        {"type": "function_call_output", "call_id": f"read-{index}",
         "output": json.dumps({"content": str(index % 10) * characters})},
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(("ratio", "eviction_rounds"), [(0.85, 1), (0.7, 2)])
async def test_eighty_thousand_window_sixteen_turns_ages_old_reads_not_recent_rounds(
    tmp_path, ratio, eviction_rounds,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=80000, agent_context_use_model_window=True,
        agent_context_high_water_ratio=ratio, agent_working_context_tokens=12000,
        agent_context_compaction_target_tokens=8192, tool_result_max_tokens=512,
    )
    runtime = Runtime()
    sink = Sink()
    context = ScholarWeaveContext(run_id="long-run", tool_runtime=runtime, event_sink=sink)
    definition = agent()
    working: list[dict[str, Any]] = []
    for turn in range(16):
        working.extend(research_round(turn))
        result = await prepare(settings, definition, working, context, turn_index=turn)
        for recent in range(max(0, turn - 1), turn + 1):
            assert all(item in result.items for item in research_round(recent))
        for prior in range(turn + 1):
            assert research_round(prior)[0] in result.items
            assert research_round(prior)[1] in result.items
        working = list(result.working_items)
    metrics = [payload for name, payload in sink.events if name == "context.prepared"]
    assert all(not entry["tool_payload_evictions"] for entry in metrics
               if entry["estimated_tokens_before"] <= int(77952 * ratio))
    assert len([entry for entry in metrics if entry["tool_payload_evictions"]]) == eviction_rounds
    assert all(not entry["compacted"] for entry in metrics)
    assert not any(name.startswith("context.compact") for name, _ in sink.events)
    assert all(entry["input_budget_tokens"] == 77952 for entry in metrics)
    assert not definition.binding.client.requests
    assert not runtime.bounded
    checkpoint = context.metadata["context_checkpoints"][-1]
    assert checkpoint["summary_method"] == "tool_eviction"
    for archive in checkpoint["history_archives"]:
        recovered = runtime.histories[archive["result_ref"]]
        assert research_round(0)[0] in recovered
    # The freed headroom spans multiple additional rounds, not one alternate turn.
    final_compacted = next(entry for entry in metrics if entry["tool_payload_evictions"])
    assert final_compacted["estimated_input_tokens"] < int(77952 * ratio * 0.8) + 1500
    eviction_indices = [index for index, entry in enumerate(metrics) if entry["tool_payload_evictions"]]
    assert all(right - left >= 3 for left, right in zip(eviction_indices, eviction_indices[1:]))
    assert settings.agent_context_high_water_ratio == ratio


@pytest.mark.anyio
async def test_explicit_high_water_ratio_is_honored_in_model_window_mode(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=80000, agent_context_high_water_ratio=0.5,
        agent_context_use_model_window=True,
    )
    sink = Sink()
    context = ScholarWeaveContext(run_id="run", tool_runtime=Runtime(), event_sink=sink)
    items = [item for index in range(9) for item in research_round(index)]
    await prepare(settings, agent(), items, context)
    assert any(name == "context.prepared" and payload["tool_payload_evictions"] > 0
               for name, payload in sink.events)


@pytest.mark.anyio
async def test_tiny_legacy_target_does_not_remove_recent_rounds(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_use_model_window=False, agent_working_context_tokens=12000,
        agent_context_compaction_target_tokens=1024,
    )
    runtime = Runtime()
    context = ScholarWeaveContext(run_id="run", tool_runtime=runtime)
    older = {"role": "assistant", "content": "Older narrative. " * 2400}
    recent = [*research_round(1, 4500), *research_round(2, 4500)]
    result = await prepare(settings, agent(), [older, *recent], context)
    assert result.items[-len(recent):] == recent
    assert result.items[0] == older
    assert not runtime.histories


@pytest.mark.anyio
async def test_summary_sees_exact_older_prefix_beyond_legacy_working_cap(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=80000, agent_context_use_model_window=True,
        agent_context_high_water_ratio=0.85,
    )
    definition = agent()
    runtime = Runtime()
    context = ScholarWeaveContext(run_id="run", tool_runtime=runtime)
    older = [
        {"role": "user", "content": "EXACT original instruction."},
        {"role": "assistant", "content": "a" * 220000 + "EXACT OLD PREFIX END"},
    ]
    recent = [*research_round(1, 25000), *research_round(2, 25000)]
    result = await prepare(settings, definition, [*older, *recent], context)
    request = definition.binding.client.requests[0]
    summary_input = json.loads(request["messages"][-1]["content"])
    assert summary_input["history"] == older
    assert result.items[-len(recent):] == recent
    assert runtime.histories["history-1"] == older
    assert not context.metadata["context_checkpoints"][0].get("summary_input_truncated")


@pytest.mark.anyio
async def test_archive_references_survive_repeated_compaction(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=16000, agent_context_use_model_window=True,
    )
    runtime = Runtime()
    context = ScholarWeaveContext(run_id="run", tool_runtime=runtime)
    working: list[dict[str, Any]] = []
    for turn in range(60):
        working.extend(research_round(turn, 6500))
        result = await prepare(settings, agent(), working, context, turn_index=turn)
        working = list(result.working_items)
        if context.metadata.get("context_checkpoints"):
            assert len(context.metadata["context_checkpoints"][-1]["history_archives"]) <= 8
    assert len(runtime.histories) >= 3
    checkpoint = context.metadata["context_checkpoints"][-1]
    assert any(entry.get("kind") == "archive_manifest" for entry in checkpoint["history_archives"])
    recovered_refs = set()

    def resolve(entries):
        for entry in entries:
            ref = entry["result_ref"]
            if ref in recovered_refs:
                continue
            recovered_refs.add(ref)
            if entry.get("kind") == "archive_manifest":
                resolve(runtime.histories[ref])

    resolve(checkpoint["history_archives"])
    assert recovered_refs == set(runtime.histories)
    assert all(entry["result_ref"] in json.dumps(result.items)
               for entry in checkpoint["history_archives"])
    assert all(
        item["_scholarweave_context_policy_version"] == 2
        for item in result.working_items if item.get("_scholarweave_context_checkpoint")
    )


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["missing", "raises", "invalid"])
async def test_history_is_not_discarded_when_archiving_unavailable(tmp_path, failure) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=8000,
    )
    runtime = Runtime()
    if failure == "missing":
        runtime.store_context_history = None
    elif failure == "invalid":
        runtime.store_context_history = lambda *args: {}
    else:
        def fail(*args):
            raise OSError("Archive disk is unavailable")
        runtime.store_context_history = fail
    context = ScholarWeaveContext(run_id="run", tool_runtime=runtime)
    items = [{"role": "assistant", "content": "Exact history " * 4000}, *recent_rounds()]
    original = json.loads(json.dumps(items))
    error_type = {"missing": RunPolicyViolation, "raises": OSError, "invalid": HarnessError}[failure]
    with pytest.raises(error_type, match="budget|storage|Archive"):
        await prepare(settings, agent(), items, context)
    assert items == original
    assert not context.metadata.get("context_checkpoints")


@pytest.mark.anyio
async def test_unfit_recent_tool_payload_is_archived_without_discarding_its_round(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=8000,
    )
    runtime = Runtime()
    sink = Sink()
    context = ScholarWeaveContext(run_id="run", tool_runtime=runtime, event_sink=sink)
    items = research_round(0, 40000)
    original = json.loads(json.dumps(items))
    result = await prepare(settings, agent(), items, context)
    assert result.items[:-1] == items[:-1]
    receipt = json.loads(result.items[-1]["output"])
    assert runtime.histories[receipt["result_ref"]] == [items[-1]]
    assert receipt["preview"]
    assert items == original
    metrics = [payload for name, payload in sink.events if name == "context.prepared"][-1]
    assert metrics["estimated_input_tokens"] + metrics["response_headroom_tokens"] <= 8000
    assert metrics["tool_payload_evictions"] == 1
    assert any(name == "tool.result.stored" for name, _ in sink.events)
    assert not runtime.bounded


@pytest.mark.anyio
async def test_missing_archive_helper_keeps_history_within_hard_limit(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=16000, agent_context_high_water_ratio=0.5,
    )
    runtime = Runtime()
    runtime.store_context_history = None
    sink = Sink()
    context = ScholarWeaveContext(run_id="run", tool_runtime=runtime, event_sink=sink)
    items = [{"role": "assistant", "content": "Older " * 5000}, *recent_rounds()]
    result = await prepare(settings, agent(), items, context)
    assert result.items == items
    assert not context.metadata.get("context_checkpoints")
    assert not next(payload for name, payload in sink.events if name == "context.prepared")["compacted"]


@pytest.mark.anyio
async def test_chat_completion_tool_rounds_remain_paired_and_recent_outputs_exact(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=16000,
    )
    runtime = Runtime()
    context = ScholarWeaveContext(run_id="run", tool_runtime=runtime)
    items: list[dict[str, Any]] = []
    for index in range(6):
        items.extend([
            {"role": "user", "content": f"Read exact evidence for round {index}."},
            {"role": "assistant", "content": f"Analysis {index}", "tool_calls": [
                {"id": f"call-{index}", "type": "function",
                 "function": {"name": "read_tool_result", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": f"call-{index}", "content": str(index) * 11000},
        ])
    result = await prepare(settings, agent(), items, context)
    assert result.items[-6:] == items[-6:]
    calls = {
        call["id"] for item in result.items for call in item.get("tool_calls", [])
    }
    outputs = {
        item["tool_call_id"] for item in result.items if item.get("role") == "tool"
    }
    assert calls == outputs == {f"call-{index}" for index in range(6)}
    assert context.metadata["context_checkpoints"][0]["summary_method"] == "tool_eviction"


@pytest.mark.anyio
@pytest.mark.parametrize(("model", "effort"), [
    ("gemma-4-27b", "none"), ("gpt-5-mini", "low"), ("unknown-model", None),
])
async def test_summary_generation_budget_is_not_tiny_checkpoint_allowance(tmp_path, model, effort) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=80000, agent_context_use_model_window=False,
        agent_context_high_water_ratio=0.7,
    )
    definition = agent()
    definition.binding = replace(
        definition.binding, model_name=model, provider_kind="openai_compatible",
    )
    definition.model_settings = ModelSettings(reasoning_effort="high")
    runtime = Runtime()
    context = ScholarWeaveContext(run_id="run", tool_runtime=runtime)
    first = {"role": "user", "content": "FIRST exact instruction — retain it!"}
    latest = {"role": "user", "content": "LATEST exact instruction — retain it too!"}
    items = [first, {"role": "assistant", "content": "o" * 22000},
             *recent_rounds(), latest]
    result = await prepare(settings, definition, items, context, instructions="i" * 210000)
    request = definition.binding.client.requests[0]
    assert request["max_tokens"] == 4096
    assert request.get("reasoning_effort") == effort
    assert first in result.items
    assert latest in result.items
    assert all(item in result.items for item in recent_rounds())
    assert context.metadata["context_checkpoints"][0]["summary_method"] == "model"


@pytest.mark.anyio
async def test_reasoning_only_summary_is_a_recorded_failure_not_completed_understanding(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=80000, agent_context_high_water_ratio=0.5,
    )

    async def create(**parameters):
        client.requests.append(parameters)
        return {"choices": [{"message": {
            "role": "assistant", "content": None, "reasoning_content": "Unfinished private reasoning",
        }, "finish_reason": "length"}]}

    client = FakeClient(create)
    runtime = Runtime()
    sink = Sink()
    context = ScholarWeaveContext(run_id="run", tool_runtime=runtime, event_sink=sink)
    older = {"role": "assistant", "content": "Exact older material. " * 8500}
    result = await prepare(settings, agent(client=client), [older, *recent_rounds()], context)
    checkpoint = context.metadata["context_checkpoints"][0]
    assert checkpoint["summary_method"] == "structured_fallback"
    assert checkpoint["summary_error"]["error_type"] == "ModelBehaviorError"
    assert "Reasoning-only" in checkpoint["summary_error"]["error"]
    assert "model_summary" not in checkpoint
    assert "Unfinished private reasoning" not in json.dumps(result.items)
    assert runtime.histories[checkpoint["history_archives"][0]["result_ref"]] == [older]
    assert any(name == "context.compaction_failed" for name, _ in sink.events)


@pytest.mark.anyio
async def test_reasoning_and_tool_output_belong_to_the_same_protected_model_round(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=16000,
    )
    context = ScholarWeaveContext(run_id="run", tool_runtime=Runtime())
    items = []
    for index in range(6):
        round_items = research_round(index, 11000)
        round_items.insert(1, {
            "type": "reasoning", "content": [{"type": "reasoning_text", "text": f"Reasoning {index}"}],
        })
        items.extend(round_items)
    result = await prepare(settings, agent(), items, context)
    assert result.items[-10:] == items[-10:]
    assert context.metadata["context_checkpoints"][0]["summary_method"] == "tool_eviction"


@pytest.mark.anyio
@pytest.mark.parametrize("separate_index", [False, True])
async def test_archive_index_supports_targeted_exact_output_recovery(tmp_path, separate_index) -> None:
    class IndexedRuntime(Runtime):
        def __init__(self):
            super().__init__()
            self.texts = {}

        def store_context_history(self, items, context):
            stored = super().store_context_history(items, context)
            serialized = [json.dumps(item, ensure_ascii=False) for item in items]
            self.texts[stored["result_ref"]] = "[" + ",\n".join(serialized) + "]"
            offset = 1
            index = []
            for position, text in enumerate(serialized):
                index.append({"item": position, "offset": offset, "length": len(text)})
                offset += len(text) + 2
            if separate_index:
                stored["index_ref"] = stored["result_ref"] + "-index"
                self.texts[stored["index_ref"]] = json.dumps(index)
            else:
                stored["index"] = index
            return stored

        async def invoke(self, catalog_id, arguments, context):
            assert catalog_id == "read_tool_result"
            text = self.texts[arguments["result_ref"]]
            offset = arguments.get("offset", 0)
            return {"content": text[offset:offset + arguments.get("limit", len(text))]}

    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=16000,
    )
    runtime = IndexedRuntime()
    context = ScholarWeaveContext(run_id="run", tool_runtime=runtime)
    items = [item for index in range(6) for item in research_round(index, 11000)]
    result = await prepare(settings, agent(), items, context)
    recovered_count = 0
    for item in result.items:
        if item.get("type") != "function_call_output":
            continue
        receipt = json.loads(item["output"])
        if receipt.get("status") != "archived_context_tool_output":
            continue
        if separate_index:
            raw_index = await runtime.invoke("read_tool_result", {
                "result_ref": receipt["index_ref"],
            }, context)
            location = next(entry for entry in json.loads(raw_index["content"])
                            if entry["item"] == receipt["history_item_index"])
        else:
            location = receipt
        recovered = await runtime.invoke("read_tool_result", {
            "result_ref": receipt["result_ref"], "offset": location["offset"],
            "limit": location["length"],
        }, context)
        assert json.loads(recovered["content"]) in items
        recovered_count += 1
    assert recovered_count >= 1


@pytest.mark.anyio
async def test_older_user_turns_can_be_archived_only_when_immutable_history_exceeds_window(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=16000,
    )
    runtime = Runtime()
    context = ScholarWeaveContext(run_id="run", tool_runtime=runtime)
    first = {"role": "user", "content": "FIRST objective: preserve evidence precisely."}
    system = {"role": "developer", "content": "Never invent missing evidence."}
    short_constraint = {"role": "user", "content": "Always retain exact citations."}
    items = [system, first, {"role": "assistant", "content": "Starting."},
             short_constraint, {"role": "assistant", "content": "Acknowledged."}]
    older_users = []
    for index in range(3):
        user = {"role": "user", "content": f"Older supplied research {index}: " + "x" * 24000}
        older_users.append(user)
        items.extend([user, {"role": "assistant", "content": f"Completed older turn {index}."}])
    recent = [*research_round(8, 100), *research_round(9, 100)]
    items.extend(recent)
    result = await prepare(settings, agent(), items, context)
    assert all(item in result.items for item in [system, first, short_constraint, *recent])
    missing = [item for item in older_users if item not in result.items]
    assert missing
    for item in missing:
        assert any(item in history for history in runtime.histories.values())
    checkpoint = context.metadata["context_checkpoints"][-1]
    assert any(archive.get("contains_user_instructions") for archive in checkpoint["history_archives"])
    assert "absence from the checkpoint does not revoke a constraint" in result.instructions


@pytest.mark.anyio
async def test_hard_pressure_pages_tool_payloads_before_archiving_user_constraints(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=16000,
    )
    runtime = Runtime()
    context = ScholarWeaveContext(run_id="run", tool_runtime=runtime)
    first = {"role": "user", "content": "Preserve the original objective."}
    older = {"role": "user", "content": "Older research material: " + "x" * 36000}
    recent = [*research_round(2, 11000), *research_round(3, 11000)]
    items = [
        first, {"role": "assistant", "content": "Starting."},
        older, {"role": "assistant", "content": "Earlier task completed."},
        *recent,
    ]
    result = await prepare(settings, agent(), items, context)
    assert first in result.items
    assert older in result.items
    for item in recent:
        if item.get("type") == "function_call_output" and item not in result.items:
            assert any(item in history for history in runtime.histories.values())
        else:
            assert item in result.items
    assert runtime.histories


@pytest.mark.anyio
async def test_older_user_instructions_remain_verbatim_without_hard_limit_pressure(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=16000,
    )
    context = ScholarWeaveContext(run_id="run", tool_runtime=Runtime())
    first = {"role": "user", "content": "FIRST objective"}
    earlier = {"role": "user", "content": "Exact persistent instruction " * 600}
    items = [first, {"role": "assistant", "content": "Acknowledged."}, earlier,
             {"role": "assistant", "content": "Old discussion " * 3200}, *recent_rounds()]
    result = await prepare(settings, agent(), items, context)
    assert first in result.items
    assert earlier in result.items
    assert not any(
        archive.get("contains_user_instructions")
        for archive in context.metadata["context_checkpoints"][-1]["history_archives"]
    )


@pytest.mark.anyio
async def test_policy_archive_is_readable_by_real_runtime_after_run_cleanup(test_settings) -> None:
    from backend.bootstrap import create_services

    settings = test_settings.model_copy(update={"agent_context_window_tokens": 16000})
    services = create_services(settings)
    runtime = services.runs._tool_runtime
    run = services.runs._repository.create(
        conversation_id="policy-archive", agent_name="Worker", input_value="Research", blueprint={},
    )
    context = ScholarWeaveContext(run.id, runtime, conversation_id="policy-archive")
    items = [item for index in range(6) for item in research_round(index, 11000)]
    try:
        result = await prepare(settings, agent(), items, context)
        assert result.items[-8:] == items[-8:]
        checkpoint = context.metadata["context_checkpoints"][-1]
        assert "conversations/ survive run cleanup" in checkpoint["reference_lifetime"]
        assert "Legacy or standalone references under runs/" in checkpoint["reference_lifetime"]
        assert all(archive["result_ref"].startswith("conversations/policy-archive/")
                   for archive in checkpoint["history_archives"])
        assert "until their conversation is deleted" in result.instructions
        assert services.runs.clear_history() == 1
        later = ScholarWeaveContext("later-run", runtime, conversation_id="policy-archive")
        recovered_count = 0
        for item in result.items:
            if item.get("type") != "function_call_output":
                continue
            receipt = json.loads(item["output"])
            if receipt.get("status") != "archived_context_tool_output":
                continue
            location = receipt
            if "index_ref" in receipt:
                index_page = runtime._read_tool_result({
                    "result_ref": receipt["index_ref"], "offset": 0, "limit": 16000,
                }, later)
                location = next(entry for entry in json.loads(index_page["content"])
                                if entry["item"] == receipt["history_item_index"])
            page = runtime._read_tool_result({
                "result_ref": receipt["result_ref"], "offset": location["offset"],
                "limit": location["length"],
            }, later)
            assert json.loads(page["content"]) in items
            recovered_count += 1
        assert recovered_count > 0
    finally:
        await services.close()


@pytest.mark.anyio
@pytest.mark.parametrize("disabled", [False, True])
@pytest.mark.parametrize("fits", [False, True])
async def test_agent_without_enabled_archive_reader_retains_history_or_fails(tmp_path, disabled, fits) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=8000,
    )
    definition = agent()
    if disabled:
        definition.tools = [replace(definition.tools[0], is_enabled=lambda context: False)]
    else:
        definition.tools = []
    runtime = Runtime()
    sink = Sink()
    context = ScholarWeaveContext(run_id="run", tool_runtime=runtime, event_sink=sink)
    items = [{"role": "assistant", "content": "o" * (21000 if fits else 28000)},
             *recent_rounds()]
    if fits:
        result = await prepare(settings, definition, items, context)
        assert result.items == items
    else:
        with pytest.raises(RunPolicyViolation):
            await prepare(settings, definition, items, context)
    assert not runtime.histories
    assert not definition.binding.client.requests
    assert not any(name.startswith("context.compact") for name, _ in sink.events)


@pytest.mark.anyio
async def test_archive_manifest_failure_does_not_adopt_a_partial_checkpoint(tmp_path) -> None:
    class FailingManifestRuntime(Runtime):
        def store_context_history(self, items, context):
            if items and all("result_ref" in item for item in items):
                raise OSError("Archive manifest storage failed")
            return super().store_context_history(items, context)

    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=16000,
    )
    runtime = FailingManifestRuntime()
    context = ScholarWeaveContext(run_id="run", tool_runtime=runtime)
    working = []
    for turn in range(60):
        working.extend(research_round(turn, 6500))
        original = json.loads(json.dumps(working))
        checkpoint_count = len(context.metadata.get("context_checkpoints", []))
        try:
            result = await prepare(settings, agent(), working, context)
        except OSError as error:
            assert str(error) == "Archive manifest storage failed"
            assert working == original
            assert len(context.metadata["context_checkpoints"]) == checkpoint_count
            break
        working = list(result.working_items)
    else:
        pytest.fail("Long-running history never exercised bounded archive manifests")


@pytest.mark.anyio
@pytest.mark.parametrize(("model", "effort"), [
    ("gemma-4-12b", "none"), ("gemma4-12b", None), ("unknown-helper", None),
])
async def test_optional_helper_uses_its_own_budget_and_reasoning(tmp_path, model, effort) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=16000, agent_context_use_model_window=False,
        agent_context_compaction_target_tokens=1024,
        agent_context_high_water_ratio=0.5,
    )
    main = agent()
    main.model_settings = ModelSettings(reasoning_effort="high", temperature=0.9)
    helper_client = summarizing_client("Helper preserved the findings.")
    helper = replace(main.binding, client=helper_client, model_name=model,
                     provider_kind="openai_compatible", context_window_tokens=16000)
    runtime = Runtime()
    sink = Sink()
    context = ScholarWeaveContext(run_id="helper", tool_runtime=runtime, event_sink=sink)
    older = {"role": "assistant", "content": "x" * 30000}
    original = [older, *recent_rounds()]
    result = await prepare(settings, main, original, context, compaction_model=helper)
    assert not main.binding.client.requests
    request = helper_client.requests[0]
    assert request["model"] == model
    assert request["max_tokens"] == 1024
    assert request.get("reasoning_effort") == effort
    assert "temperature" not in request
    assert "tools" not in request
    assert json.loads(request["messages"][-1]["content"])["history"] == [older]
    checkpoint = context.metadata["context_checkpoints"][0]
    assert checkpoint["summary_model_name"] == model
    assert checkpoint["summary_model_role"] == "helper"
    assert checkpoint["summary_context_window_tokens"] == 16000
    assert checkpoint["summary_method"] == "model"
    assert runtime.histories[checkpoint["history_archives"][0]["result_ref"]] == [older]
    assert original == [older, *recent_rounds()]
    assert result.items[-2:] == recent_rounds()
    completed = next(payload for name, payload in sink.events if name == "context.compacted")
    assert completed["model_name"] == model


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["error", "empty", "truncated"])
async def test_helper_failure_retries_main_with_exact_history(tmp_path, failure) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=16000, agent_context_use_model_window=False,
        agent_context_compaction_target_tokens=1024,
        agent_context_high_water_ratio=0.5,
    )

    async def create(**parameters):
        helper_client.requests.append(parameters)
        if failure == "error":
            raise HarnessError("Helper unavailable")
        return {"choices": [{"message": {"role": "assistant",
                                        "content": "Partial summary" if failure == "truncated" else ""},
                             "finish_reason": "length" if failure == "truncated" else "stop"}]}

    main = agent()
    helper_client = FakeClient(create)
    helper = replace(main.binding, client=helper_client, model_name="gemma4-12b",
                     context_window_tokens=16000)
    runtime = Runtime()
    sink = Sink()
    context = ScholarWeaveContext(run_id="helper", tool_runtime=runtime, event_sink=sink)
    older = {"role": "assistant", "content": "x" * 30000}
    result = await prepare(
        settings, main, [older, *recent_rounds()], context, compaction_model=helper,
    )
    assert len(helper_client.requests) == len(main.binding.client.requests) == 1
    for request in [helper_client.requests[0], main.binding.client.requests[0]]:
        assert json.loads(request["messages"][-1]["content"])["history"] == [older]
    checkpoint = context.metadata["context_checkpoints"][0]
    assert checkpoint["summary_method"] == "model"
    assert checkpoint["summary_model_role"] == "main"
    assert [attempt["status"] for attempt in checkpoint["summary_attempts"]] == ["failed", "completed"]
    assert "summary_error" not in checkpoint
    assert "Partial summary" not in checkpoint["model_summary"]
    failure_event = next(payload for name, payload in sink.events if name == "context.compaction_failed")
    assert failure_event["model_name"] == "gemma4-12b"
    assert failure_event["retrying_with_main_model"] is True
    assert failure_event["fallback_model"] == main.binding.model_name
    retry_event = [payload for name, payload in sink.events if name == "context.compaction_started"][-1]
    assert retry_event["is_fallback"] is True
    assert runtime.histories[checkpoint["history_archives"][0]["result_ref"]] == [older]
    assert result.items[-2:] == recent_rounds()


@pytest.mark.anyio
@pytest.mark.parametrize("helper_window", [16000, None])
async def test_small_helper_never_inherits_main_window_or_receives_oversized_history(
    tmp_path, helper_window,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=80000, agent_context_high_water_ratio=0.5,
    )
    main = agent()
    helper_client = summarizing_client()
    helper = replace(main.binding, client=helper_client, model_name="gemma4-12b",
                     context_window_tokens=helper_window)
    runtime = Runtime()
    sink = Sink()
    context = ScholarWeaveContext(run_id="helper", tool_runtime=runtime, event_sink=sink)
    older = {"role": "assistant", "content": "x" * 180000}
    result = await prepare(
        settings, main, [older, *recent_rounds()], context, compaction_model=helper,
        context_window_tokens_by_agent={main.id: 80000},
    )
    assert not helper_client.requests
    request = main.binding.client.requests[0]
    assert json.loads(request["messages"][-1]["content"])["history"] == [older]
    failure = next(payload for name, payload in sink.events if name == "context.compaction_failed")
    assert failure["summary_context_window_tokens"] == helper_window
    assert failure["retrying_with_main_model"] is True
    assert "No sampled summary" in failure["error"]
    checkpoint = context.metadata["context_checkpoints"][0]
    assert checkpoint["summary_method"] == "model"
    assert not checkpoint.get("summary_input_truncated")
    assert runtime.histories[checkpoint["history_archives"][0]["result_ref"]] == [older]
    assert result.items[-2:] == recent_rounds()


@pytest.mark.anyio
async def test_helper_and_main_failure_keep_structured_checkpoint_and_exact_archive(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=16000, agent_context_use_model_window=False,
        agent_context_compaction_target_tokens=1024,
        agent_context_high_water_ratio=0.5,
    )
    main = agent(client=summarizing_client(""))
    helper = replace(main.binding, client=summarizing_client(""), model_name="gemma4-12b",
                     context_window_tokens=16000)
    runtime = Runtime()
    sink = Sink()
    context = ScholarWeaveContext(run_id="helper", tool_runtime=runtime, event_sink=sink)
    older = {"role": "assistant", "content": "x" * 30000}
    await prepare(settings, main, [older, *recent_rounds()], context, compaction_model=helper)
    checkpoint = context.metadata["context_checkpoints"][0]
    assert checkpoint["summary_method"] == "structured_fallback"
    assert "model_summary" not in checkpoint
    assert "Bounded excerpts" in checkpoint["summary_limitations"]
    assert runtime.histories[checkpoint["history_archives"][0]["result_ref"]] == [older]
    failures = [payload for name, payload in sink.events if name == "context.compaction_failed"]
    assert [failure["retrying_with_main_model"] for failure in failures] == [True, False]


@pytest.mark.anyio
async def test_unresolvable_helper_reports_main_fallback_before_summarizing(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=16000, agent_context_use_model_window=False,
        agent_context_compaction_target_tokens=1024,
        agent_context_high_water_ratio=0.5,
    )
    main = agent()
    sink = Sink()
    context = ScholarWeaveContext(run_id="helper", tool_runtime=Runtime(), event_sink=sink)
    await prepare(
        settings, main,
        [{"role": "assistant", "content": "x" * 30000}, *recent_rounds()], context,
        compaction_model_error={
            "error_type": "ProviderRuntimeError", "error": "Provider profile was not found.",
            "model_name": "gemma4-12b", "provider_profile_id": "missing",
        },
    )
    assert len(main.binding.client.requests) == 1
    failure = next(payload for name, payload in sink.events if name == "context.compaction_failed")
    assert failure["model_name"] == "gemma4-12b"
    assert failure["retrying_with_main_model"] is True
    assert context.metadata["context_checkpoints"][0]["summary_method"] == "model"


@pytest.mark.anyio
async def test_helper_hot_swap_reuses_official_client_scheduler_without_nested_leases(
    test_settings, stub_provider, monkeypatch,
) -> None:
    import anyio
    import httpx
    from openai import AsyncOpenAI

    from backend.providers.inference import InferenceScheduler, _priority, inference_priority
    from backend.providers.logging import ScheduledTransport

    scheduler = InferenceScheduler()
    scheduler.configure("local", serialize_model_switches=True)
    priorities = []
    original_acquire = scheduler._acquire_request

    async def acquire(profile_id, model, priority):
        priorities.append((model, _priority.get()))
        return await original_acquire(profile_id, model, priority)

    monkeypatch.setattr(scheduler, "_acquire_request", acquire)
    monkeypatch.setattr(
        stub_provider, "_reply_for",
        lambda payload: "" if payload["model"] == "gemma4-12b" else "Main summary.",
    )
    client = AsyncOpenAI(
        api_key="test", base_url=f"{stub_provider.base_url}/v1", max_retries=0,
        http_client=httpx.AsyncClient(transport=ScheduledTransport(
            httpx.AsyncHTTPTransport(), scheduler, "local",
        )),
    )
    main = agent()
    main.binding = replace(main.binding, client=client, model_name="main",
                           context_window_tokens=16000)
    helper = replace(main.binding, model_name="gemma4-12b", context_window_tokens=16000)
    settings = test_settings.model_copy(update={
        "agent_context_use_model_window": False, "agent_context_high_water_ratio": 0.5,
    })
    context = ScholarWeaveContext(run_id="hot-swap", tool_runtime=Runtime())
    try:
        with anyio.fail_after(5), inference_priority("interactive"):
            await prepare(
                settings, main,
                [{"role": "assistant", "content": "x" * 30000}, *recent_rounds()],
                context, compaction_model=helper,
            )
            assert _priority.get() == "interactive"
            await client.chat.completions.create(
                model="main", messages=[{"role": "user", "content": "Continue"}],
            )
    finally:
        await client.close()
    assert [request["model"] for request in stub_provider.requests] == ["gemma4-12b", "main", "main"]
    assert priorities == [("gemma4-12b", "background"), ("main", "background"), ("main", "interactive")]
    assert scheduler.snapshot()["active_requests"] == 0
    assert scheduler.snapshot()["queue"] == []
    assert context.metadata["context_checkpoints"][0]["summary_model_role"] == "main"


@pytest.mark.anyio
@pytest.mark.parametrize("window", [4096, 16000, 80000])
@pytest.mark.parametrize("manual_budget", [False, True])
async def test_retry_response_budget_uses_available_context_without_changing_initial_cap(
    tmp_path, window, manual_budget,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=window,
        agent_context_use_model_window=not manual_budget,
        agent_context_compaction_target_tokens=1024,
    )
    main = agent()
    sink = Sink()
    context = ScholarWeaveContext(run_id="retry-budget", tool_runtime=Runtime(), event_sink=sink)
    user = {"role": "user", "content": "Keep these exact constraints. " * 15}
    original = [user, *recent_rounds()]
    result = await prepare(settings, main, original, context)
    metrics = [payload for name, payload in sink.events if name == "context.prepared"][-1]
    expected = window - metrics["estimated_input_tokens"] - 256
    assert result.response_retry_max_tokens == expected
    assert result.response_max_tokens == settings.agent_context_response_reserve_tokens
    retry_agent = replace(main, model_settings=replace(main.model_settings, max_tokens=expected))
    retried = await prepare(settings, retry_agent, list(result.working_items), context)
    assert 0 < retried.response_max_tokens <= expected
    assert retried.estimated_input_tokens + retried.response_max_tokens + 256 <= window
    assert retried.items == original
    assert not context.tool_runtime.histories
    assert settings.agent_context_response_reserve_tokens == 2048


@pytest.mark.anyio
async def test_retry_response_budget_leaves_large_protected_content_intact(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=8000,
    )
    main = agent()
    sink = Sink()
    context = ScholarWeaveContext(run_id="retry-budget", tool_runtime=Runtime(), event_sink=sink)
    user = {"role": "user", "content": "u" * 15000}
    original = [user, {"role": "assistant", "content": "Old note."}, *recent_rounds()]
    result = await prepare(settings, main, original, context)
    assert result.response_retry_max_tokens > result.response_max_tokens
    retry_agent = replace(main, model_settings=replace(
        main.model_settings, max_tokens=result.response_retry_max_tokens,
    ))
    retried = await prepare(settings, retry_agent, list(result.working_items), context)
    assert user in retried.items
    assert retried.items[-2:] == recent_rounds()


@pytest.mark.anyio
@pytest.mark.parametrize("wire_format", ["native", "chat"])
@pytest.mark.parametrize("tool_count", [1, 3])
async def test_context_pressure_pages_parallel_latest_outputs_without_orphaning_calls(
    tmp_path, wire_format, tool_count,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=8000, tool_result_max_tokens=512,
    )
    runtime = Runtime()
    sink = Sink()
    context = ScholarWeaveContext(run_id="parallel", tool_runtime=runtime, event_sink=sink)
    calls = [
        {"id": f"call-{index}", "type": "function",
         "function": {"name": "read", "arguments": "{}"}}
        for index in range(tool_count)
    ]
    if wire_format == "native":
        items = [
            {"type": "function_call", "call_id": call["id"], "name": "read", "arguments": "{}"}
            for call in calls
        ] + [
            {"type": "function_call_output", "call_id": call["id"], "output": "x" * 60000}
            for call in calls
        ]
    else:
        items = [{"role": "assistant", "tool_calls": calls}] + [
            {"role": "tool", "tool_call_id": call["id"], "content": "x" * 60000}
            for call in calls
        ]
    original = json.loads(json.dumps(items))
    result = await prepare(settings, agent(), items, context)
    assert len(result.items) == len(items)
    for prior, current in zip(items, result.items):
        field = "output" if prior.get("type") == "function_call_output" else "content"
        if prior.get("type") != "function_call_output" and prior.get("role") != "tool":
            assert current == prior
            continue
        receipt = json.loads(current[field])
        assert runtime.histories[receipt["result_ref"]] == [prior]
        assert current.get("call_id") == prior.get("call_id")
        assert current.get("tool_call_id") == prior.get("tool_call_id")
    assert items == original
    metrics = [payload for name, payload in sink.events if name == "context.prepared"][-1]
    assert metrics["estimated_input_tokens"] + metrics["response_headroom_tokens"] <= 8000
    assert metrics["tool_payload_evictions"] == tool_count


@pytest.mark.anyio
async def test_tool_preview_budget_tracks_only_actual_remaining_model_context(tmp_path) -> None:
    preview_lengths = {}
    for window, user_size in [(8000, 1000), (16000, 1000), (16000, 10000)]:
        settings = Settings(
            data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
            agent_context_window_tokens=window, tool_result_max_tokens=512,
        )
        context = ScholarWeaveContext(run_id="remaining", tool_runtime=Runtime())
        items = [
            {"role": "user", "content": "u" * user_size},
            {"type": "function_call", "call_id": "read", "name": "read", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "read", "output": "x" * 100000},
        ]
        result = await prepare(settings, agent(), items, context)
        preview_lengths[window, user_size] = len(json.loads(result.items[-1]["output"])["preview"])
        assert result.items[:-1] == items[:-1]
    assert preview_lengths[8000, 1000] > 512 * 4
    assert preview_lengths[16000, 1000] > preview_lengths[8000, 1000]
    assert preview_lengths[16000, 10000] < preview_lengths[16000, 1000]


@pytest.mark.anyio
async def test_latest_oversized_tool_output_is_readable_from_real_runtime(test_settings) -> None:
    from backend.bootstrap import create_services

    settings = test_settings.model_copy(update={"agent_context_window_tokens": 8000})
    services = create_services(settings)
    runtime = services.runs._tool_runtime
    context = ScholarWeaveContext("latest-run", runtime, conversation_id="latest-window")
    items = research_round(0, 40000)
    try:
        result = await prepare(settings, agent(), items, context)
        receipt = json.loads(result.items[-1]["output"])
        assert receipt["result_ref"].startswith("conversations/latest-window/")
        recovered = runtime._read_tool_result({
            "result_ref": receipt["result_ref"], "offset": receipt["offset"],
            "limit": receipt["length"],
        }, context)
        assert json.loads(recovered["content"]) == items[-1]
    finally:
        await services.close()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["missing", "raises", "invalid"])
async def test_latest_output_is_not_changed_without_a_readable_archive(tmp_path, failure) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=8000,
    )
    runtime = Runtime()
    if failure == "missing":
        runtime.store_context_history = None
    elif failure == "invalid":
        runtime.store_context_history = lambda *args: {}
    else:
        def fail(*args):
            raise OSError("Archive unavailable")
        runtime.store_context_history = fail
    context = ScholarWeaveContext(run_id="archive-failure", tool_runtime=runtime)
    items = research_round(0, 40000)
    original = json.loads(json.dumps(items))
    error_type = {"missing": RunPolicyViolation, "raises": OSError, "invalid": HarnessError}[failure]
    with pytest.raises(error_type):
        await prepare(settings, agent(), items, context)
    assert items == original


@pytest.mark.anyio
async def test_larger_helper_can_summarize_main_prefix_without_legacy_input_cap(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=80000, agent_context_high_water_ratio=0.5,
        agent_context_use_model_window=False, agent_working_context_tokens=12000,
    )
    main = agent()
    helper_client = summarizing_client()
    helper = replace(main.binding, model_name="gemma4-12b", context_window_tokens=131072,
                     client=helper_client)
    context = ScholarWeaveContext(run_id="larger-helper", tool_runtime=Runtime())
    older = {"role": "assistant", "content": "x" * 180000}
    await prepare(settings, main, [older, *recent_rounds()], context, compaction_model=helper)
    assert not main.binding.client.requests
    request = helper_client.requests[0]
    assert json.loads(request["messages"][-1]["content"])["history"] == [older]
    assert context.metadata["context_checkpoints"][0]["summary_context_window_tokens"] == 131072


@pytest.mark.anyio
@pytest.mark.parametrize("source_character", ["x", "𝛼"])
@pytest.mark.parametrize("archive_reader", [False, True])
async def test_oversized_pending_paper_batch_is_resized_with_matching_coverage(
    tmp_path, source_character, archive_reader,
) -> None:
    class ResizingRuntime(Runtime):
        def __init__(self):
            super().__init__()
            self.budgets = []
            self.pending = None

        def constrain_paper_summary_batch(self, result, context, *, max_chars):
            assert self.histories
            if self.pending is not None:
                assert result["batch_id"] == self.pending["batch_id"]
            self.budgets.append(max_chars)
            text = result["content"][:max_chars]
            self.pending = {
                **result, "content": text, "coverage": {"start": 0, "end": len(text)},
                "batch_id": f"resized-{len(self.budgets)}",
            }
            return self.pending

    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=8000,
    )
    runtime = ResizingRuntime()
    sink = Sink()
    context = ScholarWeaveContext(run_id="paper-budget", tool_runtime=runtime, event_sink=sink)
    items = _pending_paper_batch("paper-1", 1, source_character * 100000)
    original = json.loads(json.dumps(items))
    definition = agent()
    if not archive_reader:
        definition.tools = []
    result = await prepare(settings, definition, items, context)
    output = result.items[-1]["output"]
    assert result.items[0] == items[0]
    assert runtime.budgets
    assert not runtime.unavailable_batches
    assert 0 < len(output["content"]) < len(items[-1]["output"]["content"])
    assert output["coverage"] == runtime.pending["coverage"]
    assert output["coverage"]["end"] == len(output["content"])
    assert output["checkpoint_required"] is True
    assert runtime.histories[output["context_archive_ref"]] == [items[-1]]
    assert items == original
    metrics = [payload for name, payload in sink.events if name == "context.prepared"][-1]
    assert metrics["estimated_input_tokens"] + metrics["response_headroom_tokens"] <= 8000


@pytest.mark.anyio
@pytest.mark.parametrize("resize_result", ["none", "raises", "still_oversized", "changed_identity"])
async def test_archive_only_paper_batch_is_marked_unavailable_before_model_input(
    tmp_path, resize_result,
) -> None:
    class UnfitRuntime(Runtime):
        def constrain_paper_summary_batch(self, result, context, *, max_chars):
            if resize_result == "raises":
                raise ValueError("Source metadata cannot fit.")
            if resize_result == "changed_identity":
                return {**result, "batch_id": "resized"}
            return result if resize_result == "still_oversized" else None

    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=8000,
    )
    runtime = UnfitRuntime()
    context = ScholarWeaveContext(run_id="paper-budget", tool_runtime=runtime)
    items = _pending_paper_batch("paper-1", 1, "x" * 100000)
    result = await prepare(settings, agent(), items, context)
    receipt = result.items[-1]["output"]
    expected_pending = dict(items[-1]["output"])
    if resize_result == "changed_identity":
        expected_pending["batch_id"] = "resized"
    assert runtime.unavailable_batches == [expected_pending]
    assert receipt["checkpoint_available"] is False
    assert "not checkpointable" in receipt["instruction"]
    assert runtime.histories[receipt["result_ref"]] == [items[-1]]


@pytest.mark.anyio
@pytest.mark.parametrize(("model", "declared", "expected"), [
    ("gemma4-12b", ("none", "high"), "none"),
    ("gemma4-12b", ("high", "low", "medium"), "low"),
    ("gemma-4-12b", (), None),
    ("unknown-alias", None, None),
])
async def test_compaction_uses_declared_reasoning_before_alias_inference(
    tmp_path, model, declared, expected,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=16000, agent_context_high_water_ratio=0.5,
    )
    main = agent()
    main.model_settings = ModelSettings(reasoning_effort="high")
    helper_client = summarizing_client()
    helper = replace(
        main.binding, client=helper_client, model_name=model, provider_kind="openai_compatible",
        context_window_tokens=65536, reasoning_efforts=declared,
    )
    context = ScholarWeaveContext(run_id="declared-efforts", tool_runtime=Runtime())
    await prepare(
        settings, main, [{"role": "assistant", "content": "x" * 30000}, *recent_rounds()],
        context, compaction_model=helper,
    )
    assert helper_client.requests[0].get("reasoning_effort") == expected
    assert not main.binding.client.requests


@pytest.mark.anyio
async def test_token_calibration_scales_input_and_retry_room_per_agent(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=80000,
    )
    context = ScholarWeaveContext(run_id="calibration", tool_runtime=Runtime())
    definition = agent()
    items = [{"role": "user", "content": "Preserve this exact request."}]
    initial = await prepare(settings, definition, items, context)
    context.metadata["_context_token_ratios"] = {definition.id: 1.75, "other-agent": 10}
    calibrated = await prepare(settings, definition, items, context)
    assert calibrated.estimated_input_tokens == math.ceil(initial.estimated_input_tokens * 1.75)
    assert calibrated.context_window_tokens == 80000
    assert calibrated.response_retry_max_tokens == 80000 - calibrated.estimated_input_tokens - 256
    assert calibrated.items == initial.items == items
    other = await prepare(settings, agent(agent_id="uncalibrated"), items, context)
    assert other.estimated_input_tokens == initial.estimated_input_tokens


@pytest.mark.anyio
@pytest.mark.parametrize("ratio", [0, 0.5, "2", None, True, float("nan"), float("inf")])
async def test_invalid_or_downward_token_calibration_cannot_weaken_budget(tmp_path, ratio) -> None:
    settings = Settings(data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace")
    definition = agent()
    context = ScholarWeaveContext(run_id="calibration", tool_runtime=Runtime())
    items = [{"role": "user", "content": "An exact request."}]
    initial = await prepare(settings, definition, items, context)
    context.metadata["_context_token_ratios"] = {definition.id: ratio}
    calibrated = await prepare(settings, definition, items, context)
    assert calibrated.estimated_input_tokens == initial.estimated_input_tokens


@pytest.mark.anyio
async def test_increased_token_ratio_reprepares_and_archives_without_reexecuting_tools(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=16000,
    )
    definition = agent()
    runtime = Runtime()
    context = ScholarWeaveContext(run_id="calibration", tool_runtime=runtime)
    older = {"role": "assistant", "content": "x" * 20000}
    items = [older, *recent_rounds()]
    initial = await prepare(settings, definition, items, context)
    assert not runtime.histories
    context.metadata["_context_token_ratios"] = {definition.id: 2.5}
    calibrated = await prepare(settings, definition, list(initial.working_items), context)
    assert runtime.histories
    assert any(older in history for history in runtime.histories.values())
    assert calibrated.items[-2:] == recent_rounds()
    assert calibrated.estimated_input_tokens + calibrated.response_max_tokens <= 16000
    assert items == [older, *recent_rounds()]
    assert not runtime.bounded


@pytest.mark.anyio
async def test_calibrated_recent_tool_preview_stays_inside_physical_window(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=16000,
    )
    definition = agent()
    runtime = Runtime()
    context = ScholarWeaveContext(run_id="calibration", tool_runtime=runtime)
    items = research_round(0, 40000)
    initial = await prepare(settings, definition, items, context)
    assert initial.items == items
    context.metadata["_context_token_ratios"] = {definition.id: 2}
    calibrated = await prepare(settings, definition, list(initial.working_items), context)
    receipt = json.loads(calibrated.items[-1]["output"])
    assert runtime.histories[receipt["result_ref"]] == [items[-1]]
    assert calibrated.items[:-1] == items[:-1]
    assert calibrated.estimated_input_tokens + calibrated.response_max_tokens <= 16000


@pytest.mark.anyio
async def test_calibration_rejects_truly_unfit_protected_constraints(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=8000,
    )
    definition = agent()
    context = ScholarWeaveContext(run_id="calibration", tool_runtime=Runtime())
    items = [{"role": "user", "content": "x" * 10000}]
    await prepare(settings, definition, items, context)
    context.metadata["_context_token_ratios"] = {definition.id: 3}
    with pytest.raises(RunPolicyViolation):
        await prepare(settings, definition, items, context)
    assert not definition.binding.client.requests


@pytest.mark.anyio
@pytest.mark.parametrize("tool_output", [False, True])
async def test_learned_response_allowance_shrinks_before_discarding_grown_protected_input(
    tmp_path, tool_output,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=80000,
    )
    definition = agent()
    definition.model_settings = ModelSettings(max_tokens=65536)
    runtime = Runtime()
    context = ScholarWeaveContext(run_id="adaptive-output", tool_runtime=runtime)
    initial = await prepare(settings, definition, [{"role": "user", "content": "Research."}], context)
    assert initial.response_max_tokens == 65536
    if tool_output:
        grown = research_round(0, 160000)
    else:
        grown = [{"role": "user", "content": "u" * 160000}, *recent_rounds()]
    result = await prepare(settings, definition, grown, context)
    assert 2048 < result.response_max_tokens < 40000
    assert result.estimated_input_tokens + result.response_max_tokens + 256 <= 80000
    assert result.items == grown
    assert not runtime.histories
    assert definition.model_settings.max_tokens == 65536


@pytest.mark.anyio
async def test_unfit_fresh_tool_output_does_not_keep_a_stale_large_generation_reservation(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=80000,
    )
    definition = agent()
    definition.model_settings = ModelSettings(max_tokens=65536)
    runtime = Runtime()
    context = ScholarWeaveContext(run_id="adaptive-output", tool_runtime=runtime)
    original = research_round(0, 500000)
    result = await prepare(settings, definition, original, context)
    assert result.response_max_tokens == 2048
    receipt = json.loads(result.items[-1]["output"])
    assert len(receipt["preview"]) > 100000
    assert runtime.histories[receipt["result_ref"]] == [original[-1]]
    assert result.estimated_input_tokens + result.response_max_tokens <= 80000


@pytest.mark.anyio
async def test_explicit_response_maximum_is_an_upper_bound_not_required_window_space(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace")
    definition = agent()
    definition.model_settings = ModelSettings(max_tokens=40000)
    context = ScholarWeaveContext(run_id="adaptive-output", tool_runtime=Runtime())
    original = [{"role": "user", "content": "Exact constraint"}]
    result = await prepare(settings, definition, original, context)
    assert 0 < result.response_max_tokens < 40000
    assert result.estimated_input_tokens + result.response_max_tokens + 256 <= settings.agent_context_window_tokens
    assert result.items == original
    assert definition.model_settings.max_tokens == 40000


@pytest.mark.anyio
@pytest.mark.parametrize("archive_only", [False, True])
async def test_real_pending_paper_coverage_is_safe_through_actual_context_preparation(
    test_settings, monkeypatch, archive_only,
) -> None:
    from backend.bootstrap import create_services

    settings = test_settings.model_copy(update={"agent_context_window_tokens": 8000})
    services = create_services(settings)
    runtime = services.runs._tool_runtime
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF", filename="policy-source.pdf", title="Policy source",
    )
    services.documents.repository.mark_ready(document.id, page_count=1, metadata={})
    services.retrieval.replace_document_chunks(
        document.id, [{"text": "Measured result π.\n" * 6000, "citation": "p.1"}],
    )
    context = ScholarWeaveContext("policy-summary", runtime)
    try:
        full = await runtime._read_paper_summary_batch(
            {"document_id": document.id, "action": "chunks", "start": 0}, context,
        )
        items = [
            {"type": "function_call", "name": "read_paper_summary_batch", "call_id": "source",
             "arguments": json.dumps({"document_id": document.id, "action": "chunks", "start": 0})},
            {"type": "function_call_output", "call_id": "source", "output": full},
        ]
        original = json.loads(json.dumps(items[-1]))
        if archive_only:
            monkeypatch.setattr(runtime, "constrain_paper_summary_batch", lambda *args, **kwargs: None)
        prepared = await prepare(settings, agent(), items, context)
        visible = prepared.items[-1]["output"]
        pending = runtime._paper_summary_state(context, document.id)["pending_checkpoint"]
        assert prepared.estimated_input_tokens + prepared.response_max_tokens <= 8000
        if archive_only:
            assert visible["checkpoint_available"] is False
            assert pending["content_unavailable"] is True
            with pytest.raises(ValueError, match="archived"):
                await runtime._paper_summary_checkpoint(
                    {"document_id": document.id, "action": "append", "content": "Unseen claim [p.1]."},
                    context,
                )
            with pytest.raises(ValueError, match="archived"):
                runtime._save_paper_summary_version(
                    {"document_id": document.id, "content": "Unseen summary.", "review_summary": "Unseen."},
                    context,
                )
            archive_ref = visible["result_ref"]
        else:
            assert visible["has_more"] is True
            assert pending["id"] == visible["batch_id"]
            assert pending["coverage"] == visible["coverage"]
            appended = await runtime._paper_summary_checkpoint(
                {"document_id": document.id, "action": "append", "content": "Visible findings [p.1]."},
                context,
            )
            assert appended["complete"] is False
            archive_ref = visible["context_archive_ref"]
        recovered = runtime._read_tool_result(
            {"result_ref": archive_ref, "offset": 0, "limit": len(json.dumps(original)) + 2}, context,
        )
        assert json.loads(recovered["content"]) == [original]
    finally:
        await services.close()
