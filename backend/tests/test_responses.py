"""Stateless Responses adapter coverage, including recovery and stream failures."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from backend.agents.harness import (
    AgentDefinition, FunctionTool, JsonSchemaOutput, ModelBehaviorError, ModelBinding,
    ModelSettings, RunSettings, model_input_messages, request_parameters, run_agent,
    delegation_tool, stream_model_turn, to_chat_messages,
)
from backend.agents.context_budget import ContextBudgetPolicy
from backend.agents.responses import (
    response_output_text, responses_binding_scope, responses_parameters_from_chat,
    to_responses_input,
)
from backend.core.config import Settings
from backend.tests.test_harness import RecordingSink, make_context


@pytest.fixture
def anyio_backend():
    return "asyncio"


class ResponseStream:
    def __init__(self, events):
        self.events = events
        self.closed = False

    async def __aiter__(self):
        for event in self.events:
            await asyncio.sleep(0)
            yield event

    async def close(self):
        self.closed = True


class ResponsesClient:
    base_url = "http://provider.test/v1/"

    def __init__(self, turns):
        self.turns = turns
        self.requests = []
        self.streams = []
        self.responses = SimpleNamespace(create=self.create)

    async def create(self, **parameters):
        self.requests.append(parameters)
        value = self.turns[len(self.requests) - 1]
        if not parameters.get("stream"):
            return value
        stream = ResponseStream(value)
        self.streams.append(stream)
        return stream


def definition(client, **overrides):
    return AgentDefinition(
        id="main", name="Main", instructions="Be helpful.",
        binding=ModelBinding(
            client=client, model_name="reasoner", provider_kind="openai", api_mode="responses",
            supports_parallel_tool_calls=True, default_reasoning_effort="high",
        ),
        **overrides,
    )


def message(text, identifier="msg_1"):
    return {
        "id": identifier, "type": "message", "role": "assistant", "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def reasoning(identifier="rs_1"):
    return {
        "type": "reasoning", "id": identifier, "encrypted_content": "opaque-encrypted-data",
        "summary": [{"type": "summary_text", "text": "Checking sources."}],
    }


def completed(output, **overrides):
    return {
        "status": "completed", "output": output,
        "usage": {
            "input_tokens": 19, "output_tokens": 7, "total_tokens": 26,
            "input_tokens_details": {"cached_tokens": 11},
        }, **overrides,
    }


def terminal(output, **overrides):
    return {"type": "response.completed", "response": completed(output, **overrides)}


def function_call():
    return {
        "id": "fc_1", "type": "function_call", "call_id": "call_1", "name": "echo",
        "arguments": '{"text":"hi"}', "status": "completed",
    }


def tool(calls):
    async def invoke(_invocation, arguments):
        calls.append(arguments)
        return {"answer": "hello"}

    return FunctionTool(
        name="echo", description="Echo a message.", params_json_schema={
            "type": "object", "properties": {"text": {"type": "string"}},
            "required": ["text"], "additionalProperties": False,
        }, on_invoke_tool=invoke,
    )


def test_responses_parameter_conversion_and_chat_default():
    client = ResponsesClient([])
    agent = definition(client, model_settings=ModelSettings(
        reasoning_effort="low", verbosity="medium", max_tokens=200,
        tool_choice="echo", parallel_tool_calls=True,
    ), output_schema=JsonSchemaOutput(
        "answer", {"type": "object", "properties": {}, "additionalProperties": False}, strict=True,
    ))
    tools = [tool([])]
    parameters = request_parameters(agent, [{"role": "user", "content": "hello"}], tools)
    assert parameters["store"] is False
    assert parameters["include"] == ["reasoning.encrypted_content"]
    assert parameters["input"] == [{
        "type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}],
    }]
    assert parameters["reasoning"] == {"effort": "low"}
    assert parameters["text"]["verbosity"] == "medium"
    assert parameters["text"]["format"]["name"] == "answer"
    assert parameters["text"]["format"]["strict"] is True
    assert parameters["max_output_tokens"] == 200
    assert parameters["tools"][0] == {
        "type": "function", "name": "echo", "description": tools[0].description,
        "parameters": tools[0].params_json_schema, "strict": True,
    }
    assert parameters["tool_choice"] == {"type": "function", "name": "echo"}
    assert parameters["parallel_tool_calls"] is True
    assert not {"messages", "reasoning_effort", "verbosity", "response_format", "stream_options",
                "previous_response_id", "conversation"} & parameters.keys()
    overridden = replace(agent, binding=replace(agent.binding, tool_reasoning_effort="none"))
    assert request_parameters(overridden, [], tools)["reasoning"] == {"effort": "none"}
    chat_agent = replace(agent, binding=replace(agent.binding, api_mode="chat_completions"))
    assert "messages" in request_parameters(chat_agent, [], tools)


@pytest.mark.parametrize("name", ["frequency_penalty", "presence_penalty", "top_p"])
def test_unsupported_settings_fail_explicitly(name):
    agent = definition(None, model_settings=ModelSettings(**{name: 0.1}))
    with pytest.raises(ModelBehaviorError, match=name):
        request_parameters(agent, [], [])


def test_chat_input_images_and_tools_convert_without_chat_only_fields():
    agent = definition(ResponsesClient([]))
    converted = to_responses_input([
        {"role": "user", "content": [
            {"type": "text", "text": "Read this."},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc", "detail": "high"}},
        ]},
        {"role": "assistant", "content": "Looking.", "reasoning_content": "private",
         "tool_calls": [{"id": "call_1", "function": {"name": "echo", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": {"ok": True}},
    ], agent.binding)
    assert converted[0]["content"][1] == {
        "type": "input_image", "image_url": "data:image/png;base64,abc", "detail": "high",
    }
    assert converted[1] == {
        "type": "message", "role": "assistant", "content": [{"type": "input_text", "text": "Looking."}],
    }
    assert converted[2]["type"] == "function_call"
    assert converted[3] == {"type": "function_call_output", "call_id": "call_1", "output": '{"ok":true}'}


def test_generation_kwargs_use_shared_conversion():
    binding = definition(ResponsesClient([])).binding
    converted = responses_parameters_from_chat({
        "model": "reasoner", "messages": [{"role": "user", "content": "Extract."}],
        "temperature": 0.2, "reasoning_effort": "high", "max_completion_tokens": 200,
        "stream": True, "stream_options": {"include_usage": True},
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "result", "schema": {"type": "object"}, "strict": True,
        }},
    }, binding)
    assert converted["reasoning"] == {"effort": "high"}
    assert converted["text"]["format"] == {
        "type": "json_schema", "name": "result", "schema": {"type": "object"}, "strict": True,
    }
    assert converted["max_output_tokens"] == 200
    assert converted["stream"] is True
    assert "stream_options" not in converted
    with pytest.raises(ModelBehaviorError, match="frequency_penalty"):
        responses_parameters_from_chat({"frequency_penalty": 0.5}, binding)
    with pytest.raises(ModelBehaviorError, match="hosted tools"):
        responses_parameters_from_chat({"tools": [{"type": "web_search"}]}, binding)


@pytest.mark.anyio
async def test_responses_delegation_uses_worker_binding_and_isolates_history():
    worker_client = ResponsesClient([[terminal([reasoning(), message("Worker finding.")])]])
    worker = replace(definition(worker_client), id="worker", name="Worker")
    delegate = delegation_tool(
        owner_depth=0, delegate=worker, tool_name="worker", tool_description="Check sources.",
        max_turns=2, settings=RunSettings(),
    )
    owner_client = ResponsesClient([
        [terminal([{**function_call(), "name": "worker",
                    "arguments": '{"request":"Check this one source."}'}])],
        [terminal([message("Coordinator answer.")])],
    ])
    result = await run_agent(
        definition(owner_client, tools=[delegate]), "Parent-private details.",
        context=make_context(RecordingSink()), settings=RunSettings(),
    )
    assert result.final_output == "Coordinator answer."
    assert len(worker_client.requests) == 1
    assert "Check this one source." in json.dumps(worker_client.requests[0]["input"])
    assert "Parent-private details." not in json.dumps(worker_client.requests[0]["input"])
    assert owner_client.requests[0]["reasoning"] == {"effort": "high"}
    assert worker_client.requests[0]["reasoning"] == {"effort": "high"}
    assert not any(item.get("type") == "reasoning" for item in owner_client.requests[1]["input"])


@pytest.mark.anyio
async def test_encrypted_reasoning_survives_tools_new_turn_and_recovered_history():
    calls = []
    client = ResponsesClient([
        [terminal([reasoning(), function_call()])],
        [terminal([reasoning("rs_2"), message("hello")])],
        [terminal([message("welcome")])],
    ])
    agent = definition(client, tools=[tool(calls)])
    sink = RecordingSink()
    result = await run_agent(agent, "Echo.", context=make_context(sink), settings=RunSettings())
    assert result.final_output == "hello"
    assert calls == ['{"text":"hi"}']
    replayed = client.requests[1]["input"]
    encrypted = next(item for item in replayed if item.get("type") == "reasoning")
    assert encrypted == reasoning()
    assert "_scholarweave_binding" not in encrypted
    output = next(item for item in replayed if item.get("type") == "function_call_output")
    assert output["output"] == '{"answer":"hello"}'
    assert result.usage.input_tokens == 38
    recovered = json.loads(json.dumps(result.to_input_list()))
    recovered.append({"role": "user", "content": "Again."})
    await run_agent(agent, recovered, context=make_context(), settings=RunSettings())
    assert [item["id"] for item in client.requests[2]["input"] if item.get("type") == "reasoning"] == [
        "rs_1", "rs_2",
    ]
    assert all(stream.closed for stream in client.streams)
    telemetry = [payload for event, payload in sink.events if event == "model.telemetry"]
    assert telemetry[0]["cached_input_tokens"] == 11
    assert telemetry[0]["usage_complete"] is True
    assert telemetry[0]["completed"] is True


def test_reasoning_scope_prevents_cross_model_endpoint_and_chat_replay():
    agent = definition(ResponsesClient([]))
    item = {**reasoning(), "_scholarweave_binding": responses_binding_scope(agent.binding)}
    assert to_responses_input([item], agent.binding) == [reasoning()]
    assert to_responses_input([item], replace(agent.binding, model_name="other")) == []
    other_client = ResponsesClient([])
    other_client.base_url = "http://other.test/v1/"
    assert to_responses_input([item], replace(agent.binding, client=other_client)) == []
    assert to_chat_messages([item, {"role": "assistant", "content": "Answer."}], None,
                            preserve_thinking=True, model_name="reasoner") == [
        {"role": "assistant", "content": "Answer."},
    ]


def test_reasoning_scope_prevents_cross_profile_and_rotated_credential_replay():
    client = ResponsesClient([])
    client.api_key = "first-test-credential"
    binding = replace(definition(client).binding, provider_profile_id="profile-one")
    scope = responses_binding_scope(binding)
    item = {**reasoning(), "_scholarweave_binding": scope}
    assert to_responses_input([item], binding) == [reasoning()]
    assert to_responses_input([item], replace(binding, provider_profile_id="profile-two")) == []
    assert "first-test-credential" not in json.dumps(scope)
    assert len(scope["credential_fingerprint"]) == 64
    client.api_key = "rotated-test-credential"
    assert to_responses_input([item], binding) == []
    assert responses_binding_scope(binding)["credential_fingerprint"] != scope["credential_fingerprint"]


def test_fake_client_without_credentials_has_stable_replay_scope():
    first = definition(ResponsesClient([])).binding
    second = definition(ResponsesClient([])).binding
    item = {**reasoning(), "_scholarweave_binding": responses_binding_scope(first)}
    assert to_responses_input([item], second) == [reasoning()]
    assert responses_binding_scope(first)["credential_fingerprint"] == ""


@pytest.mark.anyio
async def test_stream_deltas_and_final_fallback_are_not_duplicated():
    final_call = function_call()
    client = ResponsesClient([[
        {"type": "response.output_item.added", "output_index": 1,
         "item": {**final_call, "arguments": ""}},
        {"type": "response.reasoning_summary_text.delta", "output_index": 0,
         "summary_index": 0, "delta": "Checking "},
        {"type": "response.function_call_arguments.delta", "output_index": 1,
         "delta": '{"text":'},
        {"type": "response.output_text.delta", "output_index": 2,
         "content_index": 0, "delta": "hel"},
        terminal([reasoning(), final_call, message("hello")]),
    ]])
    sink = RecordingSink()
    turn = await stream_model_turn(definition(client), [], [tool([])], make_context(sink))
    assert turn.text == "hello"
    assert turn.reasoning == "Checking sources."
    assert turn.tool_calls[0].id == "call_1"
    assert turn.tool_calls[0].arguments == '{"text":"hi"}'
    for kind, text in [
        ("response.output_text.delta", "hello"),
        ("response.reasoning_summary_text.delta", "Checking sources."),
        ("response.function_call_arguments.delta", '{"text":"hi"}'),
    ]:
        assert "".join(payload["delta"] for event, payload in sink.events
                       if event == "model.stream" and payload.get("raw_type") == kind) == text
    assert [payload["raw_type"] for event, payload in sink.events if event == "model.stream"].count(
        "response.completed",
    ) == 1


@pytest.mark.anyio
async def test_completed_items_supply_parallel_calls_when_argument_deltas_are_absent():
    first = function_call()
    second = {**first, "id": "fc_2", "call_id": "call_2", "arguments": '{"text":"second"}'}
    client = ResponsesClient([[
        {"type": "response.output_item.done", "output_index": 0, "item": first},
        {"type": "response.function_call_arguments.done", "output_index": 0,
         "arguments": first["arguments"]},
        terminal([first, second]),
    ]])
    sink = RecordingSink()
    turn = await stream_model_turn(definition(client), [], [tool([])], make_context(sink))
    assert [(call.id, call.arguments) for call in turn.tool_calls] == [
        ("call_1", '{"text":"hi"}'), ("call_2", '{"text":"second"}'),
    ]
    assert [payload["delta"] for event, payload in sink.events
            if event == "model.stream"
            and payload.get("raw_type") == "response.function_call_arguments.delta"] == [
        '{"text":"hi"}', '{"text":"second"}',
    ]


@pytest.mark.anyio
async def test_encrypted_reasoning_from_done_item_is_not_lost_in_final_fallback():
    item = reasoning()
    client = ResponsesClient([[
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        terminal([{**item, "encrypted_content": None}, message("Answer.")]),
    ]])
    turn = await stream_model_turn(definition(client), [], [], make_context())
    assert turn.reasoning_items[0]["encrypted_content"] == "opaque-encrypted-data"


@pytest.mark.anyio
@pytest.mark.parametrize("event,match", [
    ({"type": "response.failed", "response": completed(
        [function_call()], status="failed", error={"message": "provider exploded"},
    )}, "provider exploded"),
    ({"type": "response.incomplete", "response": completed(
        [function_call()], status="incomplete", incomplete_details={"reason": "max_output_tokens"},
    )}, "max_output_tokens"),
    ({"type": "response.incomplete", "response": completed(
        [], status="incomplete", incomplete_details={"reason": "content_filter"},
    )}, "content_filter"),
    ({"type": "error", "code": "bad_request", "message": "broken"}, "broken"),
    ({"type": "response.refusal.delta", "delta": "No."}, "refused"),
    (terminal([{"type": "web_search_call"}]), "hosted tools"),
])
async def test_failed_streams_close_and_never_execute_tools(event, match):
    calls = []
    client = ResponsesClient([[event]])
    sink = RecordingSink()
    with pytest.raises(ModelBehaviorError, match=match):
        await run_agent(definition(client, tools=[tool(calls)]), "Echo.",
                        context=make_context(sink), settings=RunSettings())
    assert not calls
    assert client.streams[0].closed
    assert not any(payload.get("raw_type") == "response.completed" for _, payload in sink.events)
    assert next(payload for kind, payload in sink.events if kind == "model.telemetry")["completed"] is False


@pytest.mark.anyio
async def test_stream_without_terminal_event_is_an_error():
    client = ResponsesClient([[{"type": "response.output_text.delta", "delta": "Partial"}]])
    with pytest.raises(ModelBehaviorError, match="without a terminal"):
        await stream_model_turn(definition(client), [], [], make_context())
    assert client.streams[0].closed


@pytest.mark.anyio
async def test_cancellation_closes_response_stream():
    started = asyncio.Event()

    class BlockingStream(ResponseStream):
        async def __aiter__(self):
            started.set()
            await asyncio.Event().wait()
            yield {}

    stream = BlockingStream([])
    client = ResponsesClient([])

    async def create(**parameters):
        return stream

    client.responses.create = create
    task = asyncio.create_task(stream_model_turn(definition(client), [], [], make_context()))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed


@pytest.mark.anyio
async def test_responses_context_preparation_retains_scoped_reasoning():
    agent = definition(ResponsesClient([]))
    item = {**reasoning(), "_scholarweave_binding": responses_binding_scope(agent.binding)}
    outcome = await ContextBudgetPolicy(Settings()).prepare(
        agent, [{"role": "user", "content": "Question"}, item], agent.instructions,
        make_context(), turn_index=1,
    )
    request = request_parameters(agent, model_input_messages(
        agent, outcome.items, outcome.instructions,
    ), [], stream=False)
    assert reasoning() in request["input"]


@pytest.mark.anyio
async def test_context_summarizer_uses_nonstream_responses():
    client = ResponsesClient([completed([message("Concise checkpoint.")])])
    agent = definition(client)
    checkpoint: dict[str, Any] = {"checkpoint_id": "cp", "caveats": []}
    summary = await ContextBudgetPolicy(Settings())._request_summary(
        agent, agent.binding, [{"role": "user", "content": "Old history"}], checkpoint,
        context_window_tokens=32000, max_tokens=500, context=make_context(),
    )
    assert summary == "Concise checkpoint."
    assert "input" in client.requests[0]
    assert "stream" not in client.requests[0]


@pytest.mark.parametrize("status", ["failed", "incomplete", "in_progress", None])
def test_nonstream_output_checks_terminal_status(status):
    with pytest.raises(ModelBehaviorError):
        response_output_text(completed([message("Partial")], status=status))


def test_nonstream_refusal_is_not_silent_success():
    with pytest.raises(ModelBehaviorError, match="refused"):
        response_output_text(completed([{
            "type": "message", "content": [{"type": "refusal", "refusal": "Cannot answer"}],
        }]))
