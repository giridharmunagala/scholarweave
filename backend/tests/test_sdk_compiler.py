from __future__ import annotations

import json

import pytest
from agents import FunctionTool, Model, ModelResponse, ModelSettings, TResponseInputItem, Usage

from backend.agents.blueprint import AgentBlueprint
from backend.agents.catalog import FunctionToolDefinition, ToolCatalog
from backend.agents.compiler import AgentCompiler
from backend.agents.export import export_agent
from backend.agents.guardrails import create_guardrail_catalog
from backend.agents.instructions import GLOBAL_AGENT_INSTRUCTIONS
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
    assert compiled.max_turns == 10


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
