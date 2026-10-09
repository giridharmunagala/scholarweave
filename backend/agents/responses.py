"""Stateless Responses API conversion and streamed output normalization."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from backend.utils import to_jsonable


def responses_binding_scope(binding: Any) -> dict[str, str]:
    """Opaque reasoning is scoped to endpoint, model, profile, and credentials."""
    api_key = getattr(binding.client, "api_key", None)
    return {
        "api_mode": "responses",
        "provider_kind": binding.provider_kind,
        "model": binding.model_name,
        "base_url": str(getattr(binding.client, "base_url", "")),
        "provider_profile_id": getattr(binding, "provider_profile_id", None) or "",
        "credential_fingerprint": (
            hashlib.sha256(api_key.encode("utf-8")).hexdigest()
            if isinstance(api_key, str) and api_key else ""
        ),
    }


def _output_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(
        to_jsonable(value), ensure_ascii=False, separators=(",", ":"),
    )


def to_responses_input(
    items: Sequence[dict[str, Any]], binding: Any,
) -> list[dict[str, Any]]:
    """Accept canonical history or Chat messages without leaking Chat-only fields."""
    result: list[dict[str, Any]] = []
    scope = responses_binding_scope(binding)
    for item in items:
        kind = item.get("type")
        role = item.get("role")
        if kind == "reasoning":
            if item.get("_scholarweave_binding") == scope and item.get("encrypted_content"):
                result.append({
                    key: item[key] for key in ("type", "id", "summary", "encrypted_content")
                    if key in item
                })
            continue
        if kind == "function_call":
            result.append({
                "type": kind, "call_id": str(item.get("call_id") or item.get("id") or ""),
                "name": str(item.get("name") or ""),
                "arguments": str(item.get("arguments") or "{}"),
            })
            continue
        if kind == "function_call_output" or role == "tool":
            result.append({
                "type": "function_call_output",
                "call_id": str(item.get("call_id") or item.get("tool_call_id") or ""),
                "output": _output_text(
                    item.get("output") if kind == "function_call_output" else item.get("content")
                ),
            })
            continue
        if role not in {"system", "developer", "user", "assistant"}:
            continue
        content = item.get("content")
        parts: list[dict[str, Any]] = []
        if isinstance(content, str):
            parts = [{"type": "input_text", "text": content}]
        elif isinstance(content, list):
            for part in content:
                if part.get("type") in {"text", "input_text", "output_text"}:
                    parts.append({"type": "input_text", "text": str(part.get("text") or "")})
                elif part.get("type") in {"image_url", "input_image"}:
                    image = part.get("image_url")
                    image = image if isinstance(image, dict) else {"url": image}
                    parts.append({
                        "type": "input_image", "image_url": image.get("url"),
                        "detail": part.get("detail") or image.get("detail") or "auto",
                    })
                else:
                    _behavior_error(f"Unsupported Responses input content: {part.get('type')!r}.")
        if parts:
            result.append({"type": "message", "role": role, "content": parts})
        for call in item.get("tool_calls") or []:
            function = call.get("function") or {}
            result.append({
                "type": "function_call", "call_id": call["id"],
                "name": function["name"], "arguments": function.get("arguments") or "{}",
            })
    return result


def responses_request_parameters(
    agent: Any, messages: list[dict[str, Any]], tools: list[Any], *, stream: bool = True,
) -> dict[str, Any]:
    settings = agent.model_settings
    for name in ("frequency_penalty", "presence_penalty", "top_p"):
        if getattr(settings, name) is not None:
            _behavior_error(f"Responses API does not support configured {name} in ScholarWeave.")
    parameters: dict[str, Any] = {
        "model": agent.binding.model_name,
        "messages": messages,
    }
    if stream:
        parameters["stream"] = True
    if settings.temperature is not None:
        parameters["temperature"] = settings.temperature
    if settings.max_tokens is not None:
        parameters["max_tokens"] = settings.max_tokens
    effort = settings.reasoning_effort
    if effort is None:
        effort = agent.binding.default_reasoning_effort
    if tools and agent.binding.tool_reasoning_effort is not None:
        effort = agent.binding.tool_reasoning_effort
    if effort is not None:
        parameters["reasoning_effort"] = effort
    if settings.verbosity is not None:
        parameters["verbosity"] = settings.verbosity
    if agent.output_schema is not None:
        parameters["response_format"] = {
            "type": "json_schema", "json_schema": {
                "name": agent.output_schema.name, "schema": agent.output_schema.json_schema(),
                "strict": agent.output_schema.strict,
            },
        }
    if tools:
        parameters["tools"] = [{
            "type": "function", "function": {
                "name": tool.name, "description": tool.description,
                "parameters": tool.params_json_schema, "strict": tool.strict_json_schema,
            },
        } for tool in tools]
        if settings.tool_choice is not None:
            parameters["tool_choice"] = settings.tool_choice
        if settings.parallel_tool_calls is not None and agent.binding.supports_parallel_tool_calls:
            parameters["parallel_tool_calls"] = settings.parallel_tool_calls
    return responses_parameters_from_chat(parameters, agent.binding)


def responses_parameters_from_chat(
    parameters: dict[str, Any], binding: Any,
) -> dict[str, Any]:
    """Translate existing generation kwargs without dropping configured options."""
    supported = {
        "model", "messages", "stream", "stream_options", "temperature", "reasoning_effort",
        "verbosity", "max_tokens", "max_completion_tokens", "tools", "tool_choice",
        "parallel_tool_calls", "response_format",
    }
    unsupported = set(parameters) - supported
    if unsupported:
        _behavior_error(f"Unsupported Responses request settings: {', '.join(sorted(unsupported))}.")
    result: dict[str, Any] = {
        "model": parameters.get("model") or binding.model_name,
        "input": to_responses_input(parameters.get("messages") or [], binding),
        "store": False, "include": ["reasoning.encrypted_content"],
    }
    for name in ("temperature", "stream", "parallel_tool_calls"):
        if name in parameters:
            result[name] = parameters[name]
    if parameters.get("reasoning_effort") is not None:
        result["reasoning"] = {"effort": parameters["reasoning_effort"]}
    max_tokens = parameters.get("max_completion_tokens", parameters.get("max_tokens"))
    if max_tokens is not None:
        result["max_output_tokens"] = max_tokens
    text: dict[str, Any] = {}
    if parameters.get("verbosity") is not None:
        text["verbosity"] = parameters["verbosity"]
    output_format = parameters.get("response_format")
    if output_format:
        text["format"] = (
            {"type": "json_schema", **output_format["json_schema"]}
            if output_format.get("type") == "json_schema" else dict(output_format)
        )
    if text:
        result["text"] = text
    if parameters.get("tools"):
        tools = []
        for tool in parameters["tools"]:
            if tool.get("type") != "function":
                _behavior_error("Responses hosted tools are disabled.")
            tools.append({"type": "function", **tool["function"]})
        result["tools"] = tools
    choice = parameters.get("tool_choice")
    if choice is not None:
        if isinstance(choice, dict):
            if choice.get("type") != "function":
                _behavior_error("Responses hosted tool choices are disabled.")
            result["tool_choice"] = {"type": "function", "name": choice["function"]["name"]}
        else:
            result["tool_choice"] = (
                choice if choice in {"auto", "none", "required"}
                else {"type": "function", "name": choice}
            )
    # Responses streams always carry usage on terminal response events.
    options = parameters.get("stream_options")
    if options and set(options) - {"include_usage"}:
        _behavior_error("Unsupported Responses stream options.")
    return result


def _behavior_error(message: str) -> None:
    from backend.agents.harness import ModelBehaviorError

    raise ModelBehaviorError(message)


def validate_response(response: Any) -> dict[str, Any]:
    """Never accept failed, filtered, incomplete, or nonterminal output as success."""
    raw = to_jsonable(response)
    if not isinstance(raw, dict):
        _behavior_error("Responses API returned an invalid response.")
    if raw.get("status") != "completed":
        _behavior_error(
            f"Responses API response {raw.get('status') or 'missing terminal status'}: "
            f"{raw.get('error') or raw.get('incomplete_details') or 'no completion details'}."
        )
    return raw


def response_output_text(response: Any) -> str:
    raw = validate_response(response)
    texts: list[str] = []
    for item in raw.get("output") or []:
        if item.get("type") not in {"message", "function_call", "reasoning"}:
            _behavior_error(
                f"Unsupported Responses output item: {item.get('type')!r}; hosted tools are disabled."
            )
        if item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if part.get("type") == "refusal":
                _behavior_error(f"Responses API refused the response: {part.get('refusal') or ''}.")
            if part.get("type") == "output_text":
                texts.append(part.get("text") or "")
    return "".join(texts)


class ResponsesStreamState:
    """Reconcile delta and completed-item events without duplicate projections."""

    def __init__(self, binding: Any) -> None:
        self.binding = binding
        self.output: dict[int, dict[str, Any]] = {}
        self.text_parts: dict[tuple[int, int], str] = {}
        self.summary_parts: dict[tuple[int, int], str] = {}
        self.argument_parts: dict[int, str] = {}
        self.final_arguments: dict[int, str] = {}
        self.response: dict[str, Any] | None = None

    def _record_output(self, index: int, item: dict[str, Any]) -> None:
        previous = self.output.get(index, {})
        if previous.get("type") != item.get("type") or previous.get("id") != item.get("id"):
            previous = {}
        self.output[index] = {
            **previous, **{key: value for key, value in item.items() if value is not None},
        }

    async def consume(
        self, event: dict[str, Any], emit: Callable[[str, dict[str, Any]], Awaitable[None]],
        progress: Callable[[str], Awaitable[None]],
    ) -> None:
        kind = event.get("type")
        index = event.get("output_index", 0)
        if kind in {"response.created", "response.in_progress"}:
            await progress("processing")
        elif kind in {"response.output_item.added", "response.output_item.done"}:
            self._record_output(index, event["item"])
            if event["item"].get("type") == "reasoning":
                await progress("thinking")
            elif event["item"].get("type") == "function_call":
                await progress("tool")
        elif kind in {"response.output_text.delta", "response.reasoning_summary_text.delta"}:
            summary = kind == "response.reasoning_summary_text.delta"
            key = (index, event.get("summary_index" if summary else "content_index", 0))
            parts = self.summary_parts if summary else self.text_parts
            delta = event.get("delta") or ""
            parts[key] = parts.get(key, "") + delta
            if delta:
                await progress("thinking" if summary else "writing")
                await emit("model.stream", {"raw_type": kind, "delta": delta})
        elif kind == "response.function_call_arguments.delta":
            delta = event.get("delta") or ""
            self.argument_parts[index] = self.argument_parts.get(index, "") + delta
            await progress("tool")
            if delta:
                await emit("model.stream", {"raw_type": kind, "delta": delta})
        elif kind == "response.function_call_arguments.done":
            self.final_arguments[index] = event.get("arguments") or ""
        elif kind in {"response.completed", "response.failed", "response.incomplete"}:
            self.response = event.get("response") or {}
            validate_response(self.response)
            for output_index, item in enumerate(self.response.get("output") or []):
                self._record_output(output_index, item)
        elif kind == "error":
            _behavior_error(
                f"Responses API stream error: {event.get('code') or ''}: {event.get('message') or ''}."
            )
        elif kind in {"response.refusal.delta", "response.refusal.done"}:
            _behavior_error("Responses API refused or filtered the model response.")

    async def finish(
        self, emit: Callable[[str, dict[str, Any]], Awaitable[None]],
        progress: Callable[[str], Awaitable[None]],
    ) -> None:
        if self.response is None:
            _behavior_error("Responses API stream ended without a terminal response.")
        for index, item in sorted(self.output.items()):
            kind = item.get("type")
            if kind == "message":
                for part_index, part in enumerate(item.get("content") or []):
                    if part.get("type") == "refusal":
                        _behavior_error("Responses API refused or filtered the model response.")
                    if part.get("type") == "output_text":
                        await self._fallback(
                            self.text_parts, (index, part_index), part.get("text") or "",
                            "response.output_text.delta", "writing", emit, progress,
                        )
            elif kind == "reasoning":
                for part_index, part in enumerate(item.get("summary") or []):
                    await self._fallback(
                        self.summary_parts, (index, part_index), part.get("text") or "",
                        "response.reasoning_summary_text.delta", "thinking", emit, progress,
                    )
            elif kind == "function_call":
                arguments = (
                    item.get("arguments") or self.final_arguments.get(index)
                    or self.argument_parts.get(index, "")
                )
                prior = self.argument_parts.get(index, "")
                if not isinstance(arguments, str) or not arguments.startswith(prior):
                    _behavior_error("Responses API final function arguments conflict with streamed deltas.")
                if arguments.startswith(prior) and len(arguments) > len(prior):
                    await progress("tool")
                    await emit("model.stream", {
                        "raw_type": "response.function_call_arguments.delta",
                        "delta": arguments[len(prior):],
                    })
                item["arguments"] = arguments
            else:
                _behavior_error(f"Unsupported Responses output item: {kind!r}; hosted tools are disabled.")

    async def _fallback(self, parts, key, text, raw_type, phase, emit, progress) -> None:
        prior = parts.get(key, "")
        if not text.startswith(prior):
            _behavior_error(f"Responses API final {phase} text conflicts with streamed deltas.")
        if text.startswith(prior) and len(text) > len(prior):
            delta = text[len(prior):]
            parts[key] = text
            await progress(phase)
            await emit("model.stream", {"raw_type": raw_type, "delta": delta})

    @property
    def text(self) -> str:
        return "".join(value for _, value in sorted(self.text_parts.items()))

    @property
    def reasoning(self) -> str:
        return "".join(value for _, value in sorted(self.summary_parts.items()))

    def reasoning_items(self) -> list[dict[str, Any]]:
        return [{
            **{key: item[key] for key in ("type", "id", "summary", "encrypted_content") if key in item},
            "_scholarweave_binding": responses_binding_scope(self.binding),
        } for _, item in sorted(self.output.items()) if item.get("type") == "reasoning"]

    def tool_calls(self) -> list[Any]:
        from backend.agents.harness import StreamedToolCall

        calls = []
        for index, item in sorted(self.output.items()):
            if item.get("type") != "function_call":
                continue
            if not item.get("call_id") or not item.get("name"):
                _behavior_error("Responses API returned a function call without call_id or name.")
            calls.append(StreamedToolCall(
                index=index, id=item["call_id"], name=item["name"],
                arguments=item.get("arguments") or self.argument_parts.get(index, "") or "{}",
            ))
        return calls
