from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from agents.run_config import CallModelData, ModelInputData

from backend.core.config import Settings
from backend.runtime.context import ScholarWeaveContext
from backend.runtime.context_budget import (
    _adaptive_target_tokens,
    create_context_budget_filter,
)
from backend.runtime.lifecycle import start_agent_invocation
from backend.runtime.steering import SteeringInbox, SteeringMessage, steering_message_id


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


class SummaryModel:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def get_response(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            output=[
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "The research objective remains active; prior evidence was retained.",
                        }
                    ],
                }
            ]
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
    budget_filter = create_context_budget_filter(settings)
    agent = SimpleNamespace(name="Worker")
    first_input = [{"role": "user", "content": "Research the topic."}]

    first = await budget_filter(
        CallModelData(
            model_data=ModelInputData(input=first_input, instructions=None),
            agent=agent,
            context=context,
        )
    )
    message = inbox.queue("Answer directly with the evidence already found.")
    second = await budget_filter(
        CallModelData(
            model_data=ModelInputData(
                input=[
                    *first_input,
                    {"role": "assistant", "content": "I will search first."},
                ],
                instructions=None,
            ),
            agent=agent,
            context=context,
        )
    )

    steering_item = {
        "role": "user",
        "content": "Answer directly with the evidence already found.",
    }
    assert steering_item not in first.input
    assert second.input[-1] == steering_item
    assert len(session.items) == 1
    assert steering_message_id(session.items[0]) == message.id
    assert ("steering.applied", {"message_id": message.id, "content": message.content}) in sink.events


@pytest.mark.anyio
async def test_compaction_does_not_duplicate_replayed_steering(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=4_096,
        agent_context_high_water_ratio=0.7,
        agent_context_compaction_target_tokens=1_024,
    )
    context = ScholarWeaveContext(
        run_id="run-1",
        conversation_id="conversation-1",
        tool_runtime=Runtime(),
    )
    inbox = SteeringInbox()
    inbox.bind_session(Session())
    context.metadata["_steering_inbox"] = inbox
    budget_filter = create_context_budget_filter(settings)
    agent = SimpleNamespace(name="Worker")
    initial_input = [
        {"role": "user", "content": "Research the topic."},
        {"role": "assistant", "content": "Prior reasoning " * 1_000},
    ]
    inbox.queue("Give the conclusion as soon as this call finishes.")

    first = await budget_filter(
        CallModelData(
            model_data=ModelInputData(input=initial_input, instructions=None),
            agent=agent,
            context=context,
        )
    )
    second = await budget_filter(
        CallModelData(
            model_data=ModelInputData(
                input=[*initial_input, {"role": "assistant", "content": "New evidence."}],
                instructions=None,
            ),
            agent=agent,
            context=context,
        )
    )

    steering_item = {
        "role": "user",
        "content": "Give the conclusion as soon as this call finishes.",
    }
    assert first.input.count(steering_item) == 1
    assert second.input.count(steering_item) == 1


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

    filtered = await create_context_budget_filter(settings)(
        CallModelData(
            model_data=ModelInputData(
                input=[
                    message.session_item(),
                    {"role": "user", "content": "Resume after restart."},
                ],
                instructions=None,
            ),
            agent=SimpleNamespace(name="Worker"),
            context=context,
        )
    )

    assert filtered.input.count(message.input_item()) == 1


@pytest.mark.anyio
async def test_high_water_filter_replaces_raw_history_with_checkpoint(tmp_path) -> None:
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
            "extended_work_plan": [
                {
                    "id": "evidence",
                    "title": "Collect evidence",
                    "status": "in_progress",
                }
            ]
        },
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
    budget_filter = create_context_budget_filter(settings)
    agent = SimpleNamespace(name="Worker", model=SummaryModel())

    compacted = await budget_filter(
        CallModelData(
            model_data=ModelInputData(
                input=[
                    {"role": "user", "content": "Research the topic."},
                    {
                        "role": "assistant",
                        "content": "Prior reasoning " * 1_000,
                    },
                    large_result,
                ],
                instructions="Use cited evidence.",
            ),
            agent=agent,
            context=context,
        )
    )

    serialized = json.dumps(compacted.input)
    assert "Prior reasoning Prior reasoning" not in serialized
    assert "artifact-1" in serialized
    assert "The research objective remains active" in serialized
    assert len(agent.model.calls) == 1
    checkpoint = context.metadata["context_checkpoints"][0]
    assert checkpoint["summary_method"] == "model"
    assert checkpoint["references"] == [
        "artifact-1",
        "https://example.com/source",
    ]
    lifecycle = [
        (event_type, payload)
        for event_type, payload in sink.events
        if event_type.startswith("agent.")
    ]
    assert lifecycle[0] == (
        "agent.started",
        {"agent_name": "Worker", "invocation_id": invocation_id},
    )
    assert len(lifecycle) == 1
    assert any(event_type == "context.compacted" for event_type, _ in sink.events)


@pytest.mark.anyio
async def test_filter_persists_compacted_history_across_model_turns(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=4_096,
        agent_context_high_water_ratio=0.7,
        agent_context_compaction_target_tokens=1_024,
        tool_result_max_tokens=16_000,
    )
    context = ScholarWeaveContext(run_id="run-1", tool_runtime=Runtime())
    agent = SimpleNamespace(name="Worker")
    budget_filter = create_context_budget_filter(settings)
    initial_input = [
        {"role": "user", "content": "Research the topic."},
        {"role": "assistant", "content": "Prior reasoning " * 1_000},
    ]

    first = await budget_filter(
        CallModelData(
            model_data=ModelInputData(input=initial_input, instructions=None),
            agent=agent,
            context=context,
        )
    )
    second = await budget_filter(
        CallModelData(
            model_data=ModelInputData(
                input=[*initial_input, {"role": "assistant", "content": "New turn."}],
                instructions=None,
            ),
            agent=agent,
            context=context,
        )
    )

    assert "Prior reasoning Prior reasoning" not in json.dumps(second.input)
    assert {"role": "assistant", "content": "New turn."} in second.input
    assert len(context.metadata["context_checkpoints"]) == 1
    assert first.input[0]["role"] == "user"


@pytest.mark.anyio
async def test_compaction_state_is_isolated_per_agent(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=4_096,
        agent_context_high_water_ratio=0.7,
        agent_context_compaction_target_tokens=1_024,
        tool_result_max_tokens=16_000,
    )
    context = ScholarWeaveContext(run_id="run-1", tool_runtime=Runtime())
    budget_filter = create_context_budget_filter(settings)
    shared_input = [
        {"role": "user", "content": "Research independently."},
        {"role": "assistant", "content": "Prior reasoning " * 1_000},
    ]

    worker_a = await budget_filter(
        CallModelData(
            model_data=ModelInputData(input=shared_input, instructions=None),
            agent=SimpleNamespace(name="Worker A"),
            context=context,
        )
    )
    worker_b = await budget_filter(
        CallModelData(
            model_data=ModelInputData(input=shared_input, instructions=None),
            agent=SimpleNamespace(name="Worker B"),
            context=context,
        )
    )

    assert len(context.metadata["context_checkpoints"]) == 2
    assert json.loads(worker_a.input[0]["content"].split("\n\n", 1)[1])["agent_name"] == "Worker A"
    assert json.loads(worker_b.input[0]["content"].split("\n\n", 1)[1])["agent_name"] == "Worker B"


@pytest.mark.anyio
async def test_compaction_keeps_divergent_invocations_for_same_agent_separate(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=4_096,
        agent_context_high_water_ratio=0.7,
        agent_context_compaction_target_tokens=1_024,
        tool_result_max_tokens=16_000,
    )
    context = ScholarWeaveContext(run_id="run-1", tool_runtime=Runtime())
    agent = SimpleNamespace(name="Shared worker")
    budget_filter = create_context_budget_filter(settings)
    initial_input = [
        {"role": "user", "content": "Research independently."},
        {"role": "assistant", "content": "Prior reasoning " * 1_000},
    ]
    await budget_filter(
        CallModelData(
            model_data=ModelInputData(input=initial_input, instructions=None),
            agent=agent,
            context=context,
        )
    )
    await budget_filter(
        CallModelData(
            model_data=ModelInputData(
                input=[
                    *initial_input,
                    {"role": "assistant", "content": "Branch A " * 2_000},
                ],
                instructions=None,
            ),
            agent=agent,
            context=context,
        )
    )
    branch_b = await budget_filter(
        CallModelData(
            model_data=ModelInputData(
                input=[
                    *initial_input,
                    {"role": "assistant", "content": "Branch B " * 2_000},
                ],
                instructions=None,
            ),
            agent=agent,
            context=context,
        )
    )

    states = context.metadata["_context_compaction_states"]["Shared worker"]
    assert len(states) == 2
    assert states[1]["source_input"][-1]["content"].startswith("Branch B")
    assert "Branch A" not in json.dumps(branch_b.input)


@pytest.mark.anyio
async def test_compaction_keeps_tool_call_and_output_in_one_recent_chunk(tmp_path) -> None:
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

    compacted = await create_context_budget_filter(settings)(
        CallModelData(
            model_data=ModelInputData(
                input=[
                    {"role": "user", "content": "Research the topic."},
                    {"role": "assistant", "content": "Prior reasoning " * 1_000},
                    call,
                    output,
                ],
                instructions=None,
            ),
            agent=SimpleNamespace(name="Worker"),
            context=context,
        )
    )

    assert call in compacted.input
    assert output in compacted.input


@pytest.mark.anyio
async def test_completed_hosted_call_does_not_absorb_newer_messages(tmp_path) -> None:
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

    compacted = await create_context_budget_filter(settings)(
        CallModelData(
            model_data=ModelInputData(
                input=[
                    {"role": "assistant", "content": "Old reasoning " * 1_000},
                    {
                        "type": "web_search_call",
                        "id": "search-1",
                        "status": "completed",
                    },
                    {"role": "assistant", "content": "Large search analysis " * 500},
                    latest,
                ],
                instructions=None,
            ),
            agent=SimpleNamespace(name="Worker"),
            context=context,
        )
    )

    assert latest in compacted.input


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

    await create_context_budget_filter(settings)(
        CallModelData(
            model_data=ModelInputData(
                input=[
                    {"role": "user", "content": "Research the topic."},
                    {"role": "assistant", "content": "Prior reasoning " * 1_000},
                ],
                instructions=None,
            ),
            agent=SimpleNamespace(name="Worker", model=SummaryModel()),
            context=context,
        )
    )

    assert runtime.stored[0]["summary_method"] == "model"
    assert "prior evidence was retained" in runtime.stored[0]["model_summary"]


@pytest.mark.anyio
async def test_input_filter_bounds_non_application_tool_outputs(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        tool_result_max_tokens=512,
    )
    runtime = Runtime()
    context = ScholarWeaveContext(run_id="run-1", tool_runtime=runtime)
    budget_filter = create_context_budget_filter(settings)

    filtered = await budget_filter(
        CallModelData(
            model_data=ModelInputData(
                input=[
                    {
                        "type": "function_call_output",
                        "name": "custom_large_tool",
                        "call_id": "call-1",
                        "output": "large " * 2_000,
                    }
                ],
                instructions=None,
            ),
            agent=SimpleNamespace(name="Worker"),
            context=context,
        )
    )

    assert runtime.bounded == [("custom_large_tool", "large " * 2_000)]
    assert filtered.input[0]["call_id"] == "call-1"
    assert json.loads(filtered.input[0]["output"])["result_ref"] == "retained-result"


@pytest.mark.anyio
async def test_input_filter_uses_selected_models_context_window(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        agent_context_window_tokens=128_000,
        agent_context_compaction_target_tokens=8_192,
        tool_result_max_tokens=3_000,
    )
    runtime = Runtime()
    context = ScholarWeaveContext(run_id="run-1", tool_runtime=runtime)
    agent = SimpleNamespace(name="Small local model")
    budget_filter = create_context_budget_filter(settings, {id(agent): 4_096})

    filtered = await budget_filter(
        CallModelData(
            model_data=ModelInputData(
                input=[
                    {
                        "type": "function_call_output",
                        "call_id": "call-1",
                        "output": "large " * 1_000,
                    }
                ],
                instructions=None,
            ),
            agent=agent,
            context=context,
        )
    )

    assert runtime.bounded == [("sdk.tool_output", "large " * 1_000)]
    assert json.loads(filtered.input[0]["output"])["result_ref"] == "retained-result"
