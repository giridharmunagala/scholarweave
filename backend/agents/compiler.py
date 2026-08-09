from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agents import (
    Agent,
    AgentsException,
    FileSearchTool,
    ModelSettings,
    RunConfig,
    ToolExecutionConfig,
    WebSearchTool,
    handoff,
)
from agents.tool import Tool

from backend.agents.blueprint import (
    AgentBlueprint,
    FileSearchToolSpec,
    FunctionToolSpec,
    WebSearchToolSpec,
)
from backend.agents.catalog import GuardrailCatalog, ToolCatalog
from backend.agents.instructions import with_global_agent_instructions
from backend.agents.output import JsonSchemaOutput
from backend.core.errors import ValidationError
from backend.providers.errors import ProviderRuntimeError
from backend.providers.types import AgentModelResolver, ModelReference, ResolvedAgentModel
from backend.runtime.context import ScholarWeaveContext
from backend.runtime.sdk_compat import assert_supported_sdk


@dataclass(frozen=True, slots=True)
class CompiledAgent:
    blueprint: AgentBlueprint
    entry_agent: Agent[ScholarWeaveContext]
    agents_by_id: dict[str, Agent[ScholarWeaveContext]]
    resolved_models: dict[str, ResolvedAgentModel]
    run_config: RunConfig
    max_turns: int
    completion_validator: Callable[[ScholarWeaveContext], None] | None = None


class AgentCompiler:
    def __init__(
        self,
        model_resolver: AgentModelResolver,
        tool_catalog: ToolCatalog,
        guardrail_catalog: GuardrailCatalog | None = None,
    ) -> None:
        self._models = model_resolver
        self._tools = tool_catalog
        self._guardrails = guardrail_catalog or GuardrailCatalog()

    def compile(self, blueprint: AgentBlueprint) -> CompiledAgent:
        assert_supported_sdk()
        issues = self._reference_issues(blueprint)
        if issues:
            raise ValidationError("Agent blueprint is invalid.", issues=issues)

        agents_by_id: dict[str, Agent[ScholarWeaveContext]] = {}
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
            output_type = (
                JsonSchemaOutput(
                    spec.output.name,
                    spec.output.schema_,
                    strict=spec.output.strict,
                )
                if spec.output is not None
                else None
            )
            agents_by_id[spec.id] = Agent[ScholarWeaveContext](
                name=spec.name,
                handoff_description=spec.description,
                instructions=with_global_agent_instructions(spec.instructions),
                model=resolved.model,
                model_settings=self._model_settings(spec.model_settings, resolved),
                output_type=output_type,
                tool_use_behavior=spec.tool_use_behavior,
                reset_tool_choice=spec.reset_tool_choice,
            )

        guardrails = {spec.id: spec for spec in blueprint.guardrails}
        tools_by_id: dict[str, Tool] = {}
        for spec in blueprint.tools:
            tool = self._compile_tool(spec, blueprint, resolved_models)
            if isinstance(spec, FunctionToolSpec):
                tool.tool_input_guardrails = [
                    self._guardrails.build_tool_input(guardrails[guardrail_id])
                    for guardrail_id in spec.input_guardrail_ids
                ]
                tool.tool_output_guardrails = [
                    self._guardrails.build_tool_output(guardrails[guardrail_id])
                    for guardrail_id in spec.output_guardrail_ids
                ]
            tools_by_id[spec.id] = tool

        handoffs_by_source: dict[str, list[Any]] = {agent_id: [] for agent_id in agents_by_id}
        for spec in blueprint.handoffs:
            handoffs_by_source[spec.source_agent_id].append(
                handoff(
                    agents_by_id[spec.target_agent_id],
                    tool_name_override=spec.tool_name,
                    tool_description_override=spec.tool_description,
                    nest_handoff_history=spec.nest_handoff_history,
                )
            )

        agent_tools_by_owner: dict[str, list[Tool]] = {agent_id: [] for agent_id in agents_by_id}
        for spec in blueprint.agent_tools:
            agent_tools_by_owner[spec.owner_agent_id].append(
                agents_by_id[spec.delegate_agent_id].as_tool(
                    tool_name=spec.tool_name,
                    tool_description=spec.tool_description,
                    max_turns=spec.max_turns,
                    needs_approval=spec.needs_approval,
                )
            )

        for agent_id, agent in agents_by_id.items():
            spec = agent_specs[agent_id]
            bound_tools = [tools_by_id[tool_id] for tool_id in spec.tool_ids]
            bound_tools.extend(agent_tools_by_owner[agent_id])
            self._validate_tool_names(agent_id, bound_tools)
            self._validate_hosted_tools(agent_id, bound_tools, resolved_models[agent_id])
            agent.tools = bound_tools
            agent.handoffs = handoffs_by_source[agent_id]
            agent.input_guardrails = [
                self._guardrails.build_input(guardrails[guardrail_id])
                for guardrail_id in spec.input_guardrail_ids
            ]
            agent.output_guardrails = [
                self._guardrails.build_output(guardrails[guardrail_id])
                for guardrail_id in spec.output_guardrail_ids
            ]

        return CompiledAgent(
            blueprint=blueprint,
            entry_agent=agents_by_id[blueprint.entry_agent_id],
            agents_by_id=agents_by_id,
            resolved_models=resolved_models,
            run_config=RunConfig(
                workflow_name=blueprint.name,
                tracing_disabled=not blueprint.run.tracing_enabled,
                tool_execution=ToolExecutionConfig(
                    max_function_tool_concurrency=blueprint.run.max_tool_concurrency
                ),
            ),
            max_turns=blueprint.run.max_turns,
        )

    def validate(self, blueprint: AgentBlueprint) -> tuple[str, ...]:
        try:
            self.compile(blueprint)
        except ValidationError as exc:
            return exc.issues or (exc.message,)
        except (AgentsException, ProviderRuntimeError, ValueError) as exc:
            return (str(exc),)
        return ()

    def _compile_tool(
        self,
        spec: Any,
        blueprint: AgentBlueprint,
        resolved_models: dict[str, ResolvedAgentModel],
    ) -> Tool:
        if isinstance(spec, FunctionToolSpec):
            return self._tools.build_function_tool(spec)
        if isinstance(spec, WebSearchToolSpec):
            self._require_hosted_tool_provider(spec.id, blueprint, resolved_models)
            return WebSearchTool(
                search_context_size=spec.search_context_size,
                external_web_access=spec.external_web_access,
            )
        if isinstance(spec, FileSearchToolSpec):
            self._require_hosted_tool_provider(spec.id, blueprint, resolved_models)
            return FileSearchTool(
                vector_store_ids=spec.vector_store_ids,
                max_num_results=spec.max_num_results,
                include_search_results=spec.include_search_results,
            )
        raise ValidationError(
            "Agent blueprint contains an unsupported tool.",
            issues=[f"Unsupported tool kind on '{spec.id}'."],
        )

    @staticmethod
    def _agent_requires_tools(agent_id: str, blueprint: AgentBlueprint) -> bool:
        spec = next(item for item in blueprint.agents if item.id == agent_id)
        return bool(
            spec.tool_ids
            or any(item.owner_agent_id == agent_id for item in blueprint.agent_tools)
            or any(item.source_agent_id == agent_id for item in blueprint.handoffs)
        )

    @staticmethod
    def _model_settings(spec: Any, resolved: ResolvedAgentModel) -> ModelSettings:
        parallel = spec.parallel_tool_calls
        if parallel is None:
            parallel = resolved.supports_parallel_tool_calls
        return ModelSettings(
            temperature=spec.temperature,
            top_p=spec.top_p,
            frequency_penalty=spec.frequency_penalty,
            presence_penalty=spec.presence_penalty,
            tool_choice=spec.tool_choice,
            parallel_tool_calls=parallel,
            truncation=spec.truncation,
            max_tokens=spec.max_tokens,
            verbosity=spec.verbosity,
            include_usage=True,
        )

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
        guardrail_ids = {spec.id for spec in blueprint.guardrails}
        duplicates([spec.id for spec in blueprint.agents], "agent")
        duplicates([spec.id for spec in blueprint.tools], "tool")
        duplicates([spec.id for spec in blueprint.guardrails], "guardrail")
        duplicates([spec.id for spec in blueprint.handoffs], "handoff")
        duplicates([spec.id for spec in blueprint.agent_tools], "agent-tool")

        if blueprint.entry_agent_id not in agent_ids:
            issues.append(f"Entry agent '{blueprint.entry_agent_id}' does not exist.")
        for spec in blueprint.agents:
            for tool_id in spec.tool_ids:
                if tool_id not in tool_ids:
                    issues.append(f"Agent '{spec.id}' references missing tool '{tool_id}'.")
            for guardrail_id in spec.input_guardrail_ids:
                guardrail = next((item for item in blueprint.guardrails if item.id == guardrail_id), None)
                if guardrail is None:
                    issues.append(f"Agent '{spec.id}' references missing input guardrail '{guardrail_id}'.")
                elif guardrail.kind != "input":
                    issues.append(f"Guardrail '{guardrail_id}' is not an input guardrail.")
            for guardrail_id in spec.output_guardrail_ids:
                guardrail = next((item for item in blueprint.guardrails if item.id == guardrail_id), None)
                if guardrail is None:
                    issues.append(f"Agent '{spec.id}' references missing output guardrail '{guardrail_id}'.")
                elif guardrail.kind != "output":
                    issues.append(f"Guardrail '{guardrail_id}' is not an output guardrail.")
        for spec in blueprint.tools:
            if not isinstance(spec, FunctionToolSpec):
                continue
            for guardrail_id in spec.input_guardrail_ids:
                guardrail = next(
                    (item for item in blueprint.guardrails if item.id == guardrail_id),
                    None,
                )
                if guardrail is None:
                    issues.append(
                        f"Tool '{spec.id}' references missing input guardrail "
                        f"'{guardrail_id}'."
                    )
                elif guardrail.kind != "tool_input":
                    issues.append(
                        f"Guardrail '{guardrail_id}' is not a tool input guardrail."
                    )
            for guardrail_id in spec.output_guardrail_ids:
                guardrail = next(
                    (item for item in blueprint.guardrails if item.id == guardrail_id),
                    None,
                )
                if guardrail is None:
                    issues.append(
                        f"Tool '{spec.id}' references missing output guardrail "
                        f"'{guardrail_id}'."
                    )
                elif guardrail.kind != "tool_output":
                    issues.append(
                        f"Guardrail '{guardrail_id}' is not a tool output guardrail."
                    )
        for spec in blueprint.handoffs:
            if spec.source_agent_id not in agent_ids:
                issues.append(f"Handoff '{spec.id}' has missing source agent '{spec.source_agent_id}'.")
            if spec.target_agent_id not in agent_ids:
                issues.append(f"Handoff '{spec.id}' has missing target agent '{spec.target_agent_id}'.")
            if spec.source_agent_id == spec.target_agent_id:
                issues.append(f"Handoff '{spec.id}' cannot target its source agent.")
        for spec in blueprint.agent_tools:
            if spec.owner_agent_id not in agent_ids:
                issues.append(f"Agent tool '{spec.id}' has missing owner agent '{spec.owner_agent_id}'.")
            if spec.delegate_agent_id not in agent_ids:
                issues.append(f"Agent tool '{spec.id}' has missing delegate agent '{spec.delegate_agent_id}'.")
            if spec.owner_agent_id == spec.delegate_agent_id:
                issues.append(f"Agent tool '{spec.id}' cannot delegate to its owner agent.")
        return issues

    @staticmethod
    def _validate_tool_names(agent_id: str, tools: list[Tool]) -> None:
        names: set[str] = set()
        duplicates: set[str] = set()
        for tool in tools:
            name = getattr(tool, "name", type(tool).__name__)
            if name in names:
                duplicates.add(name)
            names.add(name)
        if duplicates:
            raise ValidationError(
                "Agent has colliding tool names.",
                issues=[
                    f"Agent '{agent_id}' binds duplicate tool name '{name}'."
                    for name in sorted(duplicates)
                ],
            )

    @staticmethod
    def _validate_hosted_tools(
        agent_id: str,
        tools: list[Tool],
        resolved: ResolvedAgentModel,
    ) -> None:
        if resolved.supports_hosted_tools:
            return
        hosted = [
            type(tool).__name__
            for tool in tools
            if isinstance(tool, (WebSearchTool, FileSearchTool))
        ]
        if hosted:
            raise ValidationError(
                "Hosted SDK tools require a Responses-compatible provider.",
                issues=[f"Agent '{agent_id}' cannot use hosted tool '{name}'." for name in hosted],
            )

    @staticmethod
    def _require_hosted_tool_provider(
        tool_id: str,
        blueprint: AgentBlueprint,
        resolved_models: dict[str, ResolvedAgentModel],
    ) -> None:
        consumers = [agent.id for agent in blueprint.agents if tool_id in agent.tool_ids]
        unsupported = [
            agent_id
            for agent_id in consumers
            if not resolved_models[agent_id].supports_hosted_tools
        ]
        if unsupported:
            raise ValidationError(
                "Hosted SDK tools require a Responses-compatible provider.",
                issues=[
                    f"Agent '{agent_id}' cannot bind hosted tool '{tool_id}'."
                    for agent_id in unsupported
                ],
            )
