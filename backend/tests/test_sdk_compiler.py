from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from agents import (
    FunctionTool,
    Model,
    ModelBehaviorError,
    ModelResponse,
    ModelSettings,
    TResponseInputItem,
    Usage,
)

from backend.agents.blueprint import AgentBlueprint, ReasoningSpec
from backend.agents.catalog import FunctionToolDefinition, ToolCatalog
from backend.agents.compiler import AgentCompiler, MAX_AGENT_TOOL_DEPTH
from backend.agents.export import export_agent
from backend.agents.guardrails import create_guardrail_catalog
from backend.agents.instructions import (
    GLOBAL_AGENT_INSTRUCTIONS,
    with_global_agent_instructions,
)
from backend.agents.output import JsonSchemaOutput
from backend.core.errors import ValidationError
from backend.providers.types import ModelReference, ResolvedAgentModel


class NoopModel(Model):
    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools,
        output_schema,
        handoffs,
        tracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt,
    ) -> ModelResponse:
        return ModelResponse(output=[], usage=Usage(), response_id=None)

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


class Resolver:
    def __init__(self, *, hosted: bool = True) -> None:
        self.hosted = hosted

    def resolve_agent_model(
        self,
        reference: ModelReference,
        *,
        require_tools: bool = False,
    ) -> ResolvedAgentModel:
        return ResolvedAgentModel(
            model=NoopModel(),
            provider_kind="openai" if self.hosted else "ollama",
            supports_responses=self.hosted,
            supports_hosted_tools=self.hosted,
            supports_parallel_tool_calls=self.hosted,
        )


def tool_catalog() -> ToolCatalog:
    catalog = ToolCatalog()

    def build(spec) -> FunctionTool:
        async def invoke(_context, arguments: str) -> str:
            return json.dumps({"echo": json.loads(arguments)})

        return FunctionTool(
            name=spec.name or "echo",
            description=spec.description or "Echoes input.",
            params_json_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
            on_invoke_tool=invoke,
            needs_approval=spec.needs_approval,
        )

    catalog.register_function_tool(
        FunctionToolDefinition("builtin.echo", "Echo", "Echoes input.", build)
    )
    return catalog


def blueprint(**overrides) -> AgentBlueprint:
    payload = {
        "name": "Research team",
        "entry_agent_id": "triage",
        "tools": [
            {
                "id": "echo-tool",
                "kind": "function",
                "catalog_id": "builtin.echo",
            }
        ],
        "agents": [
            {
                "id": "triage",
                "name": "Triage",
                "instructions": "Delegate detailed work.",
                "tool_ids": ["echo-tool"],
            },
            {
                "id": "researcher",
                "name": "Researcher",
                "instructions": "Research carefully.",
                "output": {
                    "kind": "json_schema",
                    "name": "Finding",
                    "schema": {
                        "type": "object",
                        "properties": {"answer": {"type": "string"}},
                        "required": ["answer"],
                        "additionalProperties": False,
                    },
                },
            },
        ],
        "handoffs": [
            {
                "id": "to-researcher",
                "source_agent_id": "triage",
                "target_agent_id": "researcher",
            }
        ],
        "agent_tools": [
            {
                "id": "research-tool",
                "owner_agent_id": "triage",
                "delegate_agent_id": "researcher",
                "tool_name": "ask_researcher",
                "tool_description": "Ask the research specialist.",
            }
        ],
    }
    payload.update(overrides)
    return AgentBlueprint.model_validate(payload)


def test_compiler_builds_real_sdk_topology() -> None:
    compiled = AgentCompiler(Resolver(), tool_catalog()).compile(blueprint())

    assert compiled.entry_agent is compiled.agents_by_id["triage"]
    assert [tool.name for tool in compiled.entry_agent.tools] == ["echo", "ask_researcher"]
    assert len(compiled.entry_agent.handoffs) == 1
    assert compiled.entry_agent.handoffs[0].agent_name == "Researcher"
    assert compiled.agents_by_id["researcher"].output_type is not None
    assert GLOBAL_AGENT_INSTRUCTIONS in compiled.entry_agent.instructions
    assert GLOBAL_AGENT_INSTRUCTIONS in compiled.agents_by_id["researcher"].instructions
    assert "Structured output requirement:" in compiled.agents_by_id["researcher"].instructions
    assert "Return only one JSON value matching the Finding schema" in (
        compiled.agents_by_id["researcher"].instructions
    )
    assert '"required":["answer"]' in compiled.agents_by_id["researcher"].instructions
    assert "System information:\nCurrent date:" in compiled.entry_agent.instructions
    assert "\nCurrent time:" in compiled.entry_agent.instructions
    assert compiled.entry_agent.model_settings.include_usage is True
    assert compiled.max_turns == 10


def test_json_schema_output_accepts_fenced_json() -> None:
    output = JsonSchemaOutput(
        "Finding",
        {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
        strict=True,
    )

    assert output.validate_json('```json\n{"answer":"done"}\n```') == {"answer": "done"}


def test_json_schema_output_reports_plain_text_as_model_behavior_error() -> None:
    output = JsonSchemaOutput(
        "Finding",
        {"type": "object"},
        strict=True,
    )

    with pytest.raises(ModelBehaviorError, match="invalid JSON.*response started with"):
        output.validate_json("Here is the requested analysis.")


def test_compiler_maps_reasoning_effort_to_sdk_settings() -> None:
    source = blueprint()
    source.agents[0].model_settings.reasoning = ReasoningSpec(effort="xhigh")

    compiled = AgentCompiler(Resolver(), tool_catalog()).compile(source)

    assert compiled.entry_agent.model_settings.reasoning is not None
    assert compiled.entry_agent.model_settings.reasoning.effort == "xhigh"


def test_global_instructions_include_current_date_and_time() -> None:
    instructions = with_global_agent_instructions(
        "Research carefully.",
        at=datetime(2026, 8, 9, 1, 29, 15, tzinfo=UTC),
    )

    assert "Current date: 2026-08-09" in instructions
    assert "Current time: 01:29:15 UTC (UTC+00:00)" in instructions


def test_compiler_rejects_missing_references() -> None:
    invalid = blueprint(entry_agent_id="missing")
    with pytest.raises(ValidationError) as raised:
        AgentCompiler(Resolver(), tool_catalog()).compile(invalid)
    assert "Entry agent 'missing' does not exist." in raised.value.issues


@pytest.mark.parametrize(
    ("overrides", "expected_issue"),
    [
        (
            {
                "handoffs": [
                    {
                        "id": "self-handoff",
                        "source_agent_id": "triage",
                        "target_agent_id": "triage",
                    }
                ],
                "agent_tools": [],
            },
            "Handoff 'self-handoff' cannot target its source agent.",
        ),
        (
            {
                "handoffs": [],
                "agent_tools": [
                    {
                        "id": "self-tool",
                        "owner_agent_id": "triage",
                        "delegate_agent_id": "triage",
                        "tool_name": "ask_self",
                        "tool_description": "Delegate to self.",
                    }
                ],
            },
            "Agent tool 'self-tool' cannot delegate to its owner agent.",
        ),
    ],
)
def test_compiler_rejects_self_referencing_relationships(
    overrides: dict[str, object],
    expected_issue: str,
) -> None:
    invalid = blueprint(**overrides)
    with pytest.raises(ValidationError) as raised:
        AgentCompiler(Resolver(), tool_catalog()).compile(invalid)
    assert expected_issue in raised.value.issues


def test_compiler_allows_two_nested_agent_tool_levels() -> None:
    source = blueprint()
    source.agents.append(
        source.agents[1].model_copy(
            update={
                "id": "reviewer",
                "name": "Reviewer",
                "instructions": "Review the focused result.",
                "output": None,
            }
        )
    )
    source.agent_tools.append(
        source.agent_tools[0].model_copy(
            update={
                "id": "review-tool",
                "owner_agent_id": "researcher",
                "delegate_agent_id": "reviewer",
                "tool_name": "ask_reviewer",
                "tool_description": "Ask the reviewer to check the focused result.",
            }
        )
    )

    compiled = AgentCompiler(Resolver(), tool_catalog()).compile(source)

    assert MAX_AGENT_TOOL_DEPTH == 2
    assert [tool.name for tool in compiled.agents_by_id["researcher"].tools] == [
        "ask_reviewer"
    ]


def test_compiler_rejects_third_nested_agent_tool_level() -> None:
    source = blueprint()
    for agent_id in ("reviewer", "critic"):
        source.agents.append(
            source.agents[1].model_copy(
                update={
                    "id": agent_id,
                    "name": agent_id.title(),
                    "instructions": f"Act as the {agent_id}.",
                    "output": None,
                }
            )
        )
    source.agent_tools.extend(
        [
            source.agent_tools[0].model_copy(
                update={
                    "id": "review-tool",
                    "owner_agent_id": "researcher",
                    "delegate_agent_id": "reviewer",
                    "tool_name": "ask_reviewer",
                }
            ),
            source.agent_tools[0].model_copy(
                update={
                    "id": "critic-tool",
                    "owner_agent_id": "reviewer",
                    "delegate_agent_id": "critic",
                    "tool_name": "ask_critic",
                }
            ),
        ]
    )

    with pytest.raises(ValidationError) as raised:
        AgentCompiler(Resolver(), tool_catalog()).compile(source)

    assert any(
        "exceeds maximum depth 2" in issue
        for issue in raised.value.issues
    )


def test_compiler_rejects_agent_tool_cycles() -> None:
    source = blueprint()
    source.agent_tools.append(
        source.agent_tools[0].model_copy(
            update={
                "id": "return-tool",
                "owner_agent_id": "researcher",
                "delegate_agent_id": "triage",
                "tool_name": "ask_triage",
            }
        )
    )

    with pytest.raises(ValidationError) as raised:
        AgentCompiler(Resolver(), tool_catalog()).compile(source)

    assert any("contains a cycle" in issue for issue in raised.value.issues)


def test_handoffs_cannot_bridge_around_agent_tool_depth_limit() -> None:
    source = blueprint()
    for agent_id in ("reviewer", "critic", "leaf"):
        source.agents.append(
            source.agents[1].model_copy(
                update={
                    "id": agent_id,
                    "name": agent_id.title(),
                    "instructions": f"Act as the {agent_id}.",
                    "output": None,
                }
            )
        )
    source.handoffs.append(
        source.handoffs[0].model_copy(
            update={
                "id": "researcher-to-reviewer",
                "source_agent_id": "researcher",
                "target_agent_id": "reviewer",
            }
        )
    )
    source.agent_tools.extend(
        [
            source.agent_tools[0].model_copy(
                update={
                    "id": "review-tool",
                    "owner_agent_id": "reviewer",
                    "delegate_agent_id": "critic",
                    "tool_name": "ask_critic",
                }
            ),
            source.agent_tools[0].model_copy(
                update={
                    "id": "critic-tool",
                    "owner_agent_id": "critic",
                    "delegate_agent_id": "leaf",
                    "tool_name": "ask_leaf",
                }
            ),
        ]
    )

    with pytest.raises(ValidationError) as raised:
        AgentCompiler(Resolver(), tool_catalog()).compile(source)

    assert any(
        "exceeds maximum depth 2" in issue
        for issue in raised.value.issues
    )


def test_compiler_rejects_hosted_tool_for_local_model() -> None:
    local_blueprint = AgentBlueprint.model_validate(
        {
            "name": "Local",
            "entry_agent_id": "local",
            "agents": [
                {
                    "id": "local",
                    "name": "Local",
                    "instructions": "Search.",
                    "tool_ids": ["search"],
                }
            ],
            "tools": [{"id": "search", "kind": "web_search"}],
        }
    )
    with pytest.raises(ValidationError, match="Responses-compatible"):
        AgentCompiler(Resolver(hosted=False), tool_catalog()).compile(local_blueprint)


def test_blueprint_rejects_parallel_runtime_fields() -> None:
    with pytest.raises(ValueError):
        AgentBlueprint.model_validate(
            {
                "name": "Invalid",
                "entry_agent_id": "agent",
                "agents": [
                    {
                        "id": "agent",
                        "name": "Agent",
                        "instructions": "Do work.",
                        "ports": [],
                    }
                ],
                "nodes": [],
                "edges": [],
            }
        )


def test_compiler_binds_agent_and_tool_guardrails() -> None:
    guarded = AgentBlueprint.model_validate(
        {
            "name": "Guarded",
            "entry_agent_id": "agent",
            "agents": [
                {
                    "id": "agent",
                    "name": "Agent",
                    "instructions": "Use the guarded tool.",
                    "tool_ids": ["echo-tool"],
                    "input_guardrail_ids": ["input-limit"],
                    "output_guardrail_ids": ["output-limit"],
                }
            ],
            "tools": [
                {
                    "id": "echo-tool",
                    "kind": "function",
                    "catalog_id": "builtin.echo",
                    "input_guardrail_ids": ["tool-input-limit"],
                    "output_guardrail_ids": ["tool-output-limit"],
                }
            ],
            "guardrails": [
                {
                    "id": "input-limit",
                    "kind": "input",
                    "catalog_id": "content.max_characters",
                    "config": {"max_characters": 100},
                },
                {
                    "id": "output-limit",
                    "kind": "output",
                    "catalog_id": "content.max_characters",
                    "config": {"max_characters": 100},
                },
                {
                    "id": "tool-input-limit",
                    "kind": "tool_input",
                    "catalog_id": "content.max_characters",
                    "config": {"max_characters": 100},
                },
                {
                    "id": "tool-output-limit",
                    "kind": "tool_output",
                    "catalog_id": "content.max_characters",
                    "config": {"max_characters": 100},
                },
            ],
        }
    )

    compiled = AgentCompiler(
        Resolver(),
        tool_catalog(),
        create_guardrail_catalog(),
    ).compile(guarded)

    assert [guardrail.get_name() for guardrail in compiled.entry_agent.input_guardrails] == [
        "input-limit"
    ]
    assert [guardrail.get_name() for guardrail in compiled.entry_agent.output_guardrails] == [
        "output-limit"
    ]
    function_tool = compiled.entry_agent.tools[0]
    assert [guardrail.get_name() for guardrail in function_tool.tool_input_guardrails] == [
        "tool-input-limit"
    ]
    assert [guardrail.get_name() for guardrail in function_tool.tool_output_guardrails] == [
        "tool-output-limit"
    ]


def test_python_export_preserves_sdk_topology_and_exact_tool_schema() -> None:
    definition = blueprint()
    source = export_agent(definition, tool_catalog=tool_catalog())

    compile(source, "exported_agent.py", "exec")
    assert "double-dollar delimiters" in source
    assert "$$<math>$$" in source

    assert "from agents import (" in source
    assert "params_json_schema={'type': 'object', 'properties': {'text': {'type': 'string'}}" in source
    assert "PersistedJsonSchema('Finding'" in source
    assert "agent_researcher.as_tool(" in source
    assert "handoff(agent_researcher" in source
    assert "Runner.run(" in source
    assert "run_config=RUN_CONFIG" in source
    assert "session=SESSION" in source
