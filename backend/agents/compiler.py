"""Compile a persisted blueprint into native agent definitions for the harness."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from backend.agents.blueprint import (
    AgentBlueprint,
    FunctionToolSpec,
    ModelSettingsSpec,
    ReasoningSpec,
)
from backend.agents.catalog import ToolCatalog
from backend.agents.context import ScholarWeaveContext
from backend.agents.context_budget import ContextBudgetPolicy
from backend.agents.harness import (
    MAX_DELEGATION_DEPTH,
    AgentDefinition,
    FunctionTool,
    HarnessError,
    JsonSchemaOutput,
    ModelSettings,
    RunSettings,
    delegation_tool,
)
from backend.core.config import Settings
from backend.core.errors import ValidationError
from backend.prompting.registry import PromptRegistry, default_prompt_registry
from backend.providers.inference import InferenceScheduler
from backend.providers.types import (
    AgentModelResolver,
    ModelReference,
    ProviderRuntimeError,
    ResolvedAgentModel,
)
from backend.runs.hooks import ScholarWeaveRunHooks

GLOBAL_AGENT_INSTRUCTIONS = default_prompt_registry().render("global")


def current_system_information(
    at: datetime | None = None,
    *,
    timezone_name: str | None = None,
    user_profile: str | None = None,
) -> str:
    """Describe the current local date, time, timezone, and optional user context."""
    current = at or datetime.now().astimezone()
    if current.tzinfo is None:
        raise ValueError("System information requires a timezone-aware datetime.")
    if timezone_name:
        current = current.astimezone(ZoneInfo(timezone_name))
    offset = current.strftime("%z")
    formatted_offset = f"{offset[:3]}:{offset[3:]}"
    current_timezone_name = current.tzname() or "local"
    information = (
        "System information:\n"
        f"Current date: {current.date().isoformat()}\n"
        f"Current time: {current.strftime('%H:%M:%S')} "
        f"{current_timezone_name} (UTC{formatted_offset})"
    )
    if user_profile:
        information += f"\nUser context: {user_profile.strip()}"
    return information


def with_global_agent_instructions(
    instructions: str,
    *,
    global_instructions: str = GLOBAL_AGENT_INSTRUCTIONS,
    at: datetime | None = None,
    timezone_name: str | None = None,
    user_profile: str | None = None,
) -> str:
    """Append shared policy and current system information to agent instructions."""
    combined = instructions.rstrip()
    global_instructions = global_instructions.strip()
    if global_instructions and global_instructions not in combined:
        combined = f"{combined}\n\n{global_instructions}"
    return (
        f"{combined}\n\n"
        f"{current_system_information(at, timezone_name=timezone_name, user_profile=user_profile)}"
    )


def with_json_schema_output_instructions(
    instructions: str,
    schema_name: str,
    schema: dict[str, Any],
) -> str:
    """Append a provider-independent requirement to return only schema-valid JSON."""
    serialized_schema = json.dumps(
        schema,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        f"{instructions.rstrip()}\n\n"
        "Structured output requirement:\n"
        f"Return only one JSON value matching the {schema_name} schema below. "
        "Do not return Markdown fences, headings, commentary, or an answer to the user's "
        "request outside that JSON value. This requirement applies even when the provider "
        "does not enforce its response-format setting.\n"
        f"{serialized_schema}"
    )


@dataclass(frozen=True, slots=True)
class CompiledAgent:
    blueprint: AgentBlueprint
    entry_agent: AgentDefinition
    agents_by_id: dict[str, AgentDefinition]
    resolved_models: dict[str, ResolvedAgentModel]
    run_settings: RunSettings
    max_turns: int | None
    context_policy: ContextBudgetPolicy | None = None
    completion_validator: Callable[[ScholarWeaveContext], None] | None = None
    completion_policy_id: str | None = None
    context_window_tokens: int | None = None


class AgentCompiler:
    def __init__(
        self,
        model_resolver: AgentModelResolver,
        tool_catalog: ToolCatalog,
        settings: Settings | None = None,
        prompts: PromptRegistry | None = None,
        inference_scheduler: InferenceScheduler | None = None,
    ) -> None:
        self._models = model_resolver
        self._tools = tool_catalog
        self._settings = settings
        self._prompts = prompts

    def compile(
        self,
        blueprint: AgentBlueprint,
        *,
        context_window_tokens: int | None = None,
    ) -> CompiledAgent:
        """Validate a blueprint and compile its agents, tools, and run settings."""
        issues = self._reference_issues(blueprint)
        if issues:
            raise ValidationError("Agent blueprint is invalid.", issues=issues)

        agents_by_id: dict[str, AgentDefinition] = {}
        resolved_models: dict[str, ResolvedAgentModel] = {}
        agent_specs = {spec.id: spec for spec in blueprint.agents}

        for spec in blueprint.agents:
            resolved = self._models.resolve_agent_model(
                ModelReference(
                    provider_profile_id=spec.model.provider_profile_id,
                    model=spec.model.model,
                ),
                require_tools=self._agent_requires_tools(spec.id, blueprint),
            )
            resolved_models[spec.id] = resolved
            output_schema = (
                JsonSchemaOutput(
                    spec.output.name,
                    spec.output.schema_,
                    strict=spec.output.strict,
                )
                if spec.output is not None
                else None
            )
            instructions = with_global_agent_instructions(
                spec.instructions,
                global_instructions=(
                    self._prompts.render("global")
                    if self._prompts is not None
                    else GLOBAL_AGENT_INSTRUCTIONS
                ),
                timezone_name=self._settings.user_timezone if self._settings else None,
                user_profile=self._settings.user_profile if self._settings else None,
            )
            if spec.output is not None:
                instructions = with_json_schema_output_instructions(
                    instructions,
                    spec.output.name,
                    spec.output.schema_,
                )
            agents_by_id[spec.id] = AgentDefinition(
                id=spec.id,
                name=spec.name,
                description=spec.description,
                instructions=instructions,
                binding=resolved,
                model_settings=self._model_settings(spec.model_settings),
                output_schema=output_schema,
                stop_on_first_tool=spec.tool_use_behavior == "stop_on_first_tool",
            )

        run_settings = RunSettings(
            max_turns=blueprint.run.max_turns,
            max_tool_concurrency=blueprint.run.max_tool_concurrency,
            max_input_characters=blueprint.run.max_input_characters,
            max_output_characters=blueprint.run.max_output_characters,
            workflow_name=blueprint.name,
        )
        compaction_model = None
        compaction_model_error = None
        if self._settings and getattr(self._settings, "agent_context_model_summary_enabled", True):
            reference = getattr(self._settings, "default_model_references", {}).get("compaction")
            if reference and any(reference.values()):
                try:
                    if not reference.get("provider_profile_id") or not reference.get("model"):
                        raise ProviderRuntimeError(
                            "The compaction model needs an explicit provider profile and model name."
                        )
                    compaction_model = self._models.resolve_agent_model(
                        ModelReference.model_validate(reference), require_tools=False,
                    )
                except ProviderRuntimeError as exc:
                    # A stale optional helper must not prevent the main agent from running.
                    compaction_model_error = {
                        "error_type": type(exc).__name__, "error": str(exc),
                        "model_name": reference.get("model") or "",
                        "provider_profile_id": reference.get("provider_profile_id") or "",
                    }
        context_policy = (
            ContextBudgetPolicy(
                self._settings,
                context_window_tokens_by_agent={
                    agent_id: window
                    for agent_id, resolved in resolved_models.items()
                    if (window := context_window_tokens or resolved.context_window_tokens)
                    is not None
                },
                prompt_registry=self._prompts,
                compaction_model=compaction_model,
                compaction_model_error=compaction_model_error,
            )
            if self._settings is not None
            else None
        )

        tools_by_id = {
            spec.id: self._tools.build_function_tool(spec) for spec in blueprint.tools
        }
        depths = self._delegation_depths(blueprint)
        hooks = ScholarWeaveRunHooks()
        delegations_by_owner: dict[str, list[FunctionTool]] = {
            agent_id: [] for agent_id in agents_by_id
        }
        # Build deepest delegations first so a delegate already carries its own
        # sub-agent tools when its owner wraps it.
        for spec in sorted(
            blueprint.agent_tools,
            key=lambda item: depths[item.delegate_agent_id],
            reverse=True,
        ):
            delegate = agents_by_id[spec.delegate_agent_id]
            delegate.tools = self._bound_tools(
                agent_specs[spec.delegate_agent_id],
                tools_by_id,
                delegations_by_owner[spec.delegate_agent_id],
            )
            delegations_by_owner[spec.owner_agent_id].append(
                delegation_tool(
                    owner_depth=depths[spec.owner_agent_id],
                    delegate=delegate,
                    tool_name=spec.tool_name,
                    tool_description=spec.tool_description,
                    max_turns=spec.max_turns,
                    settings=run_settings,
                    hooks=hooks,
                    context_policy=context_policy,
                    serialize_calls=spec.serialize_calls,
                )
            )

        for agent_id, agent in agents_by_id.items():
            agent.tools = self._bound_tools(
                agent_specs[agent_id],
                tools_by_id,
                delegations_by_owner[agent_id],
            )
            self._validate_tool_names(agent_id, agent.tools)

        effective_context_window = (
            context_window_tokens
            or resolved_models[blueprint.entry_agent_id].context_window_tokens
            or (
                getattr(self._settings, "agent_context_window_tokens", None)
                if self._settings is not None
                else None
            )
        )
        return CompiledAgent(
            blueprint=blueprint,
            entry_agent=agents_by_id[blueprint.entry_agent_id],
            agents_by_id=agents_by_id,
            resolved_models=resolved_models,
            run_settings=run_settings,
            max_turns=blueprint.run.max_turns,
            context_policy=context_policy,
            context_window_tokens=effective_context_window,
        )

    def validate(self, blueprint: AgentBlueprint) -> tuple[str, ...]:
        try:
            self.compile(blueprint)
        except ValidationError as exc:
            return exc.issues or (exc.message,)
        except (HarnessError, ProviderRuntimeError, ValueError) as exc:
            return (str(exc),)
        return ()

    def _bound_tools(
        self,
        spec: Any,
        tools_by_id: dict[str, FunctionTool],
        delegations: list[FunctionTool],
    ) -> list[FunctionTool]:
        bound = [tools_by_id[tool_id] for tool_id in spec.tool_ids]
        bound.extend(delegations)
        if (
            self._settings is not None
            and bound
            and not any(tool.name == "read_tool_result" for tool in bound)
        ):
            bound.append(
                self._tools.build_function_tool(
                    FunctionToolSpec(
                        id="context-result-reader",
                        catalog_id="tool.results.read",
                    )
                )
            )
        return bound

    @staticmethod
    def _agent_requires_tools(agent_id: str, blueprint: AgentBlueprint) -> bool:
        spec = next(item for item in blueprint.agents if item.id == agent_id)
        return bool(
            spec.tool_ids
            or any(item.owner_agent_id == agent_id for item in blueprint.agent_tools)
        )

    @staticmethod
    def _model_settings(spec: ModelSettingsSpec) -> ModelSettings:
        return ModelSettings(
            temperature=spec.temperature,
            top_p=spec.top_p,
            frequency_penalty=spec.frequency_penalty,
            presence_penalty=spec.presence_penalty,
            tool_choice=spec.tool_choice,
            parallel_tool_calls=spec.parallel_tool_calls,
            max_tokens=spec.max_tokens,
            reasoning_effort=(
                spec.reasoning.effort if spec.reasoning is not None else None
            ),
            verbosity=spec.verbosity,
        )

    @staticmethod
    def _delegation_depths(blueprint: AgentBlueprint) -> dict[str, int]:
        """Depth 0 is the coordinator; delegates inherit their owner's depth plus one."""
        depths = {spec.id: 0 for spec in blueprint.agents}
        for _ in range(MAX_DELEGATION_DEPTH + 1):
            for relation in blueprint.agent_tools:
                owner = depths.get(relation.owner_agent_id, 0)
                current = depths.get(relation.delegate_agent_id, 0)
                depths[relation.delegate_agent_id] = max(current, owner + 1)
        return depths

    @staticmethod
    def _reference_issues(blueprint: AgentBlueprint) -> list[str]:
        issues: list[str] = []

        def duplicates(values: list[str], label: str) -> None:
            seen: set[str] = set()
            for value in values:
                if value in seen:
                    issues.append(f"Duplicate {label} ID '{value}'.")
                seen.add(value)

        agent_ids = {spec.id for spec in blueprint.agents}
        tool_ids = {spec.id for spec in blueprint.tools}
        duplicates([spec.id for spec in blueprint.agents], "agent")
        duplicates([spec.id for spec in blueprint.tools], "tool")
        duplicates([spec.id for spec in blueprint.agent_tools], "agent-tool")

        if blueprint.entry_agent_id not in agent_ids:
            issues.append(f"Entry agent '{blueprint.entry_agent_id}' does not exist.")
        for spec in blueprint.agents:
            for tool_id in spec.tool_ids:
                if tool_id not in tool_ids:
                    issues.append(f"Agent '{spec.id}' references missing tool '{tool_id}'.")
        for spec in blueprint.agent_tools:
            if spec.owner_agent_id not in agent_ids:
                issues.append(
                    f"Agent tool '{spec.id}' has missing owner agent '{spec.owner_agent_id}'."
                )
            if spec.delegate_agent_id not in agent_ids:
                issues.append(
                    f"Agent tool '{spec.id}' has missing delegate agent "
                    f"'{spec.delegate_agent_id}'."
                )
            if spec.owner_agent_id == spec.delegate_agent_id:
                issues.append(f"Agent tool '{spec.id}' cannot delegate to its owner agent.")
        issues.extend(AgentCompiler._delegation_topology_issues(blueprint, agent_ids))
        return issues

    @staticmethod
    def _delegation_topology_issues(
        blueprint: AgentBlueprint,
        agent_ids: set[str],
    ) -> list[str]:
        """Reject delegation cycles and paths deeper than coordinator -> sub -> helper."""
        graph: dict[str, list[str]] = {agent_id: [] for agent_id in agent_ids}
        for relation in blueprint.agent_tools:
            owner = relation.owner_agent_id
            delegate = relation.delegate_agent_id
            if owner not in agent_ids or delegate not in agent_ids or owner == delegate:
                continue
            graph[owner].append(delegate)

        issues: list[str] = []

        def walk(agent_id: str, depth: int, path: list[str]) -> None:
            for delegate in graph[agent_id]:
                delegation_path = [*path, agent_id, delegate]
                if delegate in path or delegate == agent_id:
                    cycle_start = delegation_path.index(delegate)
                    message = (
                        "Agent-tool delegation contains a cycle: "
                        + " -> ".join(f"'{item}'" for item in delegation_path[cycle_start:])
                        + "."
                    )
                    if message not in issues:
                        issues.append(message)
                    continue
                if depth + 1 > MAX_DELEGATION_DEPTH:
                    message = (
                        f"Agent-tool delegation exceeds maximum depth "
                        f"{MAX_DELEGATION_DEPTH}: "
                        + " -> ".join(f"'{item}'" for item in delegation_path)
                        + "."
                    )
                    if message not in issues:
                        issues.append(message)
                    continue
                walk(delegate, depth + 1, [*path, agent_id])

        for agent_id in sorted(agent_ids):
            walk(agent_id, 0, [])
        return issues

    @staticmethod
    def _validate_tool_names(agent_id: str, tools: list[FunctionTool]) -> None:
        names: set[str] = set()
        duplicated: set[str] = set()
        for tool in tools:
            if tool.name in names:
                duplicated.add(tool.name)
            names.add(tool.name)
        if duplicated:
            raise ValidationError(
                "Agent has colliding tool names.",
                issues=[
                    f"Agent '{agent_id}' binds duplicate tool name '{name}'."
                    for name in sorted(duplicated)
                ],
            )


def with_reasoning_effort(compiled: CompiledAgent, effort: str | None) -> CompiledAgent:
    """Return a copy of the compiled agents that request one reasoning effort."""
    if effort is None:
        return compiled
    blueprint = compiled.blueprint.model_copy(deep=True)
    for agent in blueprint.agents:
        agent.model_settings.reasoning = ReasoningSpec(effort=effort)
    agents_by_id = {
        agent_id: replace(
            agent,
            model_settings=replace(agent.model_settings, reasoning_effort=effort),
        )
        for agent_id, agent in compiled.agents_by_id.items()
    }
    return replace(
        compiled,
        blueprint=blueprint,
        agents_by_id=agents_by_id,
        entry_agent=agents_by_id[blueprint.entry_agent_id],
    )


__all__ = [
    "AgentCompiler",
    "CompiledAgent",
    "current_system_information",
    "with_global_agent_instructions",
    "with_json_schema_output_instructions",
    "with_reasoning_effort",
]
