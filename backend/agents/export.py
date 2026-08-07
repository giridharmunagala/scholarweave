from __future__ import annotations

import json
import re
from typing import Any

from backend.agents.blueprint import (
    AgentBlueprint,
    FileSearchToolSpec,
    FunctionToolSpec,
    GuardrailSpec,
    WebSearchToolSpec,
)
from backend.agents.instructions import with_global_agent_instructions
from backend.agents.catalog import GuardrailCatalog, ToolCatalog


def export_agent(
    blueprint: AgentBlueprint,
    *,
    tool_catalog: ToolCatalog | None = None,
    guardrail_catalog: GuardrailCatalog | None = None,
) -> str:
    lines = _preamble(blueprint)
    guardrail_variables = _render_guardrails(lines, blueprint, guardrail_catalog)
    tool_variables = _render_tools(
        lines,
        blueprint,
        tool_catalog,
        guardrail_variables,
    )
    _render_agents(lines, blueprint)
    _render_relationships(lines, blueprint, tool_variables)
    _render_session_and_runner(lines, blueprint)
    return "\n".join(lines)


def _preamble(blueprint: AgentBlueprint) -> list[str]:
    return [
        '"""Generated from a ScholarWeave OpenAI Agents SDK blueprint.',
        "",
        "Provider profile IDs are emitted as metadata only. Replace model strings with",
        "your SDK Model objects when using non-default or OpenAI-compatible providers.",
        '"""',
        "",
        "import asyncio",
        "import json",
        "from typing import Any",
        "",
        "from agents import (",
        "    Agent,",
        "    AgentOutputSchemaBase,",
        "    FileSearchTool,",
        "    FunctionTool,",
        "    GuardrailFunctionOutput,",
        "    InputGuardrail,",
        "    ModelSettings,",
        "    OutputGuardrail,",
        "    RunConfig,",
        "    Runner,",
        "    SQLiteSession,",
        "    ToolExecutionConfig,",
        "    ToolGuardrailFunctionOutput,",
        "    ToolInputGuardrail,",
        "    ToolOutputGuardrail,",
        "    WebSearchTool,",
        "    handoff,",
        ")",
        "from jsonschema import Draft202012Validator",
        "",
        f"SDK_VERSION = {blueprint.sdk_version!r}",
        f"PROVIDER_REFERENCES = {_literal({
            spec.id: spec.model.model_dump(mode='json')
            for spec in blueprint.agents
        })}",
        "",
        "",
        "class PersistedJsonSchema(AgentOutputSchemaBase):",
        "    def __init__(self, name: str, schema: dict[str, Any], strict: bool) -> None:",
        "        Draft202012Validator.check_schema(schema)",
        "        self._name = name",
        "        self._schema = schema",
        "        self._strict = strict",
        "        self._validator = Draft202012Validator(schema)",
        "",
        "    def is_plain_text(self) -> bool:",
        "        return False",
        "",
        "    def is_strict_json_schema(self) -> bool:",
        "        return self._strict",
        "",
        "    def json_schema(self) -> dict[str, Any]:",
        "        return self._schema",
        "",
        "    def name(self) -> str:",
        "        return self._name",
        "",
        "    def validate_json(self, json_str: str) -> Any:",
        "        value = json.loads(json_str)",
        "        self._validator.validate(value)",
        "        return value",
        "",
        "",
        "def _serialized_length(value: Any) -> int:",
        "    if isinstance(value, str):",
        "        return len(value)",
        "    return len(json.dumps(value, ensure_ascii=False, default=str, sort_keys=True))",
        "",
    ]


def _render_guardrails(
    lines: list[str],
    blueprint: AgentBlueprint,
    catalog: GuardrailCatalog | None,
) -> dict[str, str]:
    if blueprint.guardrails and catalog is not None:
        known = {(item.kind, item.catalog_id) for item in catalog.definitions()}
    else:
        known = set()
    variables: dict[str, str] = {}
    for spec in blueprint.guardrails:
        variable = _identifier(f"guardrail_{spec.id}")
        variables[spec.id] = variable
        if (spec.kind, spec.catalog_id) == (spec.kind, "content.max_characters"):
            _render_length_guardrail(lines, variable, spec)
        else:
            lines.extend(_placeholder_guardrail(variable, spec, known))
        lines.append("")
    return variables


def _render_length_guardrail(
    lines: list[str],
    variable: str,
    spec: GuardrailSpec,
) -> None:
    limit = int(spec.config.get("max_characters", 50_000))
    callback = _identifier(f"check_{spec.id}")
    if spec.kind == "input":
        lines.extend(
            [
                f"async def {callback}(_context, _agent, input_value):",
                "    actual = _serialized_length(input_value)",
                "    return GuardrailFunctionOutput(",
                f"        output_info={{'actual_characters': actual, 'max_characters': {limit}}},",
                f"        tripwire_triggered=actual > {limit},",
                "    )",
                f"{variable} = InputGuardrail({callback}, name={spec.id!r}, run_in_parallel=False)",
            ]
        )
    elif spec.kind == "output":
        lines.extend(
            [
                f"async def {callback}(_context, _agent, output):",
                "    actual = _serialized_length(output)",
                "    return GuardrailFunctionOutput(",
                f"        output_info={{'actual_characters': actual, 'max_characters': {limit}}},",
                f"        tripwire_triggered=actual > {limit},",
                "    )",
                f"{variable} = OutputGuardrail({callback}, name={spec.id!r})",
            ]
        )
    elif spec.kind == "tool_input":
        lines.extend(
            [
                f"async def {callback}(data):",
                "    actual = len(data.context.tool_arguments)",
                f"    info = {{'actual_characters': actual, 'max_characters': {limit}}}",
                f"    if actual > {limit}:",
                "        return ToolGuardrailFunctionOutput.reject_content(",
                "            'Tool arguments exceeded the configured character limit.', info",
                "        )",
                "    return ToolGuardrailFunctionOutput.allow(info)",
                f"{variable} = ToolInputGuardrail({callback}, name={spec.id!r})",
            ]
        )
    else:
        lines.extend(
            [
                f"async def {callback}(data):",
                "    actual = _serialized_length(data.output)",
                f"    info = {{'actual_characters': actual, 'max_characters': {limit}}}",
                f"    if actual > {limit}:",
                "        return ToolGuardrailFunctionOutput.reject_content(",
                "            'Tool output exceeded the configured character limit.', info",
                "        )",
                "    return ToolGuardrailFunctionOutput.allow(info)",
                f"{variable} = ToolOutputGuardrail({callback}, name={spec.id!r})",
            ]
        )


def _placeholder_guardrail(
    variable: str,
    spec: GuardrailSpec,
    known: set[tuple[str, str]],
) -> list[str]:
    callback = _identifier(f"check_{spec.id}")
    unknown_note = (
        ""
        if (spec.kind, spec.catalog_id) in known
        else f" Unknown catalog binding: {spec.catalog_id}."
    )
    if spec.kind == "input":
        return [
            f"async def {callback}(_context, _agent, _input):",
            f"    raise NotImplementedError({('Implement guardrail.' + unknown_note)!r})",
            f"{variable} = InputGuardrail({callback}, name={spec.id!r}, run_in_parallel=False)",
        ]
    if spec.kind == "output":
        return [
            f"async def {callback}(_context, _agent, _output):",
            f"    raise NotImplementedError({('Implement guardrail.' + unknown_note)!r})",
            f"{variable} = OutputGuardrail({callback}, name={spec.id!r})",
        ]
    guardrail_type = "ToolInputGuardrail" if spec.kind == "tool_input" else "ToolOutputGuardrail"
    return [
        f"async def {callback}(_data):",
        f"    raise NotImplementedError({('Implement guardrail.' + unknown_note)!r})",
        f"{variable} = {guardrail_type}({callback}, name={spec.id!r})",
    ]


def _render_tools(
    lines: list[str],
    blueprint: AgentBlueprint,
    catalog: ToolCatalog | None,
    guardrail_variables: dict[str, str],
) -> dict[str, str]:
    variables: dict[str, str] = {}
    for spec in blueprint.tools:
        variable = _identifier(f"tool_{spec.id}")
        variables[spec.id] = variable
        if isinstance(spec, FunctionToolSpec):
            _render_function_tool(lines, variable, spec, catalog, guardrail_variables)
        elif isinstance(spec, WebSearchToolSpec):
            lines.extend(
                [
                    f"{variable} = WebSearchTool(",
                    f"    search_context_size={spec.search_context_size!r},",
                    f"    external_web_access={spec.external_web_access!r},",
                    ")",
                ]
            )
        elif isinstance(spec, FileSearchToolSpec):
            lines.extend(
                [
                    f"{variable} = FileSearchTool(",
                    f"    vector_store_ids={spec.vector_store_ids!r},",
                    f"    max_num_results={spec.max_num_results!r},",
                    f"    include_search_results={spec.include_search_results!r},",
                    ")",
                ]
            )
        lines.append("")
    return variables


def _render_function_tool(
    lines: list[str],
    variable: str,
    spec: FunctionToolSpec,
    catalog: ToolCatalog | None,
    guardrail_variables: dict[str, str],
) -> None:
    runtime_tool = catalog.build_function_tool(spec) if catalog is not None else None
    name = (
        runtime_tool.name
        if runtime_tool is not None
        else spec.name or _identifier(spec.catalog_id.replace(".", "_"))
    )
    description = (
        runtime_tool.description
        if runtime_tool is not None
        else spec.description or spec.catalog_id
    )
    schema = (
        runtime_tool.params_json_schema
        if runtime_tool is not None
        else {"type": "object", "properties": {}, "additionalProperties": True}
    )
    output_schema = runtime_tool.output_json_schema if runtime_tool is not None else None
    strict = runtime_tool.strict_json_schema if runtime_tool is not None else False
    callback = _identifier(f"invoke_{spec.id}")
    input_guardrails = [guardrail_variables[item] for item in spec.input_guardrail_ids]
    output_guardrails = [guardrail_variables[item] for item in spec.output_guardrail_ids]
    lines.extend(
        [
            f"async def {callback}(_context, arguments: str):",
            "    parsed_arguments = json.loads(arguments)",
            f"    raise NotImplementedError({('Implement ' + spec.catalog_id + ': ' + repr(schema))!r})",
            "",
            f"{variable} = FunctionTool(",
            f"    name={name!r},",
            f"    description={description!r},",
            f"    params_json_schema={_literal(schema)},",
            f"    on_invoke_tool={callback},",
            f"    strict_json_schema={strict!r},",
            f"    needs_approval={spec.needs_approval!r},",
            f"    tool_input_guardrails=[{', '.join(input_guardrails)}],",
            f"    tool_output_guardrails=[{', '.join(output_guardrails)}],",
            f"    output_json_schema={_literal(output_schema)},",
            ")",
        ]
    )


def _render_agents(lines: list[str], blueprint: AgentBlueprint) -> None:
    guardrails = {item.id: _identifier(f"guardrail_{item.id}") for item in blueprint.guardrails}
    for spec in blueprint.agents:
        settings = spec.model_settings.model_dump(exclude_none=True)
        output = (
            "None"
            if spec.output is None
            else (
                "PersistedJsonSchema("
                f"{spec.output.name!r}, {_literal(spec.output.schema_)}, {spec.output.strict!r}"
                ")"
            )
        )
        model_name = spec.model.model
        lines.extend(
            [
                f"{_identifier('agent_' + spec.id)} = Agent(",
                f"    name={spec.name!r},",
                f"    handoff_description={spec.description!r},",
                f"    instructions={with_global_agent_instructions(spec.instructions)!r},",
                f"    model={model_name!r},",
                f"    model_settings=ModelSettings(**{_literal(settings)}),",
                f"    output_type={output},",
                f"    input_guardrails=[{', '.join(guardrails[item] for item in spec.input_guardrail_ids)}],",
                f"    output_guardrails=[{', '.join(guardrails[item] for item in spec.output_guardrail_ids)}],",
                f"    tool_use_behavior={spec.tool_use_behavior!r},",
                f"    reset_tool_choice={spec.reset_tool_choice!r},",
                ")",
                "",
            ]
        )


def _render_relationships(
    lines: list[str],
    blueprint: AgentBlueprint,
    tool_variables: dict[str, str],
) -> None:
    handoffs_by_source: dict[str, list[str]] = {}
    for relation in blueprint.handoffs:
        expression = (
            f"handoff({_identifier('agent_' + relation.target_agent_id)}, "
            f"tool_name_override={relation.tool_name!r}, "
            f"tool_description_override={relation.tool_description!r}, "
            f"nest_handoff_history={relation.nest_handoff_history!r})"
        )
        handoffs_by_source.setdefault(relation.source_agent_id, []).append(expression)
    agent_tools_by_owner: dict[str, list[str]] = {}
    for relation in blueprint.agent_tools:
        expression = (
            f"{_identifier('agent_' + relation.delegate_agent_id)}.as_tool("
            f"tool_name={relation.tool_name!r}, "
            f"tool_description={relation.tool_description!r}, "
            f"max_turns={relation.max_turns!r}, "
            f"needs_approval={relation.needs_approval!r})"
        )
        agent_tools_by_owner.setdefault(relation.owner_agent_id, []).append(expression)
    for spec in blueprint.agents:
        variable = _identifier("agent_" + spec.id)
        bound = [tool_variables[tool_id] for tool_id in spec.tool_ids]
        bound.extend(agent_tools_by_owner.get(spec.id, []))
        lines.append(f"{variable}.tools = [{', '.join(bound)}]")
        lines.append(
            f"{variable}.handoffs = [{', '.join(handoffs_by_source.get(spec.id, []))}]"
        )


def _render_session_and_runner(
    lines: list[str],
    blueprint: AgentBlueprint,
) -> None:
    entry = _identifier("agent_" + blueprint.entry_agent_id)
    run_settings = blueprint.run
    lines.extend(
        [
            "",
            "SESSION = SQLiteSession(",
            "    'scholarweave-export',",
            "    'scholarweave-agent-sessions.sqlite3',",
            ")",
        ]
    )
    lines.extend(
        [
            "RUN_CONFIG = RunConfig(",
            f"    workflow_name={blueprint.name!r},",
            f"    tracing_disabled={not run_settings.tracing_enabled!r},",
            "    tool_execution=ToolExecutionConfig(",
            f"        max_function_tool_concurrency={run_settings.max_tool_concurrency!r}",
            "    ),",
            ")",
            "",
            "",
            "async def main() -> None:",
            "    result = await Runner.run(",
            f"        {entry},",
            "        input('Prompt: '),",
            f"        max_turns={run_settings.max_turns},",
            "        run_config=RUN_CONFIG,",
            "        session=SESSION,",
            "    )",
            "    print(result.final_output)",
            "",
            "",
            "if __name__ == '__main__':",
            "    asyncio.run(main())",
            "",
        ]
    )


def _literal(value: Any) -> str:
    return repr(value)


def _identifier(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_]", "_", value)
    if not sanitized or sanitized[0].isdigit():
        sanitized = f"_{sanitized}"
    return sanitized
