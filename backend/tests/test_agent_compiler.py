from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from backend.agents.blueprint import AgentBlueprint, ReasoningSpec
from backend.agents.catalog import FunctionToolDefinition, ToolCatalog
from backend.agents.compiler import (
    GLOBAL_AGENT_INSTRUCTIONS,
    AgentCompiler,
    with_global_agent_instructions,
    with_reasoning_effort,
)
from backend.agents.context import ScholarWeaveContext
from backend.agents.harness import (
    MAX_DELEGATION_DEPTH,
    FunctionTool,
    JsonSchemaOutput,
    ModelBehaviorError,
    ModelBinding,
    ToolInvocation,
    request_parameters,
)
from backend.core.errors import ValidationError
from backend.conversations.turns import deep_work_blueprint, research_blueprint
from backend.providers.types import ModelReference, ProviderRuntimeError, ResolvedAgentModel
from backend.tests.harness_support import FakeClient, StubResolver, stub_binding
from backend.tools.catalog import create_tool_catalog


class Resolver:
    def __init__(self, *, parallel: bool = True, context_window: int | None = None) -> None:
        self.parallel = parallel
        self.context_window = context_window

    def resolve_agent_model(
        self,
        reference: ModelReference,
        *,
        require_tools: bool = False,
    ) -> ResolvedAgentModel:
        return ModelBinding(
            client=FakeClient.scripted([[]]),
            model_name="stub-model",
            provider_kind="openai" if self.parallel else "ollama",
            supports_parallel_tool_calls=self.parallel,
            context_window_tokens=self.context_window,
        )


def tool_catalog() -> ToolCatalog:
    catalog = ToolCatalog()

    def build(spec) -> FunctionTool:
        async def invoke(_invocation, arguments: str) -> str:
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


def test_compiler_builds_native_agent_topology() -> None:
    compiled = AgentCompiler(Resolver(), tool_catalog()).compile(blueprint())

    assert compiled.entry_agent is compiled.agents_by_id["triage"]
    assert [tool.name for tool in compiled.entry_agent.tools] == ["echo", "ask_researcher"]
    assert compiled.agents_by_id["researcher"].output_schema is not None
    assert GLOBAL_AGENT_INSTRUCTIONS in compiled.entry_agent.instructions
    assert GLOBAL_AGENT_INSTRUCTIONS in compiled.agents_by_id["researcher"].instructions
    assert "Structured output requirement:" in compiled.agents_by_id["researcher"].instructions
    assert "Return only one JSON value matching the Finding schema" in (
        compiled.agents_by_id["researcher"].instructions
    )
    assert '"required":["answer"]' in compiled.agents_by_id["researcher"].instructions
    assert "System information:\nCurrent date:" in compiled.entry_agent.instructions
    assert "\nCurrent time:" in compiled.entry_agent.instructions
    assert compiled.max_turns is None
    assert compiled.run_settings.max_turns is None


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_compiler_resolves_optional_compaction_model_once(test_settings, configured, enabled) -> None:
    calls = []

    class TrackingResolver(Resolver):
        def resolve_agent_model(self, reference, *, require_tools=False):
            calls.append((reference, require_tools))
            return super().resolve_agent_model(reference, require_tools=require_tools)

    test_settings.agent_context_model_summary_enabled = enabled
    test_settings.default_model_references = (
        {"compaction": {"provider_profile_id": "local", "model": "gemma4-12b"}}
        if configured else {}
    )
    compiled = AgentCompiler(TrackingResolver(), tool_catalog(), test_settings).compile(blueprint(
        tools=[], agent_tools=[], agents=[
            {"id": "triage", "name": "Main", "instructions": "Main"},
            {"id": "researcher", "name": "Researcher", "instructions": "Research"},
        ],
    ))
    assert len(calls) == 2 + int(configured and enabled)
    assert compiled.context_policy is not None
    assert (compiled.context_policy._compaction_model is not None) == (configured and enabled)
    if configured and enabled:
        assert calls[-1] == (ModelReference("local", "gemma4-12b"), False)
    assert set(compiled.resolved_models) == {"triage", "researcher"}


@pytest.mark.parametrize("reference", [
    {"provider_profile_id": "missing", "model": "gemma4-12b"},
    {"model": "gemma4-12b"},
    {"provider_profile_id": "local"},
])
def test_invalid_optional_compaction_reference_does_not_block_main_agent(test_settings, reference) -> None:
    calls = []

    class MissingHelperResolver(Resolver):
        def resolve_agent_model(self, selected, *, require_tools=False):
            calls.append(selected)
            if selected.model == "gemma4-12b":
                raise ProviderRuntimeError("Provider profile was not found.")
            return super().resolve_agent_model(selected, require_tools=require_tools)

    test_settings.default_model_references = {"compaction": reference}
    compiled = AgentCompiler(MissingHelperResolver(), tool_catalog(), test_settings).compile(blueprint(
        tools=[], agent_tools=[], agents=[
            {"id": "triage", "name": "Main", "instructions": "Main"},
            {"id": "researcher", "name": "Researcher", "instructions": "Research"},
        ],
    ))
    assert compiled.context_policy is not None
    assert compiled.context_policy._compaction_model is None
    assert compiled.context_policy._compaction_model_error["error_type"] == "ProviderRuntimeError"
    assert len(calls) == (3 if len(reference) == 2 else 2)


def test_delegation_tool_exposes_one_self_contained_request_parameter() -> None:
    compiled = AgentCompiler(Resolver(), tool_catalog()).compile(blueprint())

    delegation = next(
        tool for tool in compiled.entry_agent.tools if tool.name == "ask_researcher"
    )

    assert delegation.is_delegation is True
    assert delegation.params_json_schema["required"] == ["request"]
    assert delegation.params_json_schema["additionalProperties"] is False


@pytest.mark.anyio
@pytest.mark.parametrize("max_turns", [None, 2])
async def test_compiled_delegation_has_only_opt_in_turn_limits(
    stub_provider, max_turns,
) -> None:
    stub_provider.tool_plans = [
        ("extended research", "echo", {"text": str(index)}) for index in range(101)
    ]
    model = stub_binding(stub_provider)
    payload = blueprint().model_dump(by_alias=True)
    payload["run"]["max_turns"] = 1
    payload["agents"][1]["output"] = None
    payload["agents"][1]["tool_ids"] = ["echo-tool"]
    if max_turns is not None:
        payload["agent_tools"][0]["max_turns"] = max_turns
    else:
        payload["agent_tools"][0].pop("max_turns")
    compiled = AgentCompiler(StubResolver(model), tool_catalog()).compile(
        AgentBlueprint.model_validate(payload)
    )
    delegation = compiled.entry_agent.tools[1]
    context = ScholarWeaveContext(run_id="run-1", tool_runtime=SimpleNamespace())
    try:
        output = await delegation.on_invoke_tool(
            ToolInvocation(context, "delegate-1", delegation.name, "Triage"),
            json.dumps({"request": "Complete this extended research."}),
        )
    finally:
        await model.client.close()

    if max_turns is None:
        assert output == stub_provider.reply
        assert len(stub_provider.requests) == 102
    else:
        assert "whole 2-turn budget" in output
        assert len(stub_provider.requests) == 2


@pytest.mark.parametrize("max_turns", [None, 1, 101])
def test_blueprint_accepts_optional_positive_turn_limits(max_turns) -> None:
    payload = blueprint(run={"max_turns": max_turns}).model_dump(by_alias=True)
    payload["agent_tools"][0]["max_turns"] = max_turns
    spec = AgentBlueprint.model_validate(payload)
    assert spec.run.max_turns == max_turns
    assert spec.agent_tools[0].max_turns == max_turns


@pytest.mark.parametrize("max_turns", [0, -1])
@pytest.mark.parametrize("target", ["run", "delegate"])
def test_blueprint_rejects_nonpositive_turn_limits(max_turns, target) -> None:
    payload = blueprint().model_dump(by_alias=True)
    settings = payload["run"] if target == "run" else payload["agent_tools"][0]
    settings["max_turns"] = max_turns
    with pytest.raises(ValueError):
        AgentBlueprint.model_validate(payload)


def test_compiler_applies_explicit_context_window_to_every_agent() -> None:
    compiler = AgentCompiler(
        Resolver(),
        tool_catalog(),
        settings=SimpleNamespace(
            user_timezone=None,
            user_profile=None,
            agent_context_window_tokens=32_768,
            agent_context_high_water_ratio=0.7,
            agent_context_compaction_target_tokens=8_192,
            tool_result_max_tokens=3_000,
        ),
    )
    payload = blueprint().model_dump(by_alias=True)
    payload["tools"] = []
    payload["agent_tools"] = []
    for agent in payload["agents"]:
        agent["tool_ids"] = []

    compiled = compiler.compile(
        AgentBlueprint.model_validate(payload),
        context_window_tokens=65_536,
    )

    assert compiled.context_policy is not None
    assert (
        compiled.context_policy.context_window_tokens(compiled.agents_by_id["triage"])
        == 65_536
    )
    assert (
        compiled.context_policy.context_window_tokens(
            compiled.agents_by_id["researcher"]
        )
        == 65_536
    )
    assert compiled.context_window_tokens == 65_536
    assert all(not agent.tools for agent in compiled.agents_by_id.values())


@pytest.mark.parametrize(
    ("deep_work", "web_enabled", "fast_answer"),
    [
        (False, True, False),
        (False, False, False),
        (False, True, True),
        (True, True, False),
        (True, False, False),
    ],
)
def test_product_agents_can_recover_cached_context(
    test_settings, deep_work, web_enabled, fast_answer,
) -> None:
    spec = (
        deep_work_blueprint({}, web_enabled=web_enabled)
        if deep_work
        else research_blueprint({}, web_enabled=web_enabled, fast_answer=fast_answer)
    )
    compiled = AgentCompiler(
        Resolver(), create_tool_catalog(), settings=test_settings,
    ).compile(spec)
    context = ScholarWeaveContext(run_id="run-1", tool_runtime=SimpleNamespace())

    assert compiled.context_policy is not None
    for definition in compiled.agents_by_id.values():
        names = [tool.name for tool in definition.enabled_tools(context)]
        assert names.count("read_tool_result") == 1


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
    output = JsonSchemaOutput("Finding", {"type": "object"}, strict=True)

    with pytest.raises(ModelBehaviorError, match="invalid JSON.*response started with"):
        output.validate_json("Here is the requested analysis.")


def test_compiler_maps_reasoning_effort_to_request_settings() -> None:
    source = blueprint()
    source.agents[0].model_settings.reasoning = ReasoningSpec(effort="xhigh")

    compiled = AgentCompiler(Resolver(), tool_catalog()).compile(source)

    assert compiled.entry_agent.model_settings.reasoning_effort == "xhigh"
    assert request_parameters(compiled.entry_agent, [], [])["reasoning_effort"] == "xhigh"


def test_run_reasoning_effort_override_reaches_every_agent() -> None:
    compiled = AgentCompiler(Resolver(), tool_catalog()).compile(blueprint())

    overridden = with_reasoning_effort(compiled, "high")

    assert {
        agent.model_settings.reasoning_effort
        for agent in overridden.agents_by_id.values()
    } == {"high"}
    assert overridden.entry_agent is overridden.agents_by_id["triage"]
    assert all(
        agent.model_settings.reasoning is not None
        and agent.model_settings.reasoning.effort == "high"
        for agent in overridden.blueprint.agents
    )


def _single_agent_blueprint() -> dict:
    return {
        "agents": [
            {
                "id": "triage",
                "name": "Triage",
                "instructions": "Work.",
                "tool_ids": ["echo-tool"],
                "model_settings": {"parallel_tool_calls": True},
            }
        ],
        "agent_tools": [],
    }


def test_parallel_tool_calls_only_reach_providers_that_support_them() -> None:
    supported = AgentCompiler(Resolver(parallel=True), tool_catalog()).compile(
        blueprint(**_single_agent_blueprint())
    )
    unsupported = AgentCompiler(Resolver(parallel=False), tool_catalog()).compile(
        blueprint(**_single_agent_blueprint())
    )

    assert (
        request_parameters(supported.entry_agent, [], supported.entry_agent.tools)[
            "parallel_tool_calls"
        ]
        is True
    )
    assert "parallel_tool_calls" not in request_parameters(
        unsupported.entry_agent,
        [],
        unsupported.entry_agent.tools,
    )


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


def test_compiler_rejects_self_delegation() -> None:
    invalid = blueprint(
        agent_tools=[
            {
                "id": "self-tool",
                "owner_agent_id": "triage",
                "delegate_agent_id": "triage",
                "tool_name": "ask_self",
                "tool_description": "Delegate to self.",
            }
        ]
    )
    with pytest.raises(ValidationError) as raised:
        AgentCompiler(Resolver(), tool_catalog()).compile(invalid)
    assert (
        "Agent tool 'self-tool' cannot delegate to its owner agent."
        in raised.value.issues
    )


def test_compiler_allows_two_delegation_levels() -> None:
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

    assert MAX_DELEGATION_DEPTH == 2
    assert [tool.name for tool in compiled.agents_by_id["researcher"].tools] == [
        "ask_reviewer"
    ]
    assert compiled.agents_by_id["reviewer"].tools == []


def test_compiler_rejects_third_delegation_level() -> None:
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

    assert any("exceeds maximum depth 2" in issue for issue in raised.value.issues)


def test_compiler_rejects_delegation_cycles() -> None:
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


def test_compiler_rejects_colliding_tool_names() -> None:
    source = blueprint(
        agent_tools=[
            {
                "id": "research-tool",
                "owner_agent_id": "triage",
                "delegate_agent_id": "researcher",
                "tool_name": "echo",
                "tool_description": "Collides with the bound function tool.",
            }
        ]
    )

    with pytest.raises(ValidationError, match="colliding tool names"):
        AgentCompiler(Resolver(), tool_catalog()).compile(source)


def test_blueprint_rejects_retired_sdk_fields() -> None:
    for retired in ("handoffs", "guardrails"):
        with pytest.raises(ValueError):
            AgentBlueprint.model_validate(
                {
                    "name": "Invalid",
                    "entry_agent_id": "agent",
                    "agents": [
                        {"id": "agent", "name": "Agent", "instructions": "Do work."}
                    ],
                    retired: [],
                }
            )


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


def test_run_length_policy_is_a_plain_run_setting() -> None:
    compiled = AgentCompiler(Resolver(), tool_catalog()).compile(
        blueprint(run={"max_turns": 4, "max_input_characters": 64})
    )

    assert compiled.run_settings.max_input_characters == 64
    assert compiled.run_settings.max_turns == 4


def test_compiler_validate_returns_issue_tuple() -> None:
    compiler = AgentCompiler(Resolver(), tool_catalog())

    assert compiler.validate(blueprint()) == ()
    assert compiler.validate(blueprint(entry_agent_id="missing")) == (
        "Entry agent 'missing' does not exist.",
    )


def test_tool_enablement_is_evaluated_against_the_run_context() -> None:
    compiled = AgentCompiler(Resolver(), tool_catalog()).compile(
        blueprint(agent_tools=[])
    )
    echo = compiled.entry_agent.tools[0]
    echo.is_enabled = lambda context: not context.metadata.get("disabled")
    context = ScholarWeaveContext(run_id="run-1", tool_runtime=SimpleNamespace())

    assert [tool.name for tool in compiled.entry_agent.enabled_tools(context)] == ["echo"]
    context.metadata["disabled"] = True
    assert compiled.entry_agent.enabled_tools(context) == []
