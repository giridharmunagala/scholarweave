from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from agents.run_config import CallModelData, ModelInputData

from backend.core.config import Settings
from backend.runtime.context import ScholarWeaveContext
from backend.runtime.context_budget import create_context_budget_filter
from backend.runtime.lifecycle import start_agent_invocation


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


class Sink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, payload))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


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
            agent=SimpleNamespace(name="Worker"),
            context=context,
        )
    )

    serialized = json.dumps(compacted.input)
    assert "Prior reasoning Prior reasoning" not in serialized
    assert "artifact-1" in serialized
    assert len(serialized) < settings.agent_context_compaction_target_tokens * 4
    checkpoint = context.metadata["context_checkpoints"][0]
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
    assert lifecycle[1][0] == "agent.superseded"
    assert lifecycle[1][1]["invocation_id"] == invocation_id
    assert lifecycle[2][0] == "agent.started"
    assert lifecycle[2][1]["invocation_id"] != invocation_id
    assert any(event_type == "context.compacted" for event_type, _ in sink.events)


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
