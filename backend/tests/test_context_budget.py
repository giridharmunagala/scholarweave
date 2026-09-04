from __future__ import annotations

import json
from typing import Any

import pytest

from backend.agents.context import ScholarWeaveContext, ToolReceipt
from backend.agents.context_budget import (
    CHECKPOINT_MESSAGE_PREFIX,
    ContextBudgetPolicy,
    _adaptive_target_tokens,
)
from backend.agents.harness import AgentDefinition, ModelBinding, ModelSettings
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

    async def invoke(self, catalog_id, arguments, context):
        raise AssertionError("No tool invocation was expected.")

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
    )


def policy(settings: Settings, **overrides: Any) -> ContextBudgetPolicy:
    return ContextBudgetPolicy(settings, **overrides)


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


def test_compaction_target_scales_with_model_context_window() -> None:
    fallback_target = _adaptive_target_tokens(22_937, 8_192)
    large_model_target = _adaptive_target_tokens(91_750, 8_192)

    assert fallback_target > 8_192
    assert large_model_target > fallback_target


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
            {"role": "assistant", "content": "Prior reasoning " * 1_000},
            large_result,
        ],
        context,
        instructions="Use cited evidence.",
    )

    serialized = json.dumps(compacted.items)
    assert "Prior reasoning Prior reasoning" not in serialized
    assert original_user_message in compacted.items
    assert "artifact-1" in serialized
    assert "The research objective remains active" in serialized
    assert len(summarizer.requests) == 1
    assert summarizer.requests[0].get("stream") is None
    checkpoint = context.metadata["context_checkpoints"][0]
    assert checkpoint["summary_method"] == "model"
    assert checkpoint["references"] == ["artifact-1", "https://example.com/source"]
    assert checkpoint["activity"][0]["title"] == "Created papers/paper-1/notes.md"
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
            {"role": "assistant", "content": "Prior reasoning " * 1_000},
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
    ]

    first = await budget.prepare(definition, list(initial_input), "", context, turn_index=1)
    working = [*first.working_items, {"role": "assistant", "content": "New turn."}]
    second = await budget.prepare(definition, working, "", context, turn_index=2)

    assert "Prior reasoning Prior reasoning" not in json.dumps(second.items)
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
    runtime = CheckpointRuntime()
    context = ScholarWeaveContext(run_id="run-1", tool_runtime=runtime)

    await prepare(
        settings,
        agent(),
        [
            {"role": "user", "content": "Research the topic."},
            {"role": "assistant", "content": "Prior reasoning " * 1_000},
        ],
        context,
    )

    assert runtime.stored[0]["summary_method"] == "model"
    assert "prior evidence was retained" in runtime.stored[0]["model_summary"]


@pytest.mark.anyio
async def test_oversized_tool_outputs_are_bounded_before_the_model_sees_them(
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

    assert runtime.bounded == [("custom_large_tool", "large " * 2_000)]
    assert prepared.items[0]["call_id"] == "call-1"
    assert json.loads(prepared.items[0]["output"])["result_ref"] == "retained-result"


@pytest.mark.anyio
async def test_bounding_uses_the_selected_models_context_window(tmp_path) -> None:
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

    assert runtime.bounded == [("tool_output", "large " * 1_000)]
    assert json.loads(prepared.items[0]["output"])["result_ref"] == "retained-result"
