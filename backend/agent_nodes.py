"""Agent and Python-code nodes, executed through the OpenAI Agents SDK.

The graph stays ours — the SDK has no DAG concept — so these nodes are the bridge. An
``agent`` node builds an ``Agent`` from its wired tools and handoffs, and only runs it
when something downstream actually reads its answer. That means an agent wired purely as
another agent's handoff target or tool is constructed, not invoked, and costs nothing.
"""

from __future__ import annotations

import json
import re
from typing import Any

from agents import Agent, ModelSettings, Runner
from agents.exceptions import AgentsException, MaxTurnsExceeded
from agents.stream_events import RawResponsesStreamEvent, RunItemStreamEvent
from openai.types.responses import ResponseTextDeltaEvent
from pydantic import BaseModel, Field

from backend.agent_runtime import (
    AgentProviderError,
    build_run_config_for_resolved,
    chat_model_for_resolved,
    default_model_settings_for_kind,
)
from backend.agent_tools import AgentRunContext, node_as_tool
from backend.registry import BaseNode, NodeExecutionContext, PortDefinition, ToolParameter, ToolSpec
from backend.sandbox import SandboxError, SandboxLimits, run_python
from backend.schemas import ModelReference
from backend.utils import dumps_json, truncate_text

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_.]*)\s*\}\}")

DEFAULT_PYTHON_CODE = '''def transform(inputs):
    """Reshape values between nodes.

    Each wired value arrives under the name of the port it came from, and the workflow's
    own inputs are under inputs["workflow"]. Whatever you return becomes this node's
    value output, so return a list if the next step maps over it.
    """
    return inputs
'''


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return dumps_json(value)


def fit_to_budget(text: str, budget: int) -> tuple[str, bool]:
    """Trims an oversized prompt from the middle, keeping the opening and the ending.

    Providers silently drop whatever does not fit their context window, and they drop it
    from the *front* — so an over-long paper summary ends up being written from the
    reference list. Cutting the middle ourselves keeps the abstract and the conclusion,
    and leaves a visible marker where the gap is.
    """
    if budget <= 0 or len(text) <= budget:
        return text, False
    marker = "\n\n[... trimmed to fit the model's context window ...]\n\n"
    room = max(budget - len(marker), 0)
    head = room * 2 // 3
    tail = room - head
    return text[:head] + marker + (text[-tail:] if tail else ""), True


def render_template(template: str, values: dict[str, Any]) -> str:
    """Substitutes ``{{name}}`` and ``{{a.b}}`` references, leaving unknown ones intact.

    Unknown placeholders survive verbatim so a typo shows up in the prompt instead of
    silently becoming an empty string.
    """

    def replace(match: re.Match[str]) -> str:
        path = match.group(1).split(".")
        cursor: Any = values
        for part in path:
            if isinstance(cursor, dict) and part in cursor:
                cursor = cursor[part]
            else:
                return match.group(0)
        return _as_text(cursor)

    return _PLACEHOLDER.sub(replace, template)


def _extract_json(text: str) -> Any:
    """Pulls a JSON value out of a model answer that may be fenced or prefaced with prose."""
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = candidate.find(opener)
        end = candidate.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("The model did not return JSON")


class AgentNodeConfig(BaseModel):
    name: str = Field(default="Agent", min_length=1, max_length=80)
    instructions: str = Field(
        default="You are a careful research assistant. Answer using the material you are given.",
        min_length=1,
    )
    model: str | None = None
    provider_profile_id: str | None = None
    model_reference: ModelReference | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_turns: int | None = Field(default=None, ge=1, le=50)
    #: JSON schema describing the answer. Enforced by asking, parsing and retrying,
    #: because Ollama's compatibility layer does not reliably honour response formats.
    output_schema: dict[str, Any] | None = None
    handoff_description: str = ""
    tool_name: str | None = None
    tool_description: str | None = None


class AgentNode(BaseNode):
    type_name = "agent"
    label = "Agent"
    description = "Runs an LLM agent that can call the tools and hand off to the agents wired into it."
    category = "agents"
    tags = ["agent", "llm"]
    inputs = [
        PortDefinition("input", "text", required=False, description="The task for this agent, available as {{input}}."),
        PortDefinition("context", "any", required=False, description="Reference material, available as {{context}}."),
        PortDefinition(
            "tools",
            "tool",
            required=False,
            fan_in=True,
            description="Tools the agent may call. Connect as many as you like.",
        ),
        PortDefinition(
            "handoffs",
            "agent",
            required=False,
            fan_in=True,
            description="Agents this one may hand the conversation to.",
        ),
    ]
    outputs = [
        PortDefinition("text", "text", description="The agent's final message."),
        PortDefinition("output", "any", required=False, description="Parsed answer when an output schema is set."),
        PortDefinition("agent", "agent", required=False, description="Wire into another agent's handoffs port."),
        PortDefinition("tool", "tool", required=False, description="Wire into another agent's tools port."),
    ]
    config_model = AgentNodeConfig

    def _build(
        self, context: NodeExecutionContext, inputs: dict[str, Any], config: AgentNodeConfig
    ) -> tuple[Agent[Any], bool, Any]:
        settings = context.services.settings
        reference = config.model_reference
        if reference is None and (config.provider_profile_id or config.model):
            reference = ModelReference(provider_profile_id=config.provider_profile_id, model=config.model)
        has_agent_connections = bool({"tools", "handoffs"} & context.connected_inputs)
        capability = (
            "tools"
            if has_agent_connections or _fan_in(inputs.get("tools")) or _fan_in(inputs.get("handoffs"))
            else "chat"
        )
        resolved = context.services.model_runtime.resolve(
            capability, node_reference=reference, workflow_defaults=context.workflow_model_defaults
        )
        prompt_values = {
            **context.run_inputs,
            **{key: value for key, value in inputs.items() if key not in {"tools", "handoffs"}},
        }
        instructions = render_template(config.instructions, prompt_values)
        instructions, trimmed = fit_to_budget(instructions, settings.max_context_chars)
        if config.output_schema:
            instructions += (
                "\n\nReply with JSON only — no prose, no code fences — matching this schema:\n"
                f"{dumps_json(config.output_schema)}"
            )
        tools = [tool for tool in _fan_in(inputs.get("tools")) if tool is not None]
        handoffs = [agent for agent in _fan_in(inputs.get("handoffs")) if isinstance(agent, Agent)]
        model_settings = default_model_settings_for_kind(resolved.kind)
        if config.temperature is not None:
            model_settings = ModelSettings(
                temperature=config.temperature,
                parallel_tool_calls=model_settings.parallel_tool_calls,
            )
        agent = Agent(
            name=config.name,
            instructions=instructions,
            model=chat_model_for_resolved(settings, resolved),
            model_settings=model_settings,
            tools=tools,
            handoffs=handoffs,
            handoff_description=config.handoff_description or f"Hand off to {config.name}.",
        )
        return agent, trimmed, resolved

    async def _run(
        self,
        context: NodeExecutionContext,
        agent: Agent[Any],
        prompt: str,
        config: AgentNodeConfig,
        resolved: Any,
    ) -> str:
        settings = context.services.settings
        run_context = AgentRunContext(
            services=context.services,
            run_id=context.run_id,
            node_path=context.node_path,
            run_inputs=dict(context.run_inputs),
        )
        streamed = Runner.run_streamed(
            agent,
            prompt,
            context=run_context,
            run_config=build_run_config_for_resolved(settings, resolved, workflow_name=context.node_path),
            max_turns=config.max_turns or settings.agent_max_turns,
        )
        try:
            async for event in streamed.stream_events():
                try:
                    context.check_cancelled()
                except Exception:
                    streamed.cancel()
                    raise
                await self._emit_event(context, event)
        except MaxTurnsExceeded as exc:
            raise ValueError(
                f"{config.name} stopped after {config.max_turns or settings.agent_max_turns} turns "
                "without settling on an answer. Raise the turn limit or simplify its tools."
            ) from exc
        except AgentsException as exc:
            raise ValueError(f"{config.name} failed: {exc}") from exc
        return streamed.final_output_as(str, raise_if_incorrect_type=False) or ""

    async def _emit_event(self, context: NodeExecutionContext, event: Any) -> None:
        if isinstance(event, RawResponsesStreamEvent):
            data = event.data
            if isinstance(data, ResponseTextDeltaEvent) and data.delta:
                await context.emit("token", {"node_path": context.node_path, "text": data.delta})
            return
        if isinstance(event, RunItemStreamEvent):
            if event.name == "tool_called":
                raw = getattr(event.item, "raw_item", None)
                await context.emit(
                    "agent_tool_call",
                    {
                        "node_path": context.node_path,
                        "tool": getattr(raw, "name", "tool"),
                        "arguments": truncate_text(str(getattr(raw, "arguments", "")), 400),
                    },
                )
            elif event.name == "tool_output":
                await context.emit(
                    "agent_tool_output",
                    {"node_path": context.node_path, "output": truncate_text(str(getattr(event.item, "output", "")), 400)},
                )
            elif event.name in {"handoff_requested", "handoff_occured"}:
                await context.emit(
                    "agent_handoff",
                    {"node_path": context.node_path, "stage": event.name},
                )

    async def execute(self, context: NodeExecutionContext, inputs: dict[str, Any], config: AgentNodeConfig) -> dict[str, Any]:
        agent, trimmed, resolved = self._build(context, inputs, config)
        result: dict[str, Any] = {"agent": agent, "text": "", "output": None}
        settings = context.services.settings
        result["tool"] = agent.as_tool(
            tool_name=_tool_slug(config.tool_name or config.name),
            tool_description=config.tool_description or config.handoff_description or f"Ask {config.name} to help.",
            run_config=build_run_config_for_resolved(settings, resolved, workflow_name=context.node_path),
            max_turns=config.max_turns or settings.agent_max_turns,
        )
        # An agent referenced only as a handoff target or a tool must not run on its own.
        wanted = context.consumed_ports
        if wanted and not ({"text", "output"} & set(wanted)):
            return result

        prompt = _as_text(inputs.get("input")) or _as_text(inputs.get("context"))
        if not prompt.strip():
            prompt = "Carry out your instructions."
        prompt, prompt_trimmed = fit_to_budget(prompt, context.services.settings.max_context_chars)
        if trimmed or prompt_trimmed:
            await context.emit(
                "warning",
                {
                    "node_path": context.node_path,
                    "message": (
                        f"{config.name} was given more text than the {context.services.settings.max_context_chars} "
                        "character budget allows, so the middle was dropped. Summarise in smaller batches, or raise "
                        "Max context characters in Settings."
                    ),
                },
            )
        try:
            answer = await self._run(context, agent, prompt, config, resolved)
        except AgentProviderError as exc:
            raise ValueError(str(exc)) from exc
        result["text"] = answer
        if config.output_schema:
            try:
                result["output"] = _extract_json(answer)
            except ValueError:
                retry = await self._run(
                    context,
                    agent,
                    f"{prompt}\n\nYour previous reply was not valid JSON. Reply with JSON only.",
                    config,
                    resolved,
                )
                result["text"] = retry
                result["output"] = _extract_json(retry)
        else:
            result["output"] = answer
        return result


def _tool_slug(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_]+", "_", name.strip()).strip("_").lower()
    return slug or "agent"


def _fan_in(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


class PythonCodeConfig(BaseModel):
    code: str = Field(default=DEFAULT_PYTHON_CODE, max_length=40_000)
    entrypoint: str = Field(default="transform", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    #: Populated when the node is wired into an agent's tools port.
    tool_name: str = Field(default="run_python_step", pattern=r"^[a-z0-9_]{1,60}$")
    tool_description: str = "Runs a custom Python transform over the supplied values."
    timeout_seconds: float | None = Field(default=None, gt=0, le=300)
    memory_mb: int | None = Field(default=None, ge=32, le=8192)


class PythonCodeNode(BaseNode):
    type_name = "python_code"
    label = "Python code"
    description = (
        "Runs your own Python in a restricted subprocess. Inputs are named after the ports "
        "they came from, and whatever you return becomes the value output."
    )
    category = "code"
    tags = ["code", "python", "transform"]
    inputs = [
        PortDefinition(
            "inputs",
            "any",
            required=False,
            fan_in=True,
            description="Values handed to transform(inputs). Connect as many as you like.",
        ),
    ]
    outputs = [
        PortDefinition("value", "any", description="Whatever the function returned."),
        PortDefinition("text", "text", description="The return value rendered as text."),
        PortDefinition("stdout", "text", required=False, description="Anything the code printed."),
    ]
    config_model = PythonCodeConfig
    tool_spec = ToolSpec(
        name="run_python_step",
        description="Runs a custom Python transform over the supplied values.",
        parameters=(
            ToolParameter(
                port="inputs",
                json_type="string",
                description="JSON object of values to hand to the function.",
                required=False,
            ),
        ),
        result_port="text",
    )

    async def execute(self, context: NodeExecutionContext, inputs: dict[str, Any], config: PythonCodeConfig) -> dict[str, Any]:
        settings = context.services.settings
        if not settings.python_node_enabled:
            raise ValueError(
                "Python nodes are turned off. Enable them under Settings if you trust the code in this workflow."
            )
        payload = _python_inputs(inputs.get("inputs"), context.run_inputs, context.input_origins.get("inputs", []))
        limits = SandboxLimits(
            timeout_seconds=config.timeout_seconds or settings.python_node_timeout_seconds,
            memory_mb=config.memory_mb or settings.python_node_memory_mb,
        )
        context.check_cancelled()
        try:
            outcome = await run_python(
                config.code,
                payload,
                limits=limits,
                allowed_imports=settings.python_node_allowed_imports,
                entrypoint=config.entrypoint,
            )
        except SandboxError as exc:
            if exc.stdout:
                await context.emit("stdout", {"node_path": context.node_path, "text": exc.stdout})
            raise ValueError(_sandbox_message(exc)) from exc
        if outcome.stdout:
            await context.emit("stdout", {"node_path": context.node_path, "text": outcome.stdout})
        return {"value": outcome.value, "text": _as_text(outcome.value), "stdout": outcome.stdout}


def _python_inputs(wired: Any, run_inputs: dict[str, Any], origins: list[str]) -> dict[str, Any]:
    """Builds the dict handed to ``transform``.

    Values are named after the output port they came from, so wiring a node's ``chunks``
    into a Python step gives you ``inputs["chunks"]``. Everything is also reachable
    under ``value`` (a single input) or ``values`` (several), and the workflow's own
    inputs sit under ``workflow`` so a transform can read them without an extra edge.
    """
    values = _fan_in(wired)
    payload: dict[str, Any] = {}
    if len(values) == 1 and isinstance(values[0], dict) and not origins:
        payload = dict(values[0])
    for index, value in enumerate(values):
        name = origins[index] if index < len(origins) else f"input_{index}"
        payload.setdefault(name, value)
    if len(values) == 1:
        payload.setdefault("value", values[0])
    elif values:
        payload.setdefault("values", values)
        payload.setdefault("value", values)
    payload.setdefault("workflow", dict(run_inputs))
    return payload


_SANDBOX_HINTS = {
    "timeout": "The code ran past its time limit. Raise the timeout or make the transform cheaper.",
    "memory": "The code used more memory than allowed. Raise the memory cap or process less at a time.",
    "import": "That import is not on the allowlist. Add it under Settings if you need it.",
    "permission": "The code tried to reach the network or the filesystem, which the sandbox blocks.",
    "killed": "The code was stopped by the sandbox.",
}


def _sandbox_message(error: SandboxError) -> str:
    hint = _SANDBOX_HINTS.get(error.kind)
    return f"{error}. {hint}" if hint else str(error)


__all__ = [
    "AgentNode",
    "AgentNodeConfig",
    "PythonCodeNode",
    "PythonCodeConfig",
    "AgentRunContext",
    "node_as_tool",
    "render_template",
    "DEFAULT_PYTHON_CODE",
]
