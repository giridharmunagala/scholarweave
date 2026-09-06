"""Native harness behaviour: streaming, tool loops, delegation, and policies."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import httpx
from openai import BadRequestError

from backend.agents.context import ScholarWeaveContext
from backend.agents.context_budget import ContextBudgetPolicy
from backend.agents.harness import (
    MAX_DELEGATION_DEPTH,
    AgentDefinition,
    AgentRunner,
    FunctionTool,
    JsonSchemaOutput,
    MaxTurnsExceeded,
    ModelBehaviorError,
    ModelBinding,
    ModelSettings,
    PreparedInput,
    RunPolicyViolation,
    RunSettings,
    ToolCallAccumulator,
    current_tool_model_identity,
    delegation_tool,
    request_parameters,
    run_agent,
    run_streamed,
    to_chat_messages,
)
from backend.runs.events import BufferedRunEventSink
from backend.core.config import Settings
from backend.providers.reasoning import infer_reasoning_efforts
from backend.runs.hooks import ScholarWeaveRunHooks
from backend.tests.harness_support import (
    FakeClient,
    multi_tool_call_chunks,
    stub_binding,
    text_chunks,
    tool_call_chunks,
    tool_call_fragments,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append((event_type, payload))

    async def emit_transient(self, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append((event_type, payload))

    async def emit_batch(self, events: list[tuple[str, dict[str, Any]]]) -> None:
        self.events.extend(events)


class NoopToolRuntime:
    async def invoke(self, catalog_id, arguments, context, *, tool_call_id=None):
        raise NotImplementedError

    async def bound_tool_result(self, catalog_id, result, context, *, max_tokens=None):
        return result

    def store_context_checkpoint(self, checkpoint, context):
        return {}


def make_context(sink: RecordingSink | None = None) -> ScholarWeaveContext:
    return ScholarWeaveContext(
        run_id="run-1",
        tool_runtime=NoopToolRuntime(),
        event_sink=sink,
    )


def agent(client: FakeClient, **overrides: Any) -> AgentDefinition:
    defaults: dict[str, Any] = {
        "id": "agent",
        "name": "Agent",
        "instructions": "Answer well.",
        "binding": ModelBinding(
            client=client,
            model_name="stub-model",
            provider_kind="ollama",
        ),
        "model_settings": ModelSettings(),
    }
    defaults.update(overrides)
    return AgentDefinition(**defaults)


def echo_tool(calls: list[str], *, name: str = "echo", result: Any = "ok") -> FunctionTool:
    async def invoke(_invocation, raw_arguments: str) -> Any:
        calls.append(raw_arguments)
        return result

    return FunctionTool(
        name=name,
        description="Echoes the request.",
        params_json_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        on_invoke_tool=invoke,
    )


@pytest.mark.anyio
async def test_streamed_text_reaches_the_sink_and_final_output() -> None:
    client = FakeClient.scripted(
        [text_chunks("Hello there.", usage={"prompt_tokens": 5, "completion_tokens": 3})]
    )
    sink = RecordingSink()
    context = make_context(sink)

    result = await run_agent(
        agent(client),
        "Say hello.",
        context=context,
        settings=RunSettings(),
        max_turns=3,
    )

    assert result.final_output == "Hello there."
    assert result.usage.input_tokens == 5
    assert result.usage.output_tokens == 3
    assert result.usage.requests == 1
    deltas = [
        payload["delta"]
        for event_type, payload in sink.events
        if event_type == "model.stream" and "delta" in payload
    ]
    assert "".join(deltas) == "Hello there."
    raw_types = [
        payload["raw_type"] for event_type, payload in sink.events if event_type == "model.stream"
    ]
    assert raw_types[0] == "response.created"
    assert raw_types[-1] == "response.completed"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("text", "finish_reason", "error"),
    [
        ("", "length", "finish_reason=length"),
        ("Partial answer", "length", "finish_reason=length"),
        ("", "stop", "no answer text or tool calls"),
        (" \n ", "stop", "no answer text or tool calls"),
        ("", None, "no answer text or tool calls"),
        ("Filtered partial answer", "content_filter", "finish_reason=content_filter"),
    ],
)
async def test_unusable_model_completion_is_not_a_success(text, finish_reason, error) -> None:
    chunks = text_chunks(text, usage={"prompt_tokens": 1515, "completion_tokens": 2048})
    chunks[-2]["choices"][0]["finish_reason"] = finish_reason
    chunks.insert(0, {"choices": [{"delta": {"reasoning_content": "Still thinking."}}]})
    client = FakeClient.scripted([chunks])
    with pytest.raises(ModelBehaviorError, match=error):
        await run_agent(
            agent(client), "Answer.", context=make_context(),
            settings=RunSettings(), max_turns=3,
        )
    assert len(client.requests) == 1
    assert "reasoning_effort" not in client.requests[0]


@pytest.mark.anyio
@pytest.mark.parametrize("arguments", ['{"text":"valid but truncated turn"}', '{"text":'])
async def test_length_truncated_tool_turn_never_dispatches_tools(arguments) -> None:
    chunks = tool_call_chunks("echo", arguments)
    chunks[-1]["choices"][0]["finish_reason"] = "length"
    client = FakeClient.scripted([chunks])
    calls: list[str] = []
    with pytest.raises(ModelBehaviorError, match="finish_reason=length"):
        await run_agent(
            agent(client, tools=[echo_tool(calls)]), "Use a tool.",
            context=make_context(), settings=RunSettings(), max_turns=3,
        )
    assert calls == []
    assert len(client.requests) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("arguments", ['{"text":"discard this"}', '{"text":'])
async def test_context_budget_retries_truncation_without_dispatching_partial_tools(arguments) -> None:
    truncated = tool_call_chunks("echo", arguments, text="Incomplete answer.")
    truncated[-1]["choices"][0]["finish_reason"] = "length"
    truncated.append({"choices": [], "usage": {"prompt_tokens": 1515, "completion_tokens": 2048}})
    client = FakeClient.scripted([
        truncated,
        tool_call_chunks("echo", '{"text":"execute once"}'),
        text_chunks("Complete answer.", usage={"prompt_tokens": 1600, "completion_tokens": 100}),
    ])
    calls: list[str] = []
    sink = RecordingSink()
    definition = agent(client, tools=[echo_tool(calls)])
    result = await run_agent(
        definition, "Do the research.", context=make_context(sink),
        settings=RunSettings(), max_turns=2,
        context_policy=ContextBudgetPolicy(Settings()),
    )

    assert result.final_output == "Complete answer."
    assert calls == ['{"text":"execute once"}']
    assert [request["max_tokens"] for request in client.requests] == [2048, 4096, 4096]
    assert client.requests[0]["messages"] == client.requests[1]["messages"]
    assert "Incomplete answer." not in json.dumps(result.generated_items)
    assert result.usage.requests == 3
    assert result.usage.output_tokens == 2148
    assert definition.model_settings.max_tokens is None
    retries = [payload for kind, payload in sink.events if kind == "model.retry"]
    assert len(retries) == 1
    assert retries[0]["discarded_text_characters"] == len("Incomplete answer.")


@pytest.mark.anyio
async def test_truncation_growth_uses_context_not_a_fixed_output_ceiling() -> None:
    truncated = text_chunks("Still incomplete.")
    truncated[-1]["choices"][0]["finish_reason"] = "length"
    client = FakeClient.scripted([truncated] * 5 + [text_chunks("Done.")])
    result = await run_agent(
        agent(client), "Answer.", context=make_context(), settings=RunSettings(),
        context_policy=ContextBudgetPolicy(Settings(agent_context_window_tokens=80_000)),
        max_turns=1,
    )
    assert result.final_output == "Done."
    assert [request["max_tokens"] for request in client.requests] == [
        2048, 4096, 8192, 16384, 32768, 65536,
    ]


@pytest.mark.anyio
async def test_truncation_stops_when_context_has_no_more_generation_room() -> None:
    truncated = text_chunks("Incomplete.")
    truncated[-1]["choices"][0]["finish_reason"] = "length"
    client = FakeClient.scripted([truncated])
    with pytest.raises(ModelBehaviorError, match="available context"):
        await run_agent(
            agent(client), "Answer.", context=make_context(), settings=RunSettings(),
            context_policy=ContextBudgetPolicy(Settings(agent_context_window_tokens=4096)),
        )
    budgets = [request["max_tokens"] for request in client.requests]
    assert len(budgets) > 1
    assert budgets == sorted(set(budgets))
    assert max(budgets) < 4096


@pytest.mark.anyio
async def test_explicit_response_cap_is_not_silently_overridden() -> None:
    truncated = text_chunks("Incomplete.")
    truncated[-1]["choices"][0]["finish_reason"] = "length"
    client = FakeClient.scripted([truncated])
    with pytest.raises(ModelBehaviorError, match="explicit response"):
        await run_agent(
            agent(client, model_settings=ModelSettings(max_tokens=1024)),
            "Answer.", context=make_context(), settings=RunSettings(),
            context_policy=ContextBudgetPolicy(Settings()),
        )
    assert len(client.requests) == 1


@pytest.mark.anyio
async def test_long_tool_run_recovers_truncation_over_real_chat_completions(
    stub_provider, monkeypatch,
) -> None:
    stub_provider.tool_plans = [
        ("Research", "echo", {"text": str(index)}) for index in range(24)
    ]
    original_stream = stub_provider.stream

    def stream(payload):
        response = original_stream(payload)
        if payload["max_tokens"] < 8192:
            response = response.replace('"finish_reason": "stop"', '"finish_reason": "length"')
        return response

    monkeypatch.setattr(stub_provider, "stream", stream)
    binding = stub_binding(stub_provider, context_window_tokens=80_000)
    calls: list[str] = []
    sink = RecordingSink()
    try:
        result = await run_agent(
            AgentDefinition(
                id="research", name="Research", instructions="Research carefully.",
                binding=binding, tools=[echo_tool(calls)],
            ),
            "Research", context=make_context(sink), settings=RunSettings(),
            context_policy=ContextBudgetPolicy(Settings()),
        )
    finally:
        await binding.client.close()
    assert result.final_output == stub_provider.reply
    assert [json.loads(call)["text"] for call in calls] == [str(index) for index in range(24)]
    assert len(stub_provider.requests) == 27
    assert [request["max_tokens"] for request in stub_provider.requests[-3:]] == [2048, 4096, 8192]
    assert len([item for item in result.generated_items if item.get("role") == "assistant"]) == 1


@pytest.mark.anyio
async def test_cancellation_interrupts_response_recovery_before_another_request() -> None:
    retry_started = asyncio.Event()

    class PausingSink(RecordingSink):
        async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
            await super().emit(event_type, payload)
            if event_type == "model.retry":
                retry_started.set()
                await asyncio.Event().wait()

    truncated = tool_call_chunks("echo", '{"text":"must not execute"}')
    truncated[-1]["choices"][0]["finish_reason"] = "length"
    client = FakeClient.scripted([truncated, text_chunks("Not reached.")])
    calls: list[str] = []
    task = asyncio.create_task(run_agent(
        agent(client, tools=[echo_tool(calls)]), "Research.",
        context=make_context(PausingSink()), settings=RunSettings(),
        context_policy=ContextBudgetPolicy(Settings()),
    ))
    try:
        await asyncio.wait_for(retry_started.wait(), timeout=2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert calls == []
    assert len(client.requests) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("context_error", [True, False])
async def test_provider_context_rejection_reprepares_without_retrying_unrelated_errors(context_error) -> None:
    class ArchiveRuntime(NoopToolRuntime):
        def store_context_history(self, items, context):
            return {"result_ref": "runs/run-1/history.json"}

    error = BadRequestError(
        "Provider rejected request.",
        response=httpx.Response(400, request=httpx.Request("POST", "http://stub.test/v1/chat/completions")),
        body={"error": {
            "type": "exceed_context_size_error" if context_error else "invalid_request_error",
            "message": "Input is too large." if context_error else "Unknown model.",
        }},
    )
    client = FakeClient.scripted([text_chunks("Done.")])
    create = client.chat.completions.create

    async def reject_first(**parameters):
        if not client.requests:
            client.requests.append(parameters)
            raise error
        return await create(**parameters)

    client.chat.completions.create = reject_first
    sink = RecordingSink()
    context = ScholarWeaveContext(run_id="run-1", tool_runtime=ArchiveRuntime(), event_sink=sink)
    calls: list[str] = []
    history = [
        {"role": "user", "content": "Keep exact sources."},
        {"role": "assistant", "content": "Old research evidence. " * 2000},
        {"role": "user", "content": "Check the method."},
        {"role": "assistant", "content": "Checked."},
        {"role": "user", "content": "Check scope."},
        {"role": "assistant", "content": "Checked."},
        {"role": "user", "content": "Answer now."},
    ]
    execution = run_agent(
        agent(client, tools=[echo_tool(calls, name="read_tool_result")]),
        history, context=context, settings=RunSettings(),
        context_policy=ContextBudgetPolicy(Settings(agent_context_model_summary_enabled=False)),
    )
    if context_error:
        result = await execution
        assert result.final_output == "Done."
        assert result.usage.requests == 2
        assert len(json.dumps(client.requests[1]["messages"])) < len(json.dumps(client.requests[0]["messages"]))
        assert any(kind == "context.compacted" for kind, _ in sink.events)
        assert any(kind == "model.retry" and payload["reason"] == "context_length_exceeded"
                   for kind, payload in sink.events)
        assert calls == []
    else:
        with pytest.raises(ModelBehaviorError, match="provider request failed"):
            await execution
        assert len(client.requests) == 1
        assert not any(kind == "model.retry" for kind, _ in sink.events)


@pytest.mark.anyio
async def test_tool_only_turn_uses_server_timings_not_argument_delta_time() -> None:
    now = 10.0

    async def create(**_parameters):
        async def chunks():
            nonlocal now
            scripted = tool_call_chunks("echo", '{"text":"hello"}')
            now = 12.0
            yield scripted[0]
            now = 15.0
            yield scripted[1]
            yield {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 30}}
            yield {"choices": [], "timings": {
                "prompt_n": 20, "prompt_ms": 100, "predicted_n": 30, "predicted_ms": 200,
            }}
        return chunks()

    downstream = RecordingSink()
    sink = BufferedRunEventSink(downstream, clock=lambda: now)
    calls: list[str] = []
    result = await run_agent(
        agent(FakeClient(create), tools=[echo_tool(calls)], stop_on_first_tool=True),
        "Use the tool.", context=make_context(sink), settings=RunSettings(),
        hooks=ScholarWeaveRunHooks(), max_turns=1,
    )
    await sink.flush()
    assert result.final_output == "ok"
    assert calls == ['{"text":"hello"}']
    assert sink.performance()["prompt_seconds"] == 0.1
    assert sink.performance()["generation_seconds"] == 0.2
    assert sink.performance()["generation_tokens_per_second"] == 150.0
    assert not any(
        payload.get("snapshot") for event_type, payload in downstream.events
        if event_type == "model.stream"
    )


@pytest.mark.anyio
async def test_official_client_preserves_final_timing_only_chunk(stub_provider, monkeypatch) -> None:
    original_stream = stub_provider.stream

    def stream(payload):
        body = original_stream(payload)
        records = [
            {"choices": [], "timings": {"prompt_n": 10, "prompt_ms": 10,
                                       "predicted_n": 1, "predicted_ms": 10}},
            {"choices": [], "usage": {"prompt_tokens": 1000, "completion_tokens": 40}},
            {"choices": [], "timings": {"prompt_n": 100, "prompt_ms": 250,
                                       "predicted_n": 40, "predicted_ms": 500}},
        ]
        return body.replace("data: [DONE]\n\n", "".join(
            f"data: {json.dumps(record)}\n\n" for record in records
        ) + "data: [DONE]\n\n")

    monkeypatch.setattr(stub_provider, "stream", stream)
    downstream = RecordingSink()
    sink = BufferedRunEventSink(downstream)
    binding = stub_binding(stub_provider)
    try:
        result = await run_agent(
            AgentDefinition(id="main", name="Main", instructions="Answer.", binding=binding),
            "Hello", context=make_context(sink), settings=RunSettings(),
            hooks=ScholarWeaveRunHooks(),
        )
    finally:
        await binding.client.close()
    assert result.usage.input_tokens == 1000
    telemetry = [payload for kind, payload in downstream.events if kind == "model.telemetry"]
    assert len(telemetry) == 1
    assert telemetry[0]["timings"]["predicted_n"] == 40
    assert telemetry[0]["context_scope"] == "main"
    assert sink.performance()["model_calls"] == 1
    assert sink.performance()["input_tokens"] == 1000
    assert sink.performance()["prompt_tokens_per_second"] == 400
    assert sink.performance()["generation_tokens_per_second"] == 80


@pytest.mark.parametrize("model", ["qwen-27b", "org/Qwen-27B"])
def test_local_qwen_alias_infers_switchable_reasoning_only_for_compatible_provider(model) -> None:
    assert infer_reasoning_efforts("openai_compatible", model) == ["none", "high"]
    assert infer_reasoning_efforts("openai", model) is None
    assert infer_reasoning_efforts("ollama", model) is None


@pytest.mark.parametrize("model", ["qwen-7b", "qwen-27b-q8", "qwen-27b:latest"])
def test_unknown_qwen_alias_does_not_infer_switchable_reasoning(model) -> None:
    assert infer_reasoning_efforts("openai_compatible", model) is None


@pytest.mark.anyio
@pytest.mark.parametrize("effort", ["none", "high"])
async def test_qwen_reasoning_off_and_explicit_override_use_official_wire_field(stub_provider, effort) -> None:
    binding = stub_binding(
        stub_provider, model_name="qwen-27b", provider_kind="openai_compatible",
        reasoning_efforts=("none", "high"),
    )
    try:
        await run_agent(
            AgentDefinition(
                id="summary", name="Summary", instructions="Summarize.", binding=binding,
                model_settings=ModelSettings(reasoning_effort=effort),
            ),
            "Paper text.", context=make_context(), settings=RunSettings(),
        )
    finally:
        await binding.client.close()
    assert stub_provider.requests[-1]["reasoning_effort"] == effort
    assert "chat_template_kwargs" not in stub_provider.requests[-1]


@pytest.mark.anyio
async def test_failed_stream_retains_reported_usage_once() -> None:
    async def create(**_parameters):
        async def chunks():
            yield {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 2},
                   "timings": {"predicted_n": 2, "predicted_ms": 100}}
            raise RuntimeError("connection interrupted")
        return chunks()

    downstream = RecordingSink()
    sink = BufferedRunEventSink(downstream)
    with pytest.raises(RuntimeError, match="interrupted"):
        await run_agent(
            agent(FakeClient(create)), "Answer.", context=make_context(sink),
            settings=RunSettings(), hooks=ScholarWeaveRunHooks(),
        )
    assert sink.performance()["model_calls"] == 1
    assert sink.performance()["output_tokens"] == 2
    assert sink.performance()["generation_tokens_per_second"] == 20
    telemetry = next(payload for kind, payload in downstream.events if kind == "model.telemetry")
    assert telemetry["completed"] is False


@pytest.mark.anyio
async def test_retry_actual_usage_preserves_policy_safety_headroom() -> None:
    class Policy:
        async def prepare(self, candidate, items, instructions, context, *, turn_index):
            return PreparedInput(
                items=items, instructions=instructions,
                response_max_tokens=candidate.model_settings.max_tokens or 3000,
                response_retry_max_tokens=8000, estimated_input_tokens=1000,
                context_window_tokens=10000, safety_headroom_tokens=1000,
            )

    partial = [
        {"choices": [{"index": 0, "delta": {"content": "Partial"}, "finish_reason": "length"}]},
        {"choices": [], "usage": {"prompt_tokens": 5000, "completion_tokens": 3000}},
    ]
    client = FakeClient.scripted([
        partial, text_chunks("Complete", usage={"prompt_tokens": 5000, "completion_tokens": 10}),
    ])
    result = await run_agent(
        agent(client), "Answer.", context=make_context(), settings=RunSettings(),
        context_policy=Policy(),
    )
    assert result.final_output == "Complete"
    assert [request["max_tokens"] for request in client.requests] == [3000, 4000]
    assert result.usage.output_tokens == 3010


@pytest.mark.anyio
async def test_summary_retry_preserves_eight_thousand_token_reserve() -> None:
    partial = [
        {"choices": [{"index": 0, "delta": {"content": "Partial"}, "finish_reason": "length"}]},
        {"choices": [], "usage": {"prompt_tokens": 45000, "completion_tokens": 20000}},
    ]
    client = FakeClient.scripted([
        partial, text_chunks("Complete", usage={"prompt_tokens": 45000, "completion_tokens": 10}),
    ])
    context = make_context()
    context.metadata["paper_summary_document_id"] = "paper-1"
    candidate = agent(client)
    candidate.binding = ModelBinding(
        client=client, model_name="qwen-27b", provider_kind="openai_compatible",
        context_window_tokens=80000,
    )
    result = await run_agent(
        candidate, "Answer.", context=context, settings=RunSettings(),
        context_policy=ContextBudgetPolicy(Settings(agent_context_model_summary_enabled=False)),
    )
    assert result.final_output == "Complete"
    assert [request["max_tokens"] for request in client.requests] == [20000, 27000]


@pytest.mark.anyio
async def test_tool_call_round_trips_into_the_next_model_turn() -> None:
    client = FakeClient.scripted(
        [
            tool_call_chunks("echo", '{"text":"hi"}', reasoning="thinking"),
            text_chunks("Done."),
        ]
    )
    calls: list[str] = []
    sink = RecordingSink()

    result = await run_agent(
        agent(client, tools=[echo_tool(calls)]),
        "Use the tool.",
        context=make_context(sink),
        settings=RunSettings(),
        max_turns=4,
    )

    assert calls == ['{"text":"hi"}']
    assert result.final_output == "Done."
    assert [item["type"] for item in result.new_items] == [
        "reasoning_item",
        "tool_call_item",
        "tool_call_output_item",
        "message_output_item",
    ]
    second_request = client.requests[1]
    assistant_call = next(
        message
        for message in second_request["messages"]
        if message.get("tool_calls")
    )
    assert assistant_call["tool_calls"][0]["function"]["name"] == "echo"
    tool_message = next(
        message for message in second_request["messages"] if message["role"] == "tool"
    )
    assert tool_message["tool_call_id"] == "call-1"
    assert tool_message["content"] == "ok"
    assert ("run.item", {"name": "tool_called", "item": result.new_items[1]}) in sink.events


@pytest.mark.anyio
async def test_unknown_tool_is_reported_to_the_model_without_failing_the_run() -> None:
    client = FakeClient.scripted(
        [tool_call_chunks("missing_tool", "{}"), text_chunks("Recovered.")]
    )

    result = await run_agent(
        agent(client, tools=[echo_tool([])]),
        "Call a tool.",
        context=make_context(),
        settings=RunSettings(),
        max_turns=4,
    )

    output = result.generated_items[1]
    assert "is not available to this agent" in output["output"]
    assert result.final_output == "Recovered."


@pytest.mark.anyio
async def test_tool_errors_become_model_visible_results() -> None:
    async def failing(_invocation, _raw_arguments: str) -> Any:
        raise RuntimeError("upstream unavailable")

    tool = echo_tool([])
    tool.on_invoke_tool = failing
    client = FakeClient.scripted(
        [tool_call_chunks("echo", "{}"), text_chunks("Handled.")]
    )

    result = await run_agent(
        agent(client, tools=[tool]),
        "Call the tool.",
        context=make_context(),
        settings=RunSettings(),
        max_turns=4,
    )

    assert "RuntimeError: upstream unavailable" in result.generated_items[1]["output"]
    assert result.final_output == "Handled."


@pytest.mark.anyio
async def test_serialized_tool_rejects_a_second_call_in_the_same_turn() -> None:
    started = 0

    async def invoke(_invocation, _raw_arguments: str) -> str:
        nonlocal started
        started += 1
        await asyncio.sleep(0.01)
        return "receipt"

    tool = FunctionTool(
        name="summary",
        description="Summarize one paper.",
        params_json_schema={"type": "object", "properties": {}, "additionalProperties": False},
        on_invoke_tool=invoke,
        serialize_calls=True,
    )
    chunks = [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": index,
                                "id": f"call-{index}",
                                "type": "function",
                                "function": {"name": "summary", "arguments": "{}"},
                            }
                            for index in range(2)
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]
    client = FakeClient.scripted([chunks, text_chunks("Done.")])

    result = await run_agent(
        agent(client, tools=[tool]),
        "Summarize twice.",
        context=make_context(),
        settings=RunSettings(),
        max_turns=4,
    )

    outputs = [
        item["output"]
        for item in result.generated_items
        if item.get("type") == "function_call_output"
    ]
    assert started == 1
    assert outputs[0] == "receipt"
    assert "one call at a time" in outputs[1]
    assert "not started" in outputs[1]


@pytest.mark.anyio
async def test_agents_that_disable_parallel_tool_calls_run_tools_sequentially() -> None:
    active = 0
    peak = 0

    async def invoke(_invocation, _raw_arguments: str) -> str:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return "ok"

    tool = echo_tool([])
    tool.on_invoke_tool = invoke
    chunks = [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": index,
                                "id": f"call-{index}",
                                "type": "function",
                                "function": {"name": "echo", "arguments": "{}"},
                            }
                            for index in range(3)
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]

    await run_agent(
        agent(
            FakeClient.scripted([chunks, text_chunks("Done.")]),
            tools=[tool],
            model_settings=ModelSettings(parallel_tool_calls=False),
        ),
        "Call the tool three times.",
        context=make_context(),
        settings=RunSettings(max_tool_concurrency=4),
        max_turns=4,
    )

    assert peak == 1


@pytest.mark.anyio
async def test_default_harness_completes_more_than_sixteen_turns(stub_provider) -> None:
    stub_provider.tool_plans = [
        ("extended research", "echo", {"text": str(index)}) for index in range(20)
    ]
    model = stub_binding(stub_provider)
    calls: list[str] = []
    definition = AgentDefinition(
        id="researcher",
        name="Researcher",
        instructions="Research until finished.",
        binding=model,
        tools=[echo_tool(calls)],
    )
    try:
        result = await run_agent(
            definition,
            "Complete this extended research.",
            context=make_context(),
            settings=RunSettings(),
        )
    finally:
        await model.client.close()

    assert result.final_output == stub_provider.reply
    assert len(calls) == 20
    assert result.usage.requests == len(stub_provider.requests) == 21


@pytest.mark.anyio
@pytest.mark.parametrize("surface", ["agent", "streamed", "runner"])
@pytest.mark.parametrize(
    ("override", "expected_turns"),
    [("omitted", 2), ("unlimited", 4), ("finite", 3)],
)
async def test_turn_limit_override_preserves_configured_defaults(
    surface, override, expected_turns,
) -> None:
    client = FakeClient.scripted([
        *(tool_call_chunks("echo", '{"text":"continue"}') for _ in range(3)),
        text_chunks("Finished."),
    ])
    definition = agent(client, tools=[echo_tool([])])
    context = make_context()
    settings = RunSettings(max_turns=2)
    kwargs = {} if override == "omitted" else {
        "max_turns": None if override == "unlimited" else 3,
    }
    if surface == "runner":
        pending = AgentRunner(definition, context=context, settings=settings).run(
            "Research.", **kwargs,
        )
    else:
        execute = run_agent if surface == "agent" else run_streamed
        pending = execute(
            definition, "Research.", context=context, settings=settings, **kwargs,
        )
    if override == "unlimited":
        assert (await pending).final_output == "Finished."
    else:
        with pytest.raises(MaxTurnsExceeded) as raised:
            await pending
        assert raised.value.run_data.usage.requests == expected_turns
    assert len(client.requests) == expected_turns


@pytest.mark.anyio
async def test_max_turns_is_enforced_and_carries_partial_progress() -> None:
    client = FakeClient.scripted([tool_call_chunks("echo", "{}")])

    with pytest.raises(MaxTurnsExceeded) as raised:
        await run_agent(
            agent(client, tools=[echo_tool([])]),
            "Loop forever.",
            context=make_context(),
            settings=RunSettings(),
            max_turns=2,
        )

    assert raised.value.run_data.usage.requests == 2
    assert len(client.requests) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("max_turns", [None, 2])
async def test_run_can_be_cancelled_mid_stream(max_turns) -> None:
    started = asyncio.Event()
    client = FakeClient.blocking(started)
    handle = run_streamed(
        agent(client),
        "Answer.",
        context=make_context(),
        settings=RunSettings(),
        max_turns=max_turns,
    )

    await asyncio.wait_for(started.wait(), timeout=5)
    handle.cancel()

    with pytest.raises(asyncio.CancelledError):
        await handle


@pytest.mark.anyio
async def test_length_policy_is_a_plain_check_not_framework_machinery() -> None:
    client = FakeClient.scripted([text_chunks("ok")])

    with pytest.raises(RunPolicyViolation) as raised:
        await run_agent(
            agent(client),
            "x" * 200,
            context=make_context(),
            settings=RunSettings(max_input_characters=10),
            max_turns=2,
        )

    assert raised.value.policy == "max_input_characters"
    assert raised.value.detail["max_characters"] == 10

    with pytest.raises(RunPolicyViolation) as output_raised:
        await run_agent(
            agent(FakeClient.scripted([text_chunks("a long answer")])),
            "hi",
            context=make_context(),
            settings=RunSettings(max_output_characters=3),
            max_turns=2,
        )

    assert output_raised.value.policy == "max_output_characters"


@pytest.mark.anyio
async def test_structured_output_is_validated_locally() -> None:
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    client = FakeClient.scripted([text_chunks('{"answer":"yes"}')])

    result = await run_agent(
        agent(client, output_schema=JsonSchemaOutput("Finding", schema, strict=True)),
        "Answer as JSON.",
        context=make_context(),
        settings=RunSettings(),
        max_turns=2,
    )

    assert result.final_output == {"answer": "yes"}

    with pytest.raises(ModelBehaviorError):
        await run_agent(
            agent(
                FakeClient.scripted([text_chunks("not json")]),
                output_schema=JsonSchemaOutput("Finding", schema, strict=True),
            ),
            "Answer as JSON.",
            context=make_context(),
            settings=RunSettings(),
            max_turns=2,
        )


@pytest.mark.anyio
@pytest.mark.parametrize("delegate_request", ["Check the claim.", "Research evidence. " * 1500])
async def test_delegation_runs_an_isolated_sub_agent(delegate_request: str) -> None:
    delegate_client = FakeClient.scripted([text_chunks("Sub-agent finding.")])
    delegate = agent(delegate_client, id="worker", name="Worker")
    tool = delegation_tool(
        owner_depth=0,
        delegate=delegate,
        tool_name="focused_worker",
        tool_description="Delegate one track.",
        max_turns=3,
        settings=RunSettings(),
    )
    owner_client = FakeClient.scripted(
        [
            tool_call_chunks("focused_worker", json.dumps({"request": delegate_request})),
            text_chunks("Coordinator answer."),
        ]
    )

    result = await run_agent(
        agent(owner_client, tools=[tool]),
        "Delegate the check.",
        context=make_context(),
        settings=RunSettings(),
        max_turns=4,
    )

    assert result.final_output == "Coordinator answer."
    assert result.generated_items[1]["output"] == "Sub-agent finding."
    delegate_messages = delegate_client.requests[0]["messages"]
    assert [message["content"] for message in delegate_messages if message["role"] == "user"] == [
        delegate_request
    ]
    assert "Delegate the check." not in json.dumps(delegate_messages)


@pytest.mark.anyio
async def test_delegation_requires_a_request_string() -> None:
    delegate = agent(FakeClient.scripted([text_chunks("unused")]), id="worker")
    tool = delegation_tool(
        owner_depth=0,
        delegate=delegate,
        tool_name="focused_worker",
        tool_description="Delegate one track.",
        max_turns=2,
        settings=RunSettings(),
    )

    from backend.agents.harness import ToolInvocation

    invocation = ToolInvocation(
        context=make_context(),
        tool_call_id="call-1",
        tool_name="focused_worker",
        agent_name="Agent",
    )

    assert "non-empty 'request'" in await tool.on_invoke_tool(invocation, "{}")
    assert "not valid JSON" in await tool.on_invoke_tool(invocation, "{")


def test_delegation_depth_is_capped_at_two_levels() -> None:
    helper = agent(FakeClient.scripted([[]]), id="helper", name="Helper")
    nested = delegation_tool(
        owner_depth=1,
        delegate=helper,
        tool_name="ask_helper",
        tool_description="Nested helper.",
        max_turns=2,
        settings=RunSettings(),
    )
    worker = agent(FakeClient.scripted([[]]), id="worker", name="Worker", tools=[nested])

    assert MAX_DELEGATION_DEPTH == 2
    with pytest.raises(ValueError, match="cannot delegate further"):
        delegation_tool(
            owner_depth=1,
            delegate=worker,
            tool_name="ask_worker",
            tool_description="Third level.",
            max_turns=2,
            settings=RunSettings(),
        )
    with pytest.raises(ValueError, match="exceeds the maximum"):
        delegation_tool(
            owner_depth=MAX_DELEGATION_DEPTH,
            delegate=helper,
            tool_name="ask_helper",
            tool_description="Fourth level.",
            max_turns=2,
            settings=RunSettings(),
        )


def test_items_translate_into_chat_completions_messages() -> None:
    messages = to_chat_messages(
        [
            {"role": "user", "content": "Find one paper."},
            {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "think"}]},
            {
                "type": "function_call",
                "call_id": "call-1",
                "name": "search",
                "arguments": '{"q":"x"}',
            },
            {
                "type": "function_call",
                "call_id": "call-2",
                "name": "search",
                "arguments": '{"q":"y"}',
            },
            {
                "type": "function_call_output",
                "call_id": "call-1",
                "name": "search",
                "output": {"id": "paper-1"},
            },
            {
                "type": "function_call_output",
                "call_id": "call-2",
                "name": "search",
                "output": "plain",
            },
            {"role": "assistant", "content": "Found."},
        ],
        "Be helpful.",
    )

    assert messages[0] == {"role": "system", "content": "Be helpful."}
    assert messages[1] == {"role": "user", "content": "Find one paper."}
    assert len(messages[2]["tool_calls"]) == 2
    assert messages[3] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": '{"id":"paper-1"}',
    }
    assert messages[4]["content"] == "plain"
    assert messages[5] == {"role": "assistant", "content": "Found."}
    assert "reasoning_content" not in messages[2]


def test_preserved_reasoning_is_replayed_only_for_the_same_model() -> None:
    items = [
        {
            "type": "reasoning",
            "model": "stub-model",
            "content": [{"type": "reasoning_text", "text": "because"}],
        },
        {"role": "assistant", "content": "Answer."},
    ]

    same = to_chat_messages(items, None, preserve_thinking=True, model_name="stub-model")
    other = to_chat_messages(items, None, preserve_thinking=True, model_name="other-model")

    assert same[0]["reasoning_content"] == "because"
    assert "reasoning_content" not in other[0]


def test_structured_output_falls_back_to_json_object_when_tools_are_offered() -> None:
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    definition = agent(
        FakeClient.scripted([[]]),
        output_schema=JsonSchemaOutput("Finding", schema, strict=True),
        tools=[echo_tool([])],
    )

    with_tools = request_parameters(definition, [], definition.tools)
    without_tools = request_parameters(definition, [], [])

    assert with_tools["response_format"] == {"type": "json_object"}
    assert without_tools["response_format"]["type"] == "json_schema"


# --- Regression: assistant preamble alongside tool calls (finding 2) ------------


@pytest.mark.anyio
async def test_assistant_text_alongside_tool_calls_is_kept_and_replayed() -> None:
    client = FakeClient.scripted(
        [
            tool_call_chunks("echo", '{"text":"hi"}', text="Let me check that."),
            text_chunks("Done."),
        ]
    )
    sink = RecordingSink()

    result = await run_agent(
        agent(client, tools=[echo_tool([])]),
        "Use the tool.",
        context=make_context(sink),
        settings=RunSettings(),
        max_turns=4,
    )

    assert [item["type"] for item in result.new_items] == [
        "message_output_item",
        "tool_call_item",
        "tool_call_output_item",
        "message_output_item",
    ]
    assert result.new_items[0]["content"] == "Let me check that."
    assert result.generated_items[0] == {
        "role": "assistant",
        "content": "Let me check that.",
    }
    assert ("run.item", {"name": "message_output_created", "item": result.new_items[0]}) in (
        sink.events
    )
    replayed = client.requests[1]["messages"]
    tool_call_message = next(
        message for message in replayed if message.get("tool_calls")
    )
    assert tool_call_message["content"] == "Let me check that."
    assert (
        sum(
            1
            for message in replayed
            if message["role"] == "assistant" and message.get("content") == "Let me check that."
        )
        == 1
    )


def test_assistant_preamble_folds_into_the_tool_call_message() -> None:
    messages = to_chat_messages(
        [
            {"role": "user", "content": "Find one paper."},
            {"role": "assistant", "content": "Searching now."},
            {
                "type": "function_call",
                "call_id": "call-1",
                "name": "search",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "call_id": "call-1",
                "name": "search",
                "output": "ok",
            },
            {"role": "assistant", "content": "Found it."},
        ],
        None,
    )

    assert [message["role"] for message in messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert messages[1]["content"] == "Searching now."
    assert messages[1]["tool_calls"][0]["function"]["name"] == "search"
    assert messages[3] == {"role": "assistant", "content": "Found it."}


def test_assistant_preamble_keeps_its_reasoning_when_folded() -> None:
    messages = to_chat_messages(
        [
            {
                "type": "reasoning",
                "model": "stub-model",
                "content": [{"type": "reasoning_text", "text": "because"}],
            },
            {"role": "assistant", "content": "Searching now."},
            {
                "type": "function_call",
                "call_id": "call-1",
                "name": "search",
                "arguments": "{}",
            },
        ],
        None,
        preserve_thinking=True,
        model_name="stub-model",
    )

    assert len(messages) == 1
    assert messages[0]["content"] == "Searching now."
    assert messages[0]["reasoning_content"] == "because"


# --- Regression: per-tool single-flight rejection (finding 3) -------------------


@pytest.mark.anyio
async def test_parallel_agents_keep_task_local_actual_model_provenance() -> None:
    context = make_context()
    both_started = asyncio.Event()
    started = []
    observed = {}

    async def record(invocation, _arguments):
        assert invocation.context.metadata["active_model"]["model"] == invocation.agent_name
        started.append(invocation.agent_name)
        if len(started) == 2:
            both_started.set()
        await both_started.wait()
        observed[invocation.agent_name] = current_tool_model_identity()
        return "Recorded."

    definitions = []
    for model in ("model-a", "model-b"):
        client = FakeClient.scripted([tool_call_chunks("record", "{}"), text_chunks("Done.")])
        definitions.append(agent(
            client, id=model, name=model,
            binding=ModelBinding(client=client, model_name=model, provider_kind="ollama"),
            tools=[FunctionTool("record", "Record provenance.", {"type": "object"}, record)],
        ))
    async with asyncio.timeout(2):
        await asyncio.gather(*(
            run_agent(definition, "Record.", context=context, settings=RunSettings(), max_turns=2)
            for definition in definitions
        ))
    assert observed == {
        model: {"model": model, "provider_kind": "ollama"} for model in ("model-a", "model-b")
    }
    assert current_tool_model_identity() is None


@pytest.mark.anyio
@pytest.mark.parametrize("failure_kind", ["ownership", "lease_lost", "cancelled", "policy"])
async def test_fatal_tool_errors_propagate_and_cancel_parallel_siblings(failure_kind) -> None:
    from backend.runs.repository import LeaseOwnershipError
    from backend.runs.service import RunLeaseLost
    from backend.tools.failures import recoverable_tool_invoker

    errors = {
        "ownership": LeaseOwnershipError("Another executor owns this run."),
        "lease_lost": RunLeaseLost("The claim expired."),
        "cancelled": asyncio.CancelledError(),
        "policy": RunPolicyViolation("Budget exhausted.", policy="working_context", detail={}),
    }
    error = errors[failure_kind]
    sibling_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()

    async def slow(_invocation, _arguments):
        sibling_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            sibling_cancelled.set()
            raise

    async def fatal(_invocation, _arguments):
        await sibling_started.wait()
        raise error

    handler = (
        fatal if failure_kind == "policy"
        else recoverable_tool_invoker("fatal", fatal, catalog_id="research.notes.write")
    )
    client = FakeClient.scripted([
        multi_tool_call_chunks([("slow", "{}"), ("fatal", "{}")]),
        text_chunks("Must never be requested."),
    ])
    context = make_context()
    with pytest.raises(type(error)) as raised:
        async with asyncio.timeout(2):
            await run_agent(
                agent(client, tools=[
                    FunctionTool("slow", "In-flight work.", {"type": "object"}, slow),
                    FunctionTool("fatal", "Fatal control failure.", {"type": "object"}, handler),
                ]),
                "Run tools.", context=context, settings=RunSettings(), max_turns=3,
            )
    assert raised.value is error
    assert sibling_cancelled.is_set()
    assert len(client.requests) == 1
    assert not context.metadata.get("_recoverable_tool_failures")


@pytest.mark.anyio
async def test_distinct_serialized_tools_each_run_once_in_the_same_turn() -> None:
    started: list[str] = []

    def single_flight(name: str) -> FunctionTool:
        async def invoke(_invocation, _raw_arguments: str) -> str:
            started.append(name)
            await asyncio.sleep(0)
            return f"{name} receipt"

        return FunctionTool(
            name=name,
            description="Single-flight tool.",
            params_json_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            on_invoke_tool=invoke,
            serialize_calls=True,
        )

    client = FakeClient.scripted(
        [
            multi_tool_call_chunks(
                [
                    ("summarize_paper", "{}"),
                    ("write_notes", "{}"),
                    ("summarize_paper", "{}"),
                ]
            ),
            text_chunks("Done."),
        ]
    )

    result = await run_agent(
        agent(
            client,
            tools=[single_flight("summarize_paper"), single_flight("write_notes")],
        ),
        "Summarize and take notes.",
        context=make_context(),
        settings=RunSettings(),
        max_turns=4,
    )

    outputs = [
        item["output"]
        for item in result.generated_items
        if item.get("type") == "function_call_output"
    ]
    assert sorted(started) == ["summarize_paper", "write_notes"]
    assert outputs[0] == "summarize_paper receipt"
    assert outputs[1] == "write_notes receipt"
    assert "one call at a time" in outputs[2]
    assert "not started" in outputs[2]


# --- Regression: streamed tool-call reconstruction (finding 4) ------------------


def test_fragments_without_index_continue_the_last_seen_call() -> None:
    accumulator = ToolCallAccumulator()

    accumulator.add({"id": "call-a", "function": {"name": "search", "arguments": '{"q":'}})
    accumulator.add({"function": {"arguments": '"graphs"'}})
    accumulator.add({"function": {"arguments": "}"}})

    calls = accumulator.calls()
    assert len(calls) == 1
    assert calls[0].id == "call-a"
    assert calls[0].name == "search"
    assert calls[0].arguments == '{"q":"graphs"}'


def test_fragments_correlate_by_id_when_index_is_missing() -> None:
    accumulator = ToolCallAccumulator()

    accumulator.add({"id": "call-a", "function": {"name": "search", "arguments": "{"}})
    accumulator.add({"id": "call-b", "function": {"name": "read", "arguments": "{"}})
    accumulator.add({"id": "call-a", "function": {"arguments": '"a":1}'}})
    accumulator.add({"id": "call-b", "function": {"arguments": '"b":2}'}})

    calls = accumulator.calls()
    assert [(call.id, call.name, call.arguments) for call in calls] == [
        ("call-a", "search", '{"a":1}'),
        ("call-b", "read", '{"b":2}'),
    ]


def test_index_fragments_never_split_a_single_call() -> None:
    accumulator = ToolCallAccumulator()

    accumulator.add(
        {"index": 0, "id": "call-a", "function": {"name": "search", "arguments": "{"}}
    )
    accumulator.add({"index": 0, "function": {"arguments": "}"}})
    accumulator.add(
        {"index": 1, "id": "call-b", "function": {"name": "read", "arguments": "{}"}}
    )

    calls = accumulator.calls()
    assert [(call.id, call.arguments) for call in calls] == [
        ("call-a", "{}"),
        ("call-b", "{}"),
    ]


def test_unnamed_fragments_do_not_become_phantom_calls() -> None:
    accumulator = ToolCallAccumulator()

    accumulator.add({"function": {"arguments": "{}"}})
    accumulator.add("not-a-fragment")

    assert accumulator.calls() == []


@pytest.mark.anyio
async def test_index_less_fragmented_tool_call_runs_exactly_once() -> None:
    calls: list[str] = []
    client = FakeClient.scripted(
        [
            tool_call_fragments(
                [
                    {"id": "call-a", "type": "function", "function": {"name": "echo"}},
                    {"function": {"arguments": '{"text"'}},
                    {"function": {"arguments": ':"hi"}'}},
                ]
            ),
            text_chunks("Done."),
        ]
    )

    result = await run_agent(
        agent(client, tools=[echo_tool(calls)]),
        "Call the tool once.",
        context=make_context(),
        settings=RunSettings(),
        max_turns=4,
    )

    assert calls == ['{"text":"hi"}']
    assert [item["type"] for item in result.new_items] == [
        "tool_call_item",
        "tool_call_output_item",
        "message_output_item",
    ]


# --- Regression: delegated event isolation (finding 1) -------------------------


def _delegation(delegate: AgentDefinition, tool_name: str) -> FunctionTool:
    return delegation_tool(
        owner_depth=0,
        delegate=delegate,
        tool_name=tool_name,
        tool_description="Delegate one track.",
        max_turns=4,
        settings=RunSettings(),
        hooks=ScholarWeaveRunHooks(),
    )


@pytest.mark.anyio
async def test_parallel_delegations_do_not_contaminate_parent_snapshots_or_usage() -> None:
    worker_a = agent(
        FakeClient.scripted(
            [
                tool_call_chunks("echo", "{}", call_id="worker-a-call"),
                text_chunks("Finding A.", usage={"prompt_tokens": 7, "completion_tokens": 3}),
            ]
        ),
        id="worker-a",
        name="Worker A",
        tools=[echo_tool([])],
    )
    worker_b = agent(
        FakeClient.scripted(
            [text_chunks("Finding B.", usage={"prompt_tokens": 5, "completion_tokens": 2})]
        ),
        id="worker-b",
        name="Worker B",
    )
    coordinator = agent(
        FakeClient.scripted(
            [
                multi_tool_call_chunks(
                    [("ask_worker_a", '{"request":"A"}'), ("ask_worker_b", '{"request":"B"}')],
                    text="Delegating now.",
                    usage={"prompt_tokens": 13, "completion_tokens": 6},
                ),
                text_chunks(
                    "Coordinator answer.",
                    usage={"prompt_tokens": 11, "completion_tokens": 4},
                ),
            ]
        ),
        tools=[
            _delegation(worker_a, "ask_worker_a"),
            _delegation(worker_b, "ask_worker_b"),
        ],
    )
    downstream = RecordingSink()
    buffered = BufferedRunEventSink(downstream, max_delta_chars=1_000, max_delay_seconds=10)
    context = make_context(buffered)

    result = await run_agent(
        coordinator,
        "Delegate both tracks.",
        context=context,
        settings=RunSettings(),
        hooks=ScholarWeaveRunHooks(),
        max_turns=4,
    )
    await buffered.flush()

    snapshots = [
        payload["delta"]
        for event_type, payload in downstream.events
        if event_type == "model.stream"
        and payload.get("snapshot") is True
        and payload.get("raw_type") == "response.output_text.delta"
    ]
    assert snapshots[-1] == "Delegating now.Coordinator answer."
    assert all("Finding A." not in snapshot for snapshot in snapshots)
    assert all("Finding B." not in snapshot for snapshot in snapshots)

    performance = buffered.performance()
    assert performance["model_calls"] == 5
    assert performance["main_model_calls"] == 2
    assert performance["input_tokens"] == 36
    assert performance["output_tokens"] == 15
    assert performance["input_tokens_estimated"] is False
    assert performance["delegated_model_calls"] == 3
    assert performance["delegated_input_tokens"] == 12
    assert performance["delegated_output_tokens"] == 5

    assert result.final_output == "Coordinator answer."
    assert [item["agent_name"] for item in result.new_items] == ["Agent"] * len(
        result.new_items
    )
    # Sub-agent findings reach the parent only as the delegation tool's result.
    assert [
        item["content"]
        for item in result.generated_items
        if item.get("role") == "assistant"
    ] == ["Delegating now.", "Coordinator answer."]
    assert sorted(
        item["output"]
        for item in result.generated_items
        if item.get("type") == "function_call_output"
    ) == ["Finding A.", "Finding B."]

    delegated_messages = [
        payload
        for event_type, payload in downstream.events
        if event_type == "run.item"
        and payload.get("delegated")
        and payload.get("item", {}).get("type") == "message_output_item"
    ]
    assert delegated_messages == []
    assert not any(
        event_type == "model.stream" and payload.get("delegated")
        for event_type, payload in downstream.events
    )

    started = {
        payload["agent_name"]: payload
        for event_type, payload in downstream.events
        if event_type == "agent.started"
    }
    assert set(started) == {"Agent", "Worker A", "Worker B"}
    assert "delegated" not in started["Agent"]
    for name in ("Worker A", "Worker B"):
        assert started[name]["delegated"] is True
        assert started[name]["delegation_depth"] == 1
        assert started[name]["parent_agent_name"] == "Agent"
        assert started[name]["delegate_agent_name"] == name

    delegated_tools = [
        payload
        for event_type, payload in downstream.events
        if event_type == "tool.started" and payload.get("delegated")
    ]
    assert [payload["tool_name"] for payload in delegated_tools] == ["echo"]
    assert delegated_tools[0]["agent_name"] == "Worker A"
    assert delegated_tools[0]["delegate_agent_name"] == "Worker A"
    assert delegated_tools[0]["delegation_depth"] == 1


@pytest.mark.anyio
async def test_delegated_runs_keep_the_run_lease_and_namespaced_completion() -> None:
    lease = object()

    class LeasedSink(RecordingSink):
        def current_lease(self):
            return lease

    seen_leases: list[Any] = []

    async def lease_probe(invocation, _raw_arguments: str) -> str:
        current_lease = getattr(invocation.context.event_sink, "current_lease", None)
        seen_leases.append(current_lease() if current_lease is not None else None)
        return "ok"

    probe = echo_tool([])
    probe.on_invoke_tool = lease_probe
    delegate = agent(
        FakeClient.scripted(
            [
                tool_call_chunks("echo", "{}", call_id="worker-call"),
                text_chunks("Sub-agent finding."),
            ]
        ),
        id="worker",
        name="Worker",
        tools=[probe],
    )
    coordinator = agent(
        FakeClient.scripted(
            [
                tool_call_chunks("ask_worker", '{"request":"Check."}'),
                text_chunks("Coordinator answer."),
            ]
        ),
        tools=[_delegation(delegate, "ask_worker")],
    )
    sink = LeasedSink()
    context = make_context(sink)

    await run_agent(
        coordinator,
        "Delegate the check.",
        context=context,
        settings=RunSettings(),
        hooks=ScholarWeaveRunHooks(),
        max_turns=4,
    )

    assert seen_leases == [lease]
    delegated_completions = [
        payload
        for event_type, payload in sink.events
        if event_type == "agent.completed" and payload.get("delegated")
    ]
    assert [payload["agent_name"] for payload in delegated_completions] == ["Worker"]
    assert delegated_completions[0]["output"] == "Sub-agent finding."
    assert delegated_completions[0]["delegation_depth"] == 1
