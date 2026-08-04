from __future__ import annotations

import ast
import asyncio
import json

import pytest

from backend.agent_nodes import _python_inputs
from backend.agent_tools import AgentRunContext, node_as_tool
from backend.app import _RETIRED_NODE_TYPES, create_backend_services
from backend.export_python import export_workflow
from backend.models import Workflow, WorkflowVersion
from backend.nodes import _group_items
from backend.registry import NodeExecutionContext
from backend.schemas import WorkflowDefinition
from backend.templates import STARTER_WORKFLOWS


# --------------------------------------------------------------------------- map grouping


def test_group_items_joins_text_batches() -> None:
    grouped = _group_items(["a", "b", "c", "d", "e"], 2, "\n\n")
    assert grouped == ["a\n\nb", "c\n\nd", "e"]


def test_group_items_passes_through_when_ungrouped() -> None:
    assert _group_items(["a", "b"], 1, "\n") == ["a", "b"]


def test_group_items_keeps_non_text_batches_as_lists() -> None:
    grouped = _group_items([{"n": 1}, {"n": 2}, {"n": 3}], 2, "\n")
    assert grouped == [[{"n": 1}, {"n": 2}], {"n": 3}]


def _map_workflow(*, items: int, max_items: int, on_overflow: str, group_size: int = 1) -> WorkflowDefinition:
    return WorkflowDefinition.model_validate(
        {
            "name": "Mapper",
            "nodes": [
                {"id": "items", "type": "text_input", "config": {"input_key": "items"}},
                {
                    "id": "mapper",
                    "type": "map_subflow",
                    "config": {
                        "max_items": max_items,
                        "on_overflow": on_overflow,
                        "group_size": group_size,
                        "concurrency": 4,
                        "subflow": {
                            "name": "Echo item",
                            "nodes": [
                                {"id": "item", "type": "text_input", "config": {"input_key": "item"}},
                                {"id": "final", "type": "final_output"},
                            ],
                            "edges": [
                                {
                                    "source_node_id": "item",
                                    "source_port": "value",
                                    "target_node_id": "final",
                                    "target_port": "content",
                                }
                            ],
                        },
                    },
                },
                {"id": "final", "type": "final_output"},
            ],
            "edges": [
                {"source_node_id": "items", "source_port": "value", "target_node_id": "mapper", "target_port": "items"},
                {"source_node_id": "mapper", "source_port": "results", "target_node_id": "final", "target_port": "content"},
            ],
        }
    ), [f"chunk-{index}" for index in range(items)]


async def _run(services, workflow: WorkflowDefinition, inputs: dict):
    run = await services.executor.start_run(workflow, inputs)
    for _ in range(200):
        await asyncio.sleep(0.05)
        current = services.executor.load_run(run.id)
        if current and current.status in {"completed", "failed", "cancelled"}:
            return current
    raise AssertionError("Run did not finish in time")


def test_map_truncates_instead_of_failing_on_overflow(test_settings) -> None:
    services = create_backend_services(test_settings)
    workflow, items = _map_workflow(items=10, max_items=4, on_overflow="truncate")
    run = asyncio.run(_run(services, workflow, {"items": items}))

    assert run.status == "completed"
    assert len(run.output_json) == 4


def test_map_errors_on_overflow_when_asked(test_settings) -> None:
    services = create_backend_services(test_settings)
    workflow, items = _map_workflow(items=10, max_items=4, on_overflow="error")
    run = asyncio.run(_run(services, workflow, {"items": items}))

    assert run.status == "failed"
    assert "limited to 4" in (run.error or "")


def test_map_group_size_reduces_the_number_of_subflow_runs(test_settings) -> None:
    """The hierarchical-summary fix: 12 chunks batched four at a time is three LLM calls, not twelve."""
    services = create_backend_services(test_settings)
    workflow, items = _map_workflow(items=12, max_items=64, on_overflow="truncate", group_size=4)
    run = asyncio.run(_run(services, workflow, {"items": items}))

    assert run.status == "completed"
    assert len(run.output_json) == 3
    assert run.output_json[0] == "chunk-0\n\nchunk-1\n\nchunk-2\n\nchunk-3"


def test_map_handles_more_items_than_the_old_ceiling(test_settings) -> None:
    """Regression for the original failure: 124 chunks used to raise before a single call."""
    services = create_backend_services(test_settings)
    workflow, items = _map_workflow(items=124, max_items=512, on_overflow="truncate", group_size=8)
    run = asyncio.run(_run(services, workflow, {"items": items}))

    assert run.status == "completed"
    assert len(run.output_json) == 16


# --------------------------------------------------------------------------- python node inputs


def test_python_inputs_are_named_after_their_source_port() -> None:
    payload = _python_inputs([["a", "b"]], {"document_id": "doc-1"}, ["chunks"])

    assert payload["chunks"] == ["a", "b"]
    assert payload["value"] == ["a", "b"]
    assert payload["workflow"] == {"document_id": "doc-1"}


def test_python_inputs_fall_back_to_positional_names() -> None:
    payload = _python_inputs(["x", "y"], {}, [])

    assert payload["input_0"] == "x"
    assert payload["input_1"] == "y"
    assert payload["values"] == ["x", "y"]


def test_python_inputs_spread_a_lone_dict_when_unnamed() -> None:
    payload = _python_inputs([{"question": "why?"}], {}, [])

    assert payload["question"] == "why?"


def test_python_node_runs_user_code(test_settings) -> None:
    services = create_backend_services(test_settings)
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "Counter",
            "nodes": [
                {"id": "words", "type": "text_input", "config": {"input_key": "words"}},
                {
                    "id": "code",
                    "type": "python_code",
                    "config": {"code": "def transform(inputs):\n    return len(inputs['value'])\n"},
                },
                {"id": "final", "type": "final_output"},
            ],
            "edges": [
                {"source_node_id": "words", "source_port": "value", "target_node_id": "code", "target_port": "inputs"},
                {"source_node_id": "code", "source_port": "value", "target_node_id": "final", "target_port": "content"},
            ],
        }
    )
    run = asyncio.run(_run(services, workflow, {"words": ["a", "b", "c"]}))

    assert run.status == "completed"
    assert run.output_json in (3, "3")


def test_python_node_reports_a_blocked_import(test_settings) -> None:
    services = create_backend_services(test_settings)
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "Blocked",
            "nodes": [
                {
                    "id": "code",
                    "type": "python_code",
                    "config": {"code": "import socket\n\ndef transform(inputs):\n    return 1\n"},
                },
                {"id": "final", "type": "final_output"},
            ],
            "edges": [
                {"source_node_id": "code", "source_port": "value", "target_node_id": "final", "target_port": "content"}
            ],
        }
    )
    run = asyncio.run(_run(services, workflow, {}))

    assert run.status == "failed"
    assert "allowlist" in (run.error or "").lower() or "import" in (run.error or "").lower()


# --------------------------------------------------------------------------- node-as-tool bridge


def _tool_context(services) -> NodeExecutionContext:
    async def emit(_event: str, _payload: dict) -> None:
        return None

    async def execute_subflow(_definition, _inputs, _path):
        raise AssertionError("not used")

    return NodeExecutionContext(
        services=services,
        run_id="run-1",
        node_path="retrieve",
        node_run_id="node-run-1",
        run_inputs={},
        emit=emit,
        check_cancelled=lambda: None,
        execute_subflow=execute_subflow,
    )


def test_agent_uses_chat_default_until_tools_or_handoffs_are_present(test_settings) -> None:
    services = create_backend_services(test_settings)
    node = services.registry.get("agent")
    config = node.validate_config({})
    context = _tool_context(services)
    seen: list[str] = []

    class ResolutionCaptured(Exception):
        pass

    def capture(capability: str, **_kwargs):
        seen.append(capability)
        raise ResolutionCaptured

    original = services.model_runtime.resolve
    services.model_runtime.resolve = capture  # type: ignore[method-assign]
    try:
        with pytest.raises(ResolutionCaptured):
            node._build(context, {}, config)
        with pytest.raises(ResolutionCaptured):
            node._build(context, {"tools": [object()]}, config)
        with pytest.raises(ResolutionCaptured):
            node._build(context, {"handoffs": [object()]}, config)
        context.connected_inputs = frozenset({"tools"})
        with pytest.raises(ResolutionCaptured):
            node._build(context, {}, config)
    finally:
        services.model_runtime.resolve = original  # type: ignore[method-assign]

    assert seen == ["chat", "tools", "tools", "tools"]


def test_node_as_tool_exposes_a_schema_and_pins_wired_values(test_settings) -> None:
    """A canvas-wired document must win over whatever the model puts in the arguments."""
    services = create_backend_services(test_settings)
    node = services.registry.get("select_document")
    config = node.config_model.model_validate({})
    context = _tool_context(services)

    seen: dict = {}

    async def fake_execute(_context, inputs, _config):
        seen.update(inputs)
        return {"text": "ok"}

    original = node.execute
    node.execute = fake_execute  # type: ignore[method-assign]
    try:
        tool = node_as_tool(node, config, context, static_inputs={"document_id": "pinned-doc"})

        assert tool.name == "load_document"
        # A pinned value is hidden from the model: asking for an ID it was never given makes
        # small local models refuse to call the tool at all.
        assert "document_id" not in json.dumps(tool.params_json_schema)

        asyncio.run(tool.on_invoke_tool(None, json.dumps({"document_id": "model-guessed-doc"})))
    finally:
        node.execute = original  # type: ignore[method-assign]

    assert seen["document_id"] == "pinned-doc"


def test_node_as_tool_advertises_arguments_the_model_must_supply(test_settings) -> None:
    services = create_backend_services(test_settings)
    node = services.registry.get("vector_retrieve")
    config = node.config_model.model_validate({})
    tool = node_as_tool(node, config, _tool_context(services), static_inputs={"document_id": "pinned-doc"})

    schema = json.dumps(tool.params_json_schema)
    assert "document_id" not in schema
    assert "query" in schema


def test_node_as_tool_returns_failures_as_text_for_the_model(test_settings) -> None:
    services = create_backend_services(test_settings)
    node = services.registry.get("select_document")
    config = node.config_model.model_validate({})

    async def failing(_context, _inputs, _config):
        raise ValueError("no such document")

    original = node.execute
    node.execute = failing  # type: ignore[method-assign]
    try:
        tool = node_as_tool(node, config, _tool_context(services))
        result = asyncio.run(tool.on_invoke_tool(None, json.dumps({"document_id": "missing"})))
    finally:
        node.execute = original  # type: ignore[method-assign]

    assert "no such document" in str(result)


def test_tool_capable_nodes_expose_a_tool_port(test_settings) -> None:
    services = create_backend_services(test_settings)
    for node_type in ("select_document", "vector_retrieve", "keyword_retrieve", "full_context"):
        node = services.registry.get(node_type)
        assert any(port.name == "tool" for port in node.outputs), node_type


def test_a_tool_only_node_does_not_need_its_inputs(test_settings) -> None:
    """Wiring a retriever into an agent's tools port means the agent supplies the query later."""
    services = create_backend_services(test_settings)
    from backend.workflows import WorkflowValidator

    workflow = WorkflowDefinition.model_validate(
        {
            "name": "Tool wiring",
            "nodes": [
                {"id": "retrieve", "type": "vector_retrieve", "config": {}},
                {"id": "agent", "type": "agent", "config": {"name": "Analyst", "instructions": "Answer."}},
                {"id": "final", "type": "final_output"},
            ],
            "edges": [
                {"source_node_id": "retrieve", "source_port": "tool", "target_node_id": "agent", "target_port": "tools"},
                {"source_node_id": "agent", "source_port": "text", "target_node_id": "final", "target_port": "content"},
            ],
        }
    )
    result = WorkflowValidator(services.registry, services.settings).validate(workflow)

    assert result.valid, result.errors


# --------------------------------------------------------------------------- templates and export


def test_every_starter_template_validates(test_settings) -> None:
    services = create_backend_services(test_settings)
    from backend.workflows import WorkflowValidator

    validator = WorkflowValidator(services.registry, services.settings)
    for template in STARTER_WORKFLOWS:
        result = validator.validate(template)
        assert result.valid, f"{template.name}: {result.errors}"


@pytest.mark.parametrize("template", STARTER_WORKFLOWS, ids=lambda t: t.name)
def test_every_starter_template_exports_valid_python(test_settings, template) -> None:
    services = create_backend_services(test_settings)
    source = export_workflow(template, services.registry, base_url="http://localhost:11434/v1", model="gemma4:e2b")

    ast.parse(source)
    assert "from agents import" in source


def test_export_inlines_python_node_bodies(test_settings) -> None:
    services = create_backend_services(test_settings)
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "Inline",
            "nodes": [
                {
                    "id": "code",
                    "type": "python_code",
                    "config": {"code": "def transform(inputs):\n    return {'marker': 'inlined-body'}\n"},
                }
            ],
            "edges": [],
        }
    )
    source = export_workflow(workflow, services.registry, base_url="http://x/v1", model="m")

    ast.parse(source)
    assert "inlined-body" in source


# --------------------------------------------------------------------------- clean break


def test_legacy_workflows_are_retired_on_startup(test_settings) -> None:
    services = create_backend_services(test_settings)
    legacy_type = next(iter(_RETIRED_NODE_TYPES))

    with services.session_factory() as session:
        workflow = Workflow(name="Old graph", description="written on the retired nodes")
        session.add(workflow)
        session.flush()
        session.add(
            WorkflowVersion(
                workflow_id=workflow.id,
                version=1,
                definition_json={
                    "name": "Old graph",
                    "nodes": [{"id": "gen", "type": legacy_type, "config": {}}],
                    "edges": [],
                },
            )
        )
        session.commit()
        legacy_id = workflow.id

    # A second boot against the same database must clear the stale graph out.
    from backend.app import _retire_legacy_workflows

    with services.session_factory() as session:
        from backend.models import AppSetting

        stale = session.get(AppSetting, "schema_generation")
        if stale is not None:
            session.delete(stale)
            session.commit()

    _retire_legacy_workflows(services.session_factory, services.settings)

    with services.session_factory() as session:
        assert session.get(Workflow, legacy_id) is None


def test_the_map_ceiling_is_lifted_past_the_old_stored_value(test_settings) -> None:
    """A database written before the rewrite must not pin max_map_items back down to 64."""
    from backend.app import _retire_legacy_workflows
    from backend.models import AppSetting

    services = create_backend_services(test_settings)
    with services.session_factory() as session:
        session.merge(AppSetting(key="max_map_items", value_json=64))
        session.merge(AppSetting(key="schema_generation", value_json=1))
        session.commit()

    _retire_legacy_workflows(services.session_factory, services.settings)

    with services.session_factory() as session:
        stored = session.get(AppSetting, "max_map_items")
        assert stored is None or int(stored.value_json) >= 512


# --------------------------------------------------------------------------- agent nodes


def _agent_services(test_settings, stub_provider):
    test_settings.ollama_base_url = stub_provider.base_url
    test_settings.default_generation_model = "stub-model"
    test_settings.agent_provider = "ollama"
    return create_backend_services(test_settings)


def test_agent_node_runs_and_returns_text(test_settings, stub_provider) -> None:
    services = _agent_services(test_settings, stub_provider)
    stub_provider.reply = "Three findings, briefly stated."
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "Single agent",
            "nodes": [
                {"id": "topic", "type": "text_input", "config": {"input_key": "topic"}},
                {
                    "id": "agent",
                    "type": "agent",
                    "config": {"name": "Analyst", "instructions": "Summarise {{input}}."},
                },
                {"id": "final", "type": "final_output"},
            ],
            "edges": [
                {"source_node_id": "topic", "source_port": "value", "target_node_id": "agent", "target_port": "input"},
                {"source_node_id": "agent", "source_port": "text", "target_node_id": "final", "target_port": "content"},
            ],
        }
    )
    run = asyncio.run(_run(services, workflow, {"topic": "retrieval augmented generation"}))

    assert run.status == "completed", run.error
    assert run.output_json == "Three findings, briefly stated."


def test_agent_instructions_interpolate_wired_and_workflow_values(test_settings, stub_provider) -> None:
    services = _agent_services(test_settings, stub_provider)
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "Templated",
            "nodes": [
                {"id": "topic", "type": "text_input", "config": {"input_key": "topic"}},
                {
                    "id": "agent",
                    "type": "agent",
                    "config": {"name": "Analyst", "instructions": "Topic is {{input}} for {{reader}}."},
                },
                {"id": "final", "type": "final_output"},
            ],
            "edges": [
                {"source_node_id": "topic", "source_port": "value", "target_node_id": "agent", "target_port": "input"},
                {"source_node_id": "agent", "source_port": "text", "target_node_id": "final", "target_port": "content"},
            ],
        }
    )
    run = asyncio.run(_run(services, workflow, {"topic": "prompt injection", "reader": "a newcomer"}))

    assert run.status == "completed", run.error
    system = json.dumps(stub_provider.requests[0]["messages"])
    assert "prompt injection" in system
    assert "a newcomer" in system


def test_a_wired_tool_is_offered_to_the_agent_and_invoked(test_settings, stub_provider) -> None:
    services = _agent_services(test_settings, stub_provider)
    stub_provider.call_tool = "count_words"
    stub_provider.tool_arguments = {}
    stub_provider.reply = "The tool said 4."
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "Tool user",
            "nodes": [
                {
                    "id": "counter",
                    "type": "python_code",
                    "config": {
                        "code": "def transform(inputs):\n    return 4\n",
                        "tool_name": "count_words",
                        "tool_description": "Counts words.",
                    },
                },
                {
                    "id": "agent",
                    "type": "agent",
                    "config": {"name": "Analyst", "instructions": "Use your tools."},
                },
                {"id": "final", "type": "final_output"},
            ],
            "edges": [
                {"source_node_id": "counter", "source_port": "tool", "target_node_id": "agent", "target_port": "tools"},
                {"source_node_id": "agent", "source_port": "text", "target_node_id": "final", "target_port": "content"},
            ],
        }
    )
    run = asyncio.run(_run(services, workflow, {}))

    assert run.status == "completed", run.error
    assert "count_words" in stub_provider.tools_offered
    assert run.output_json == "The tool said 4."


def test_a_tool_only_node_never_runs_on_its_own(test_settings, stub_provider) -> None:
    """The counter must only execute because the agent asked, not as a DAG step."""
    services = _agent_services(test_settings, stub_provider)
    stub_provider.reply = "Done without touching the tool."
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "Unused tool",
            "nodes": [
                {
                    "id": "counter",
                    "type": "python_code",
                    "config": {
                        "code": "def transform(inputs):\n    raise RuntimeError('should never run directly')\n",
                        "tool_name": "count_words",
                        "tool_description": "Counts words.",
                    },
                },
                {"id": "agent", "type": "agent", "config": {"name": "Analyst", "instructions": "Answer."}},
                {"id": "final", "type": "final_output"},
            ],
            "edges": [
                {"source_node_id": "counter", "source_port": "tool", "target_node_id": "agent", "target_port": "tools"},
                {"source_node_id": "agent", "source_port": "text", "target_node_id": "final", "target_port": "content"},
            ],
        }
    )
    run = asyncio.run(_run(services, workflow, {}))

    assert run.status == "completed", run.error
    assert run.output_json == "Done without touching the tool."


def test_a_handoff_target_is_built_but_not_run(test_settings, stub_provider) -> None:
    services = _agent_services(test_settings, stub_provider)
    stub_provider.reply = "Triage answer."
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "Handoff",
            "nodes": [
                {
                    "id": "writer",
                    "type": "agent",
                    "config": {
                        "name": "Writer",
                        "instructions": "Write it up.",
                        "handoff_description": "Takes over once evidence is gathered.",
                    },
                },
                {"id": "triage", "type": "agent", "config": {"name": "Triage", "instructions": "Decide."}},
                {"id": "final", "type": "final_output"},
            ],
            "edges": [
                {"source_node_id": "writer", "source_port": "agent", "target_node_id": "triage", "target_port": "handoffs"},
                {"source_node_id": "triage", "source_port": "text", "target_node_id": "final", "target_port": "content"},
            ],
        }
    )
    run = asyncio.run(_run(services, workflow, {}))

    assert run.status == "completed", run.error
    assert run.output_json == "Triage answer."
    # Exactly one agent ran, and it was offered the handoff.
    assert len(stub_provider.requests) == 1
    assert any("writer" in name.lower() for name in stub_provider.tools_offered), stub_provider.tools_offered


def test_structured_output_is_parsed_onto_its_own_port(test_settings, stub_provider) -> None:
    services = _agent_services(test_settings, stub_provider)
    stub_provider.reply = '{"answer": "42", "confidence": "high"}'
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "Structured",
            "nodes": [
                {
                    "id": "agent",
                    "type": "agent",
                    "config": {
                        "name": "Analyst",
                        "instructions": "Answer as JSON.",
                        "output_schema": {
                            "type": "object",
                            "properties": {"answer": {"type": "string"}, "confidence": {"type": "string"}},
                        },
                    },
                },
                {"id": "final", "type": "final_output"},
            ],
            "edges": [
                {"source_node_id": "agent", "source_port": "output", "target_node_id": "final", "target_port": "content"}
            ],
        }
    )
    run = asyncio.run(_run(services, workflow, {}))

    assert run.status == "completed", run.error
    assert run.output_json == {"answer": "42", "confidence": "high"}


def test_a_two_level_hierarchical_summary_runs_end_to_end(test_settings, stub_provider) -> None:
    """The originally broken workflow, on the new primitives: group, summarise, then reduce."""
    services = _agent_services(test_settings, stub_provider)
    stub_provider.reply = "Section summary."
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "Hierarchical",
            "nodes": [
                {"id": "chunks", "type": "text_input", "config": {"input_key": "chunks"}},
                {
                    "id": "mapper",
                    "type": "map_subflow",
                    "config": {
                        "max_items": 512,
                        "group_size": 10,
                        "concurrency": 4,
                        "on_overflow": "truncate",
                        "subflow": {
                            "name": "Summarise a section",
                            "nodes": [
                                {"id": "item", "type": "text_input", "config": {"input_key": "item"}},
                                {
                                    "id": "summariser",
                                    "type": "agent",
                                    "config": {"name": "Summariser", "instructions": "Summarise {{input}}."},
                                },
                                {"id": "final", "type": "final_output"},
                            ],
                            "edges": [
                                {
                                    "source_node_id": "item",
                                    "source_port": "value",
                                    "target_node_id": "summariser",
                                    "target_port": "input",
                                },
                                {
                                    "source_node_id": "summariser",
                                    "source_port": "text",
                                    "target_node_id": "final",
                                    "target_port": "content",
                                },
                            ],
                        },
                    },
                },
                {"id": "join", "type": "reduce_combine", "config": {"mode": "join_text"}},
                {
                    "id": "synthesis",
                    "type": "agent",
                    "config": {"name": "Editor", "instructions": "Combine these: {{input}}."},
                },
                {"id": "final", "type": "final_output"},
            ],
            "edges": [
                {"source_node_id": "chunks", "source_port": "value", "target_node_id": "mapper", "target_port": "items"},
                {"source_node_id": "mapper", "source_port": "results", "target_node_id": "join", "target_port": "items"},
                {"source_node_id": "join", "source_port": "value", "target_node_id": "synthesis", "target_port": "input"},
                {
                    "source_node_id": "synthesis",
                    "source_port": "text",
                    "target_node_id": "final",
                    "target_port": "content",
                },
            ],
        }
    )
    chunks = [f"chunk {index}" for index in range(124)]
    run = asyncio.run(_run(services, workflow, {"chunks": chunks}))

    assert run.status == "completed", run.error
    # 124 chunks in groups of ten is 13 summariser calls plus one synthesis call.
    assert len(stub_provider.requests) == 14


# --------------------------------------------------------------------------- run recovery


def test_a_run_owned_by_a_live_process_survives_a_restart(test_settings) -> None:
    """A dev-server reload must not fail a run another process is still executing."""
    import os

    from backend.app import _recover_interrupted_runs
    from backend.models import Run

    services = create_backend_services(test_settings)
    with services.session_factory() as session:
        mine = Run(workflow_name="Orphaned", status="running", workflow_json={}, owner_pid=None)
        # A process that certainly exists but is not this one.
        theirs = Run(workflow_name="Live elsewhere", status="running", workflow_json={}, owner_pid=os.getppid())
        session.add_all([mine, theirs])
        session.commit()
        orphan_id, live_id = mine.id, theirs.id

    _recover_interrupted_runs(services.session_factory)

    with services.session_factory() as session:
        assert session.get(Run, orphan_id).status == "failed"
        assert session.get(Run, live_id).status == "running"


def test_a_run_from_a_dead_process_is_recovered(test_settings) -> None:
    from backend.app import _recover_interrupted_runs
    from backend.models import Run

    services = create_backend_services(test_settings)
    with services.session_factory() as session:
        # PID 0 is never a real user process, so this stands in for a crashed owner.
        run = Run(workflow_name="Crashed", status="running", workflow_json={}, owner_pid=0)
        session.add(run)
        session.commit()
        run_id = run.id

    _recover_interrupted_runs(services.session_factory)

    with services.session_factory() as session:
        assert session.get(Run, run_id).status == "failed"


# --------------------------------------------------------------------------- context budget


def test_fit_to_budget_keeps_the_opening_and_the_ending() -> None:
    from backend.agent_nodes import fit_to_budget

    text = "HEAD" + ("x" * 5000) + "TAIL"
    fitted, trimmed = fit_to_budget(text, 500)

    assert trimmed is True
    assert len(fitted) <= 500
    assert fitted.startswith("HEAD")
    assert fitted.endswith("TAIL")


def test_fit_to_budget_leaves_short_text_alone() -> None:
    from backend.agent_nodes import fit_to_budget

    assert fit_to_budget("short", 100) == ("short", False)


def test_an_oversized_prompt_is_trimmed_and_warned_about(test_settings, stub_provider) -> None:
    """Providers drop the front of an over-long prompt, so a paper gets summarised from its references."""
    services = _agent_services(test_settings, stub_provider)
    services.settings.max_context_chars = 800
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "Oversized",
            "nodes": [
                {"id": "notes", "type": "text_input", "config": {"input_key": "notes"}},
                {"id": "agent", "type": "agent", "config": {"name": "Editor", "instructions": "Combine."}},
                {"id": "final", "type": "final_output"},
            ],
            "edges": [
                {"source_node_id": "notes", "source_port": "value", "target_node_id": "agent", "target_port": "input"},
                {"source_node_id": "agent", "source_port": "text", "target_node_id": "final", "target_port": "content"},
            ],
        }
    )
    notes = "ABSTRACT" + ("filler " * 3000) + "CONCLUSION"
    run = asyncio.run(_run(services, workflow, {"notes": notes}))

    assert run.status == "completed", run.error
    sent = json.dumps(stub_provider.requests[0]["messages"])
    assert "ABSTRACT" in sent
    assert "CONCLUSION" in sent
    assert len(sent) < len(notes)

    with services.session_factory() as session:
        from backend.models import RunEvent
        from sqlalchemy import select

        warnings = session.scalars(
            select(RunEvent).where(RunEvent.run_id == run.id, RunEvent.event_type == "node.warning")
        ).all()
    assert warnings, "the user must be told the prompt was trimmed"


def test_vector_search_warns_when_the_paper_was_never_indexed(test_settings) -> None:
    """An unindexed paper silently returns nothing, which reads as a broken workflow."""
    services = create_backend_services(test_settings)
    with services.session_factory() as session:
        from backend.models import Document, DocumentChunk

        session.add(Document(id="doc-1", title="Unindexed", source_filename="x.pdf", status="ready"))
        session.add(
            DocumentChunk(
                id="c-1",
                document_id="doc-1",
                chunk_index=0,
                page_start=1,
                page_end=1,
                citation="Unindexed p.1",
                text="Some text",
                embedding_json=None,
            )
        )
        session.commit()

    assert services.retrieval.has_embeddings("doc-1") is False

    with services.session_factory() as session:
        from backend.models import DocumentChunk

        chunk = session.get(DocumentChunk, "c-1")
        assert chunk is not None
        chunk.embedding_json = [0.1, 0.2, 0.3]
        session.commit()

    assert services.retrieval.has_embeddings("doc-1") is True


def test_hierarchical_summary_template_files_its_notes_under_a_model_named_folder(test_settings, stub_provider) -> None:
    """The template has to leave readable notes behind, not just a run artifact."""
    services = _agent_services(test_settings, stub_provider)
    stub_provider.reply = "- A claim with a number."
    stub_provider.replies = [("You name folders", "Sparse Attention Scaling")]

    from backend.models import Document

    with services.session_factory() as session:
        session.add(
            Document(
                id="doc-1",
                title="Scaling Laws for Sparse Attention",
                source_filename="paper.pdf",
                content_type="application/pdf",
                status="ready",
            )
        )
        session.commit()
    services.retrieval.replace_document_chunks(
        "doc-1",
        [
            {"section_title": "Abstract", "page_start": 1, "page_end": 1, "citation": "p.1", "text": "We study sparsity."},
            {"section_title": "Method", "page_start": 2, "page_end": 2, "citation": "p.2", "text": "We train models."},
        ],
    )

    workflow = next(item for item in STARTER_WORKFLOWS if item.name == "Hierarchical summary")
    run = asyncio.run(_run(services, workflow, {"document_id": "doc-1"}))

    assert run.status == "completed", run.error
    notes = [note.relative_path for note in services.storage.list_workspace_markdown()]
    assert notes == ["sparse-attention-scaling/section-01.md", "sparse-attention-scaling/summary.md"]

    _, summary = services.storage.read_workspace_markdown("sparse-attention-scaling/summary.md")
    assert "Scaling Laws for Sparse Attention" in summary
