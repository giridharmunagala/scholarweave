"""Native harness behaviour: streaming, tool loops, delegation, and policies."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from backend.agents.context import ScholarWeaveContext
from backend.agents.harness import (
    MAX_DELEGATION_DEPTH,
    AgentDefinition,
    FunctionTool,
    JsonSchemaOutput,
    MaxTurnsExceeded,
    ModelBehaviorError,
    ModelBinding,
    ModelSettings,
    RunPolicyViolation,
    RunSettings,
    ToolCallAccumulator,
    delegation_tool,
    request_parameters,
    run_agent,
    run_streamed,
    to_chat_messages,
)
from backend.runs.events import BufferedRunEventSink
from backend.runs.hooks import ScholarWeaveRunHooks
from backend.tests.harness_support import (
    FakeClient,
    multi_tool_call_chunks,
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
async def test_run_can_be_cancelled_mid_stream() -> None:
    started = asyncio.Event()
    client = FakeClient.blocking(started)
    handle = run_streamed(
        agent(client),
        "Answer.",
        context=make_context(),
        settings=RunSettings(),
        max_turns=2,
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
async def test_delegation_runs_an_isolated_sub_agent() -> None:
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
            tool_call_chunks("focused_worker", '{"request":"Check the claim."}'),
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
        "Check the claim."
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
    assert performance["model_calls"] == 2
    assert performance["input_tokens"] == 24
    assert performance["output_tokens"] == 10
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
