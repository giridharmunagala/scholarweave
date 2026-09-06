"""Native agent harness: a streamed Chat Completions model/tool loop.

ScholarWeave talks to every provider through ``POST /v1/chat/completions`` using the
official ``openai`` client.  This module owns the deterministic loop that turns a
compiled agent definition into streamed model output, executed tools, delegated
sub-agents, and the run items the rest of the product persists.

The canonical in-memory conversation item shape is provider-neutral:

* ``{"role": "user" | "assistant" | "system" | "developer", "content": str | list}``
* ``{"type": "function_call", "call_id": str, "name": str, "arguments": str}``
* ``{"type": "function_call_output", "call_id": str, "output": Any}``
* ``{"type": "reasoning", "content": [{"type": "reasoning_text", "text": str}]}``

Items become Chat Completions messages only at the request boundary.  Every run
event is published through :meth:`ScholarWeaveContext.emit`, so the run service can
swap the active sink (persisted, buffered, or transient) without the harness caring.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from contextvars import ContextVar
from typing import Any, Literal, Protocol

from jsonschema import Draft202012Validator, ValidationError as JsonSchemaValidationError
from openai import AsyncOpenAI, OpenAIError

from backend.agents.context import ScholarWeaveContext
from backend.runs.repository import LeaseOwnershipError
from backend.utils import to_jsonable

MessageRole = Literal["user", "assistant", "system", "developer", "tool"]
ConversationItem = dict[str, Any]
RunInputItems = list[ConversationItem]
RunInput = str | RunInputItems
MAX_DELEGATION_DEPTH = 2
_tool_model_identity: ContextVar[dict[str, str] | None] = ContextVar(
    "scholarweave_tool_model_identity", default=None
)


def current_tool_model_identity() -> dict[str, str] | None:
    """Return task-local provenance, including when parallel delegates share metadata."""
    identity = _tool_model_identity.get()
    return dict(identity) if identity is not None else None


class HarnessError(RuntimeError):
    """Base error for problems raised by the native agent loop."""


class ModelBehaviorError(HarnessError):
    """The model or provider returned output the harness cannot use."""


class MaxTurnsExceeded(HarnessError):
    """The agent used its whole model-turn budget without a final answer."""

    def __init__(self, message: str, run_data: "RunResult") -> None:
        super().__init__(message)
        self.run_data = run_data


class RunPolicyViolation(HarnessError):
    """A deterministic run policy (such as a size limit) rejected the run."""

    def __init__(self, message: str, *, policy: str, detail: dict[str, Any]) -> None:
        super().__init__(message)
        self.policy = policy
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """The per-call context handed to a tool handler."""

    context: ScholarWeaveContext
    tool_call_id: str
    tool_name: str
    agent_name: str


ToolHandler = Callable[[ToolInvocation, str], Awaitable[Any]]


@dataclass(slots=True)
class FunctionTool:
    name: str
    description: str
    params_json_schema: dict[str, Any]
    on_invoke_tool: ToolHandler
    strict_json_schema: bool = True
    is_enabled: Callable[[ScholarWeaveContext], bool] | None = None
    serialize_calls: bool = False
    is_delegation: bool = False

    def enabled_for(self, context: ScholarWeaveContext) -> bool:
        return self.is_enabled is None or bool(self.is_enabled(context))


@dataclass(frozen=True, slots=True)
class ModelSettings:
    temperature: float | None = None
    top_p: float | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    tool_choice: str | None = None
    parallel_tool_calls: bool | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    verbosity: str | None = None


class JsonSchemaOutput:
    """Validate model output against a persisted JSON Schema document."""

    def __init__(self, schema_name: str, schema: dict[str, Any], *, strict: bool) -> None:
        Draft202012Validator.check_schema(schema)
        self._schema_name = schema_name
        self._schema = schema
        self._strict = strict
        self._validator = Draft202012Validator(schema)

    @property
    def name(self) -> str:
        return self._schema_name

    @property
    def strict(self) -> bool:
        return self._strict

    def json_schema(self) -> dict[str, Any]:
        return self._schema

    def validate_json(self, json_str: str) -> Any:
        payload = strip_json_fence(json_str)
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            detail = (
                "the model returned an empty response"
                if not payload
                else f"the response started with {payload[:160]!r}"
            )
            raise ModelBehaviorError(
                f"Model returned invalid JSON for structured output "
                f"'{self._schema_name}': {exc.msg}; {detail}."
            ) from exc
        try:
            self._validator.validate(value)
        except JsonSchemaValidationError as exc:
            raise ModelBehaviorError(
                f"Structured output '{self._schema_name}' failed JSON Schema validation: "
                f"{exc.message}"
            ) from exc
        return value


def strip_json_fence(value: str) -> str:
    stripped = value.strip()
    if not stripped.startswith("```") or not stripped.endswith("```"):
        return stripped
    first_newline = stripped.find("\n")
    if first_newline == -1:
        return stripped
    return stripped[first_newline + 1 : -3].strip()


@dataclass(frozen=True, slots=True)
class ModelBinding:
    """Everything the harness needs to reach one provider model."""

    client: AsyncOpenAI
    model_name: str
    provider_kind: str
    supports_parallel_tool_calls: bool = False
    preserve_thinking: bool = False
    context_window_tokens: int | None = None
    local_inference: bool = False


@dataclass(slots=True)
class AgentDefinition:
    id: str
    name: str
    instructions: str
    binding: ModelBinding
    model_settings: ModelSettings = field(default_factory=ModelSettings)
    tools: list[FunctionTool] = field(default_factory=list)
    output_schema: JsonSchemaOutput | None = None
    description: str | None = None
    stop_on_first_tool: bool = False

    def enabled_tools(self, context: ScholarWeaveContext) -> list[FunctionTool]:
        return [tool for tool in self.tools if tool.enabled_for(context)]


@dataclass(frozen=True, slots=True)
class RunSettings:
    max_turns: int = 10
    max_tool_concurrency: int | None = None
    max_input_characters: int | None = None
    max_output_characters: int | None = None
    workflow_name: str = "ScholarWeave"


class RunHooks(Protocol):
    async def on_agent_start(
        self,
        context: ScholarWeaveContext,
        agent: "AgentDefinition",
    ) -> None: ...

    async def on_agent_end(
        self,
        context: ScholarWeaveContext,
        agent: "AgentDefinition",
        output: Any,
    ) -> None: ...

    async def on_llm_start(
        self,
        context: ScholarWeaveContext,
        agent: "AgentDefinition",
        instructions: str | None,
        input_items: RunInputItems,
    ) -> None: ...

    async def on_llm_end(
        self,
        context: ScholarWeaveContext,
        agent: "AgentDefinition",
        usage: dict[str, Any],
    ) -> None: ...

    async def on_tool_start(
        self,
        context: ScholarWeaveContext,
        agent: "AgentDefinition",
        tool_name: str,
        tool_call_id: str,
    ) -> None: ...

    async def on_tool_end(
        self,
        context: ScholarWeaveContext,
        agent: "AgentDefinition",
        tool_name: str,
        tool_call_id: str,
        result: Any,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class PreparedInput:
    """The model input for one turn, plus an optional new durable working set."""

    items: RunInputItems
    instructions: str
    working_items: RunInputItems | None = None
    response_max_tokens: int | None = None


class ContextPolicy(Protocol):
    """Bounds, compacts, and augments model input before each request."""

    async def prepare(
        self,
        agent: "AgentDefinition",
        items: RunInputItems,
        instructions: str,
        context: ScholarWeaveContext,
        *,
        turn_index: int,
    ) -> PreparedInput: ...


@dataclass(slots=True)
class Usage:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.requests += other.requests
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.total_tokens += other.total_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(slots=True)
class RunResult:
    """Audit output plus the last bounded request and its uncovered generated tail."""

    input: RunInput
    new_items: list[dict[str, Any]]
    generated_items: RunInputItems
    final_output: Any
    last_agent_name: str
    usage: Usage
    working_items: RunInputItems | None = None
    working_snapshot_items: RunInputItems | None = None
    working_snapshot_generated_count: int = 0

    def to_input_list(self) -> RunInputItems:
        return (
            list(self.working_items)
            if self.working_items is not None
            else [*normalize_run_input(self.input), *self.generated_items]
        )


def message_item(role: MessageRole, content: str) -> ConversationItem:
    return {"role": role, "content": content}


def function_call_item(call_id: str, name: str, arguments: str) -> ConversationItem:
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
    }


def function_call_output_item(call_id: str, name: str, output: Any) -> ConversationItem:
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "name": name,
        "output": output,
    }


def reasoning_item(text: str, model_name: str) -> ConversationItem:
    return {
        "type": "reasoning",
        "model": model_name,
        "content": [{"type": "reasoning_text", "text": text}],
    }


def normalize_run_input(value: RunInput) -> RunInputItems:
    if isinstance(value, str):
        return [message_item("user", value)]
    return [dict(item) if isinstance(item, dict) else item for item in value]


def item_text(item: Any) -> str | None:
    raw = to_jsonable(item)
    if not isinstance(raw, dict):
        return None
    content = raw.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts = [
        part["text"]
        for part in content
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ]
    return "\n".join(parts) if parts else None


def serialized_characters(value: Any) -> int:
    return len(json.dumps(to_jsonable(value), ensure_ascii=False, separators=(",", ":")))


def _tool_output_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    return json.dumps(to_jsonable(output), ensure_ascii=False, separators=(",", ":"))


def to_chat_messages(
    items: Sequence[ConversationItem],
    instructions: str | None,
    *,
    preserve_thinking: bool = False,
    model_name: str | None = None,
) -> list[dict[str, Any]]:
    """Translate canonical items into Chat Completions messages.

    An assistant message that directly precedes tool calls is folded into the
    tool-call message's ``content`` so the model sees its own preamble exactly once
    and in the position providers expect.
    """
    messages: list[dict[str, Any]] = []
    if instructions:
        messages.append({"role": "system", "content": instructions})
    pending_reasoning: str | None = None
    pending_assistant: dict[str, Any] | None = None

    def flush_assistant() -> None:
        nonlocal pending_assistant
        if pending_assistant is not None:
            messages.append(pending_assistant)
            pending_assistant = None

    index = 0
    while index < len(items):
        item = items[index]
        if not isinstance(item, dict):
            index += 1
            continue
        item_type = str(item.get("type") or "")
        if item_type == "reasoning":
            if preserve_thinking and item.get("model") in (None, model_name):
                pending_reasoning = item_text(item)
            index += 1
            continue
        if item_type == "function_call":
            tool_calls: list[dict[str, Any]] = []
            while index < len(items):
                candidate = items[index]
                if not isinstance(candidate, dict) or candidate.get("type") != "function_call":
                    break
                tool_calls.append(
                    {
                        "id": str(candidate.get("call_id") or candidate.get("id") or ""),
                        "type": "function",
                        "function": {
                            "name": str(candidate.get("name") or ""),
                            "arguments": str(candidate.get("arguments") or "{}"),
                        },
                    }
                )
                index += 1
            preamble = pending_assistant
            pending_assistant = None
            content = preamble.get("content") if preamble is not None else None
            call_message: dict[str, Any] = {
                "role": "assistant",
                "content": content if isinstance(content, str) and content else None,
                "tool_calls": tool_calls,
            }
            inherited_reasoning = (
                preamble.get("reasoning_content") if preamble is not None else None
            )
            if pending_reasoning or inherited_reasoning:
                call_message["reasoning_content"] = pending_reasoning or inherited_reasoning
                pending_reasoning = None
            messages.append(call_message)
            continue
        if item_type == "function_call_output":
            flush_assistant()
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(item.get("call_id") or ""),
                    "content": _tool_output_text(item.get("output")),
                }
            )
            index += 1
            continue
        role = str(item.get("role") or "")
        if role == "tool":
            flush_assistant()
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(
                        item.get("tool_call_id") or item.get("call_id") or ""
                    ),
                    "content": _tool_output_text(item.get("content")),
                }
            )
            index += 1
            continue
        if role in {"user", "assistant", "system", "developer"}:
            flush_assistant()
            content = item.get("content")
            text = content if isinstance(content, (str, list)) else item_text(item) or ""
            message: dict[str, Any] = {
                "role": "system" if role == "developer" else role,
                "content": text,
            }
            if role == "assistant" and pending_reasoning:
                message["reasoning_content"] = pending_reasoning
                pending_reasoning = None
            if role == "assistant":
                pending_assistant = message
            else:
                messages.append(message)
        index += 1
    flush_assistant()
    return messages


def tool_payload(tool: FunctionTool) -> dict[str, Any]:
    function: dict[str, Any] = {
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.params_json_schema,
    }
    if tool.strict_json_schema:
        function["strict"] = True
    return {"type": "function", "function": function}


@dataclass(slots=True)
class StreamedToolCall:
    index: int
    id: str = ""
    name: str = ""
    arguments: str = ""


class ToolCallAccumulator:
    """Reassembles streamed tool-call fragments without inventing phantom calls.

    Providers vary: some send a stable ``index`` on every fragment, some send
    ``index`` only on the opening fragment, and some send neither once a call has
    been announced by ``id``. A fragment is therefore correlated by ``index`` first,
    then by ``id``, and finally treated as a continuation of the last-seen call. A
    fragment never opens a new call unless it carries a new ``index`` or ``id``.
    """

    def __init__(self) -> None:
        self._order: list[StreamedToolCall] = []
        self._by_index: dict[int, StreamedToolCall] = {}
        self._by_id: dict[str, StreamedToolCall] = {}
        self._last: StreamedToolCall | None = None

    def add(self, raw_call: Any) -> None:
        if not isinstance(raw_call, dict):
            return
        raw_index = raw_call.get("index")
        index = (
            int(raw_index)
            if isinstance(raw_index, int) and not isinstance(raw_index, bool)
            else None
        )
        call_id = str(raw_call.get("id") or "")
        call = self._resolve(index, call_id)
        if call_id and call.id != call_id:
            call.id = call_id
            self._by_id[call_id] = call
        if index is not None:
            self._by_index.setdefault(index, call)
        function = raw_call.get("function") or {}
        if function.get("name"):
            call.name = str(function["name"])
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            call.arguments += arguments
        self._last = call

    def _resolve(self, index: int | None, call_id: str) -> StreamedToolCall:
        if index is not None:
            existing = self._by_index.get(index)
            if existing is not None:
                return existing
            if call_id and call_id in self._by_id:
                return self._by_id[call_id]
            return self._open(index)
        if call_id:
            existing = self._by_id.get(call_id)
            return existing if existing is not None else self._open(None)
        if self._last is not None:
            return self._last
        return self._open(None)

    def _open(self, index: int | None) -> StreamedToolCall:
        call = StreamedToolCall(index=index if index is not None else len(self._order))
        self._order.append(call)
        return call

    def calls(self) -> list[StreamedToolCall]:
        """Return complete calls in arrival order, dropping unusable fragments."""
        return [call for call in self._order if call.name]


@dataclass(slots=True)
class ModelTurn:
    """One complete model response."""

    text: str
    reasoning: str
    tool_calls: list[StreamedToolCall]
    usage: Usage
    finish_reason: str | None


def usage_from_payload(payload: Any) -> Usage:
    raw = to_jsonable(payload)
    if not isinstance(raw, dict):
        return Usage(requests=1)
    prompt = raw.get("prompt_tokens") or raw.get("input_tokens") or 0
    completion = raw.get("completion_tokens") or raw.get("output_tokens") or 0
    total = raw.get("total_tokens") or (int(prompt or 0) + int(completion or 0))
    return Usage(
        requests=1,
        input_tokens=int(prompt or 0),
        output_tokens=int(completion or 0),
        total_tokens=int(total or 0),
    )


def request_parameters(
    agent: AgentDefinition,
    messages: list[dict[str, Any]],
    tools: list[FunctionTool],
    *,
    stream: bool = True,
) -> dict[str, Any]:
    settings = agent.model_settings
    parameters: dict[str, Any] = {
        "model": agent.binding.model_name,
        "messages": messages,
    }
    if stream:
        parameters["stream"] = True
        parameters["stream_options"] = {"include_usage": True}
    if settings.temperature is not None:
        parameters["temperature"] = settings.temperature
    if settings.top_p is not None:
        parameters["top_p"] = settings.top_p
    if settings.frequency_penalty is not None:
        parameters["frequency_penalty"] = settings.frequency_penalty
    if settings.presence_penalty is not None:
        parameters["presence_penalty"] = settings.presence_penalty
    if settings.max_tokens is not None:
        parameters["max_tokens"] = settings.max_tokens
    if settings.reasoning_effort is not None:
        parameters["reasoning_effort"] = settings.reasoning_effort
    if settings.verbosity is not None:
        parameters["verbosity"] = settings.verbosity
    if tools:
        parameters["tools"] = [tool_payload(tool) for tool in tools]
        if settings.tool_choice is not None:
            parameters["tool_choice"] = settings.tool_choice
        if (
            settings.parallel_tool_calls is not None
            and agent.binding.supports_parallel_tool_calls
        ):
            parameters["parallel_tool_calls"] = settings.parallel_tool_calls
    if agent.output_schema is not None:
        # Several OpenAI-compatible llama.cpp servers cannot compose a strict
        # json_schema response grammar with their tool-call grammar. Constrain JSON
        # syntax only in that case; the harness still validates the returned value.
        parameters["response_format"] = (
            {"type": "json_object"}
            if tools
            else {
                "type": "json_schema",
                "json_schema": {
                    "name": agent.output_schema.name,
                    "schema": agent.output_schema.json_schema(),
                    "strict": agent.output_schema.strict,
                },
            }
        )
    return parameters


async def stream_model_turn(
    agent: AgentDefinition,
    messages: list[dict[str, Any]],
    tools: list[FunctionTool],
    context: ScholarWeaveContext,
) -> ModelTurn:
    """Stream one Chat Completions response, emitting normalized model events."""
    parameters = request_parameters(agent, messages, tools)
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    calls = ToolCallAccumulator()
    usage = Usage(requests=1)
    finish_reason: str | None = None
    await context.emit("model.stream", {"raw_type": "response.created"})
    stream = await agent.binding.client.chat.completions.create(**parameters)
    try:
        async for chunk in stream:
            raw = chunk.model_dump() if hasattr(chunk, "model_dump") else dict(chunk)
            if raw.get("usage"):
                usage = usage_from_payload(raw["usage"])
            for choice in raw.get("choices") or []:
                finish_reason = choice.get("finish_reason") or finish_reason
                delta = choice.get("delta") or {}
                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if isinstance(reasoning, str) and reasoning:
                    reasoning_parts.append(reasoning)
                    await context.emit(
                        "model.stream",
                        {
                            "raw_type": "response.reasoning_summary_text.delta",
                            "delta": reasoning,
                        },
                    )
                content = delta.get("content")
                if isinstance(content, str) and content:
                    text_parts.append(content)
                    await context.emit(
                        "model.stream",
                        {"raw_type": "response.output_text.delta", "delta": content},
                    )
                for raw_call in delta.get("tool_calls") or []:
                    calls.add(raw_call)
                    arguments = (raw_call.get("function") or {}).get("arguments")
                    if isinstance(arguments, str) and arguments:
                        await context.emit(
                            "model.stream",
                            {"raw_type": "response.function_call_arguments.delta", "delta": arguments},
                        )
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            outcome = close()
            if asyncio.iscoroutine(outcome):
                await outcome
    await context.emit(
        "model.stream",
        {
            "raw_type": "response.completed",
            "finish_reason": finish_reason,
            "output_tokens": usage.output_tokens,
        },
    )
    return ModelTurn(
        text="".join(text_parts),
        reasoning="".join(reasoning_parts),
        tool_calls=calls.calls(),
        usage=usage,
        finish_reason=finish_reason,
    )


class AgentRunner:
    """Runs one agent's model/tool loop against the streaming Chat Completions API."""

    def __init__(
        self,
        agent: AgentDefinition,
        *,
        context: ScholarWeaveContext,
        settings: RunSettings,
        hooks: RunHooks | None = None,
        context_policy: ContextPolicy | None = None,
        depth: int = 0,
    ) -> None:
        self._agent = agent
        self._context = context
        self._settings = settings
        self._hooks = hooks
        self._context_policy = context_policy
        self._depth = depth
        self._working_snapshot: RunInputItems | None = None
        self._snapshot_generated_count = 0

    async def run(self, input_value: RunInput, *, max_turns: int) -> RunResult:
        history = normalize_run_input(input_value)
        self._enforce_input_policy(history)
        # `working` is what the model sees and may be rewritten by compaction;
        # `generated` is the untouched record of new items for durable history.
        working: RunInputItems = list(history)
        generated: RunInputItems = []
        new_items: list[dict[str, Any]] = []
        usage = Usage()
        if self._hooks is not None:
            await self._hooks.on_agent_start(self._context, self._agent)
        for turn_index in range(max_turns):
            self._snapshot_generated_count = len(generated)
            turn = await self._model_turn(working, turn_index, usage)
            if turn.reasoning:
                item = reasoning_item(turn.reasoning, self._agent.binding.model_name)
                working.append(item)
                generated.append(item)
                new_items.append(
                    {
                        "type": "reasoning_item",
                        "agent_name": self._agent.name,
                        "raw_item": item,
                    }
                )
            if turn.tool_calls and turn.text.strip():
                # A turn can answer and call tools at once; keep the spoken part so it
                # survives in history and returns as the tool-call message content.
                await self._record_assistant_message(
                    turn.text,
                    working,
                    generated,
                    new_items,
                )
            if not turn.tool_calls:
                output = self._final_output(turn.text)
                await self._record_assistant_message(
                    turn.text,
                    working,
                    generated,
                    new_items,
                )
                if self._hooks is not None:
                    await self._hooks.on_agent_end(self._context, self._agent, output)
                return RunResult(
                    input=input_value,
                    new_items=new_items,
                    generated_items=generated,
                    final_output=output,
                    last_agent_name=self._agent.name,
                    usage=usage,
                    working_items=list(working),
                    working_snapshot_items=self._working_snapshot,
                    working_snapshot_generated_count=self._snapshot_generated_count,
                )
            call_items, output_items, projected_items = await self._execute_tool_calls(
                turn.tool_calls
            )
            working.extend(call_items)
            working.extend(output_items)
            generated.extend(call_items)
            generated.extend(output_items)
            new_items.extend(projected_items)
            if self._agent.stop_on_first_tool and output_items:
                output = output_items[0].get("output")
                if self._hooks is not None:
                    await self._hooks.on_agent_end(self._context, self._agent, output)
                return RunResult(
                    input=input_value,
                    new_items=new_items,
                    generated_items=generated,
                    final_output=output,
                    last_agent_name=self._agent.name,
                    usage=usage,
                    working_items=list(working),
                    working_snapshot_items=self._working_snapshot,
                    working_snapshot_generated_count=self._snapshot_generated_count,
                )
        raise MaxTurnsExceeded(
            f"Agent '{self._agent.name}' exceeded its {max_turns}-turn budget.",
            RunResult(
                input=input_value,
                new_items=new_items,
                generated_items=generated,
                final_output=None,
                last_agent_name=self._agent.name,
                usage=usage,
                working_items=list(working),
                working_snapshot_items=self._working_snapshot,
                working_snapshot_generated_count=self._snapshot_generated_count,
            ),
        )

    async def _record_assistant_message(
        self,
        text: str,
        working: RunInputItems,
        generated: RunInputItems,
        new_items: list[dict[str, Any]],
    ) -> None:
        item = message_item("assistant", text)
        working.append(item)
        generated.append(item)
        projection = {
            "type": "message_output_item",
            "agent_name": self._agent.name,
            "raw_item": item,
            "content": text,
        }
        new_items.append(projection)
        await self._context.emit(
            "run.item",
            {"name": "message_output_created", "item": projection},
        )

    async def _model_turn(
        self,
        working: RunInputItems,
        turn_index: int,
        usage: Usage,
    ) -> ModelTurn:
        instructions = self._agent.instructions
        request_agent = self._agent
        prepared: RunInputItems = working
        if self._context_policy is not None:
            outcome = await self._context_policy.prepare(
                self._agent,
                list(working),
                instructions,
                self._context,
                turn_index=turn_index,
            )
            prepared = outcome.items
            instructions = outcome.instructions
            if outcome.working_items is not None:
                working[:] = outcome.working_items
            if outcome.response_max_tokens is not None:
                request_agent = replace(
                    self._agent,
                    model_settings=replace(
                        self._agent.model_settings, max_tokens=outcome.response_max_tokens
                    ),
                )
        tools = self._agent.enabled_tools(self._context)
        self._working_snapshot = list(working)
        messages = to_chat_messages(
            prepared,
            instructions,
            preserve_thinking=self._agent.binding.preserve_thinking,
            model_name=self._agent.binding.model_name,
        )
        if self._hooks is not None:
            await self._hooks.on_llm_start(
                self._context,
                self._agent,
                instructions,
                prepared,
            )
        try:
            turn = await stream_model_turn(request_agent, messages, tools, self._context)
        except OpenAIError as exc:
            raise ModelBehaviorError(
                f"The provider request failed: {type(exc).__name__}: {exc}"
            ) from exc
        usage.add(turn.usage)
        if self._hooks is not None:
            await self._hooks.on_llm_end(
                self._context,
                self._agent,
                turn.usage.to_dict(),
            )
        if turn.finish_reason == "length":
            raise ModelBehaviorError(
                "Model response was truncated (finish_reason=length; "
                f"output_tokens={turn.usage.output_tokens}; "
                f"max_tokens={request_agent.model_settings.max_tokens}). "
                "No tool calls from this turn were executed. Increase the configured "
                "response budget or select a supported lower reasoning effort."
            )
        if turn.finish_reason == "content_filter":
            raise ModelBehaviorError(
                "The provider filtered the model response (finish_reason=content_filter)."
            )
        if not turn.tool_calls and not turn.text.strip():
            raise ModelBehaviorError(
                "The model returned no answer text or tool calls "
                f"(finish_reason={turn.finish_reason}; output_tokens={turn.usage.output_tokens}). "
                "Reasoning alone is not a completed answer."
            )
        return turn

    async def _execute_tool_calls(
        self,
        calls: list[StreamedToolCall],
    ) -> tuple[RunInputItems, RunInputItems, list[dict[str, Any]]]:
        tools = {tool.name: tool for tool in self._agent.enabled_tools(self._context)}
        call_items: RunInputItems = []
        projected: list[dict[str, Any]] = []
        for call in calls:
            call.id = call.id or f"call-{uuid.uuid4()}"
            item = function_call_item(call.id, call.name, call.arguments or "{}")
            call_items.append(item)
            projection = {
                "type": "tool_call_item",
                "agent_name": self._agent.name,
                "raw_item": item,
                "title": call.name,
                "description": None,
                "tool_origin": None,
            }
            projected.append(projection)
            await self._context.emit(
                "run.item",
                {"name": "tool_called", "item": projection},
            )

        limit = self._settings.max_tool_concurrency
        if self._agent.model_settings.parallel_tool_calls is False:
            # Honour the agent's own policy even when the provider ignores the
            # `parallel_tool_calls` request field.
            limit = 1
        semaphore = asyncio.Semaphore(limit) if limit and limit > 0 else None
        runnable: list[StreamedToolCall] = []
        rejected: list[StreamedToolCall] = []
        started_serialized: set[str] = set()
        for call in calls:
            tool = tools.get(call.name)
            if tool is None or not tool.serialize_calls:
                runnable.append(call)
                continue
            # A single-flight tool runs once per turn, but a different single-flight
            # tool in the same turn is unrelated and still gets its first call.
            if call.name in started_serialized:
                rejected.append(call)
            else:
                started_serialized.add(call.name)
                runnable.append(call)

        async def run_one(call: StreamedToolCall) -> Any:
            if semaphore is None:
                return await self._invoke_tool(call, tools.get(call.name))
            async with semaphore:
                return await self._invoke_tool(call, tools.get(call.name))

        results: dict[str, Any] = {}
        if runnable:
            tasks = [asyncio.create_task(run_one(call)) for call in runnable]
            try:
                outcomes = await asyncio.gather(*tasks)
            except BaseException:
                # A lost lease or cancelled run must not leave sibling writes alive.
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            for call, outcome in zip(runnable, outcomes):
                results[call.id] = outcome
        for call in rejected:
            results[call.id] = (
                f"Rejected: {call.name} already has a call in progress and runs exactly "
                "one call at a time. This call was not started, so nothing was read, "
                "written, or saved for it. Never place two of these calls in the same "
                "turn: wait for the in-flight receipt, then issue this call again on "
                "its own."
            )

        output_items: RunInputItems = []
        for call in calls:
            output = results.get(call.id)
            item = function_call_output_item(call.id, call.name, output)
            output_items.append(item)
            projection = {
                "type": "tool_call_output_item",
                "agent_name": self._agent.name,
                "raw_item": item,
                "output": to_jsonable(output),
                "custom_data": None,
                "tool_origin": None,
            }
            projected.append(projection)
            await self._context.emit(
                "run.item",
                {"name": "tool_output", "item": projection},
            )
        return call_items, output_items, projected

    async def _invoke_tool(
        self,
        call: StreamedToolCall,
        tool: FunctionTool | None,
    ) -> Any:
        if tool is None:
            available = ", ".join(
                sorted(item.name for item in self._agent.enabled_tools(self._context))
            )
            return (
                f"Error: tool '{call.name}' is not available to this agent. "
                f"Offered tools: {available or 'none'}."
            )
        invocation = ToolInvocation(
            context=self._context,
            tool_call_id=call.id,
            tool_name=call.name,
            agent_name=self._agent.name,
        )
        identity = {
            "model": self._agent.binding.model_name,
            "provider_kind": self._agent.binding.provider_kind,
        }
        self._context.metadata["active_model"] = identity
        token = _tool_model_identity.set(identity)
        try:
            if self._hooks is not None:
                await self._hooks.on_tool_start(
                    self._context, self._agent, call.name, call.id,
                )
            try:
                result = await tool.on_invoke_tool(invocation, call.arguments or "{}")
            except (asyncio.CancelledError, LeaseOwnershipError, RunPolicyViolation):
                raise
            except Exception as exc:
                result = f"Error: {type(exc).__name__}: {exc}"
            if self._hooks is not None:
                await self._hooks.on_tool_end(
                    self._context, self._agent, call.name, call.id, result,
                )
            return result
        finally:
            _tool_model_identity.reset(token)

    def _final_output(self, text: str) -> Any:
        self._enforce_output_policy(text)
        if self._agent.output_schema is None:
            return text
        return self._agent.output_schema.validate_json(text)

    def _enforce_input_policy(self, items: RunInputItems) -> None:
        limit = self._settings.max_input_characters
        if limit is None:
            return
        actual = serialized_characters(items)
        if actual > limit:
            raise RunPolicyViolation(
                f"Run input exceeded the {limit}-character limit.",
                policy="max_input_characters",
                detail={"actual_characters": actual, "max_characters": limit},
            )

    def _enforce_output_policy(self, text: str) -> None:
        limit = self._settings.max_output_characters
        if limit is not None and len(text) > limit:
            raise RunPolicyViolation(
                f"Final output exceeded the {limit}-character limit.",
                policy="max_output_characters",
                detail={"actual_characters": len(text), "max_characters": limit},
            )


class DelegatedEventSink:
    """Keeps a sub-agent's model stream out of its parent's transcript.

    A delegated run shares the parent's context (metadata, receipts, tool runtime),
    so it must not share the parent's *narrative*. Model deltas and the sub-agent's
    own message items are dropped, because they would otherwise be folded into the
    parent's assistant/reasoning snapshots. Model-call lifecycle events are marked
    ``delegated`` so usage stays visible without being charged to the parent's turn
    accounting, and every forwarded event is namespaced with its delegation depth
    and owning agent.
    """

    _DROPPED_EVENTS = frozenset({"model.stream"})

    def __init__(
        self,
        downstream: Any,
        *,
        parent_agent_name: str,
        delegate_agent_name: str,
        depth: int,
    ) -> None:
        self._downstream = downstream
        self._parent_agent_name = parent_agent_name
        self._delegate_agent_name = delegate_agent_name
        self._depth = depth

    def current_lease(self) -> Any:
        current_lease = getattr(self._downstream, "current_lease", None)
        return current_lease() if current_lease is not None else None

    async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        namespaced = self._namespaced(event_type, payload)
        if namespaced is None:
            return
        await self._downstream.emit(event_type, namespaced)

    async def emit_transient(self, event_type: str, payload: dict[str, Any]) -> None:
        namespaced = self._namespaced(event_type, payload)
        if namespaced is None:
            return
        emit_transient = getattr(self._downstream, "emit_transient", None)
        if emit_transient is None:
            await self._downstream.emit(event_type, namespaced)
            return
        await emit_transient(event_type, namespaced)

    async def emit_batch(self, events: list[tuple[str, dict[str, Any]]]) -> None:
        forwarded = [
            (event_type, namespaced)
            for event_type, payload in events
            if (namespaced := self._namespaced(event_type, payload)) is not None
        ]
        if not forwarded:
            return
        emit_batch = getattr(self._downstream, "emit_batch", None)
        if emit_batch is None:
            for event_type, payload in forwarded:
                await self._downstream.emit(event_type, payload)
            return
        await emit_batch(forwarded)

    def _namespaced(
        self,
        event_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        if event_type in self._DROPPED_EVENTS:
            return None
        if event_type == "run.item" and _is_message_item(payload):
            return None
        return {
            **payload,
            "delegated": True,
            "delegation_depth": self._depth,
            "parent_agent_name": self._parent_agent_name,
            "delegate_agent_name": self._delegate_agent_name,
        }


def _is_message_item(payload: dict[str, Any]) -> bool:
    item = payload.get("item")
    return isinstance(item, dict) and item.get("type") == "message_output_item"


def delegated_context(
    context: ScholarWeaveContext,
    *,
    parent_agent_name: str,
    delegate_agent_name: str,
    depth: int,
) -> ScholarWeaveContext:
    """Share run state with a sub-agent while isolating its event stream."""
    if context.event_sink is None:
        return context
    return replace(
        context,
        event_sink=DelegatedEventSink(
            context.event_sink,
            parent_agent_name=parent_agent_name,
            delegate_agent_name=delegate_agent_name,
            depth=depth,
        ),
    )


def delegation_tool(
    *,
    owner_depth: int,
    delegate: AgentDefinition,
    tool_name: str,
    tool_description: str,
    max_turns: int,
    settings: RunSettings,
    hooks: RunHooks | None = None,
    context_policy: ContextPolicy | None = None,
    serialize_calls: bool = False,
) -> FunctionTool:
    """Expose one isolated sub-agent as an explicit tool on its owner.

    The sub-agent receives only the request text, never the owner's transcript, and
    an agent already at :data:`MAX_DELEGATION_DEPTH` cannot itself delegate.
    """
    delegate_depth = owner_depth + 1
    if delegate_depth > MAX_DELEGATION_DEPTH:
        raise ValueError(
            f"Delegation depth {delegate_depth} exceeds the maximum of "
            f"{MAX_DELEGATION_DEPTH}."
        )
    if delegate_depth >= MAX_DELEGATION_DEPTH and any(
        tool.is_delegation for tool in delegate.tools
    ):
        raise ValueError(
            f"Agent '{delegate.id}' sits at delegation depth {delegate_depth} and cannot "
            "delegate further."
        )

    async def invoke(invocation: ToolInvocation, raw_arguments: str) -> Any:
        try:
            arguments = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError as exc:
            return f"Error: delegation arguments were not valid JSON: {exc.msg}."
        request = arguments.get("request") if isinstance(arguments, dict) else None
        if not isinstance(request, str) or not request.strip():
            return "Error: delegation requires a non-empty 'request' string."
        runner = AgentRunner(
            delegate,
            context=delegated_context(
                invocation.context,
                parent_agent_name=invocation.agent_name,
                delegate_agent_name=delegate.name,
                depth=delegate_depth,
            ),
            settings=settings,
            hooks=hooks,
            context_policy=context_policy,
            depth=delegate_depth,
        )
        try:
            result = await runner.run(request, max_turns=max_turns)
        except asyncio.CancelledError:
            raise
        except MaxTurnsExceeded as exc:
            partial = "\n".join(
                str(item.get("content") or "")
                for item in exc.run_data.new_items
                if item.get("type") == "message_output_item"
            )
            return (
                f"The sub-agent used its whole {max_turns}-turn budget without finishing. "
                f"Partial progress: {partial[:2000] or 'none'}. Delegate only the "
                "unfinished scope, or continue directly."
            )
        except Exception as exc:
            return (
                f"The sub-agent stopped before completion: {type(exc).__name__}: {exc}. "
                "Summarize the usable progress and delegate only the unfinished scope to a "
                "fresh sub-agent."
            )
        return result.final_output

    return FunctionTool(
        name=tool_name,
        description=tool_description,
        params_json_schema={
            "type": "object",
            "properties": {
                "request": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 20_000,
                    "description": (
                        "The complete, self-contained instruction for the sub-agent. It "
                        "cannot see this conversation, so include every needed detail."
                    ),
                }
            },
            "required": ["request"],
            "additionalProperties": False,
        },
        on_invoke_tool=invoke,
        strict_json_schema=True,
        serialize_calls=serialize_calls,
        is_delegation=True,
    )


@dataclass(slots=True)
class RunHandle:
    """An in-flight run that can be awaited or cancelled."""

    task: "asyncio.Task[RunResult]"

    def cancel(self) -> None:
        if not self.task.done():
            self.task.cancel()

    def __await__(self):
        return self.task.__await__()


def run_streamed(
    agent: AgentDefinition,
    input_value: RunInput,
    *,
    context: ScholarWeaveContext,
    settings: RunSettings,
    max_turns: int,
    hooks: RunHooks | None = None,
    context_policy: ContextPolicy | None = None,
) -> RunHandle:
    """Start one agent run; events reach the caller through ``context.emit``."""
    runner = AgentRunner(
        agent,
        context=context,
        settings=settings,
        hooks=hooks,
        context_policy=context_policy,
    )
    return RunHandle(asyncio.ensure_future(runner.run(input_value, max_turns=max_turns)))


async def run_agent(
    agent: AgentDefinition,
    input_value: RunInput,
    *,
    context: ScholarWeaveContext,
    settings: RunSettings,
    max_turns: int,
    hooks: RunHooks | None = None,
    context_policy: ContextPolicy | None = None,
) -> RunResult:
    """Run one agent to completion in the caller's task."""
    runner = AgentRunner(
        agent,
        context=context,
        settings=settings,
        hooks=hooks,
        context_policy=context_policy,
    )
    return await runner.run(input_value, max_turns=max_turns)


def with_tools(agent: AgentDefinition, tools: list[FunctionTool]) -> AgentDefinition:
    return replace(agent, tools=[*agent.tools, *tools])
