"""Bridges workflow nodes and the OpenAI Agents SDK.

A node that declares a :class:`~backend.registry.ToolSpec` becomes a ``FunctionTool`` an
agent can call, using the very same ``execute`` the DAG uses. That keeps one
implementation per capability: whether the author wires a fixed pipeline or hands the
decision to an agent, the document lookup, retrieval or Python transform behind it is
identical.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from agents import FunctionTool, RunContextWrapper
from agents.tool_context import ToolContext

from backend.registry import BaseNode, NodeExecutionContext, ToolSpec
from backend.utils import dumps_json


@dataclass(slots=True)
class AgentRunContext:
    """Typed payload handed to every tool call, so tools reach services without globals."""

    services: Any
    run_id: str
    node_path: str
    run_inputs: dict[str, Any]


def _tool_schema(spec: ToolSpec, provider_strict: bool, pinned: set[str]) -> dict[str, Any]:
    """Describes the arguments the model still has to supply.

    Anything already wired on the canvas is left out entirely. Advertising a pinned
    ``document_id`` makes a model believe it needs an ID it was never given, and small
    local models respond by refusing to call the tool at all.
    """
    properties = {
        parameter.port: {
            "type": parameter.json_type,
            "description": parameter.description or f"Value for {parameter.port}.",
        }
        for parameter in spec.parameters
        if parameter.port not in pinned
    }
    required = [
        parameter.port
        for parameter in spec.parameters
        if parameter.required and parameter.port not in pinned
    ]
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
    if provider_strict:
        # Strict mode requires every property to be listed as required.
        schema["required"] = list(properties)
    return schema


def _as_tool_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return dumps_json(value)


def node_as_tool(
    node: BaseNode,
    config: Any,
    context: NodeExecutionContext,
    *,
    static_inputs: dict[str, Any] | None = None,
    strict_schema: bool = False,
    name_override: str | None = None,
    description_override: str | None = None,
) -> FunctionTool:
    """Wraps a workflow node so an agent can call it."""
    spec = node.tool_spec
    if spec is None:
        raise ValueError(f"Node type '{node.type_name}' cannot be used as a tool")

    fixed = {key: value for key, value in (static_inputs or {}).items() if value is not None}

    async def invoke(_wrapper: ToolContext[AgentRunContext] | RunContextWrapper[Any], arguments: str) -> str:
        try:
            supplied = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError:
            return f"Could not read the arguments for {spec.name}: they were not valid JSON."
        if not isinstance(supplied, dict):
            supplied = {}
        # Values wired on the canvas win over anything the model invents, so an agent
        # cannot redirect a tool away from the document the workflow selected.
        call_inputs = {**{key: value for key, value in supplied.items() if value is not None}, **fixed}
        context.check_cancelled()
        await context.emit(
            "tool_call",
            {"node_path": context.node_path, "tool": spec.name, "arguments": call_inputs},
        )
        try:
            result = await node.execute(context, call_inputs, config)
        except Exception as exc:  # noqa: BLE001 - the model is told, and can retry or explain
            message = f"{type(exc).__name__}: {exc}"
            await context.emit(
                "tool_result",
                {"node_path": context.node_path, "tool": spec.name, "error": message},
            )
            return f"The {spec.name} tool failed. {message}"
        payload = result.get(spec.result_port, result)
        text = _as_tool_text(payload)
        await context.emit(
            "tool_result",
            {"node_path": context.node_path, "tool": spec.name, "preview": text[:400]},
        )
        if not text.strip():
            # Handing back "" makes a model assume the call failed and retry forever.
            return (
                f"The {spec.name} tool ran and found nothing. Do not call it again with the "
                "same arguments — either try different wording, use another tool, or say "
                "plainly that the document does not cover this."
            )
        return text

    return FunctionTool(
        name=name_override or spec.name,
        description=description_override or spec.description,
        params_json_schema=_tool_schema(spec, strict_schema, set(fixed)),
        on_invoke_tool=invoke,
        strict_json_schema=strict_schema,
    )
