from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select

from backend.app import create_backend_services
from backend.models import Workflow, WorkflowVersion
from backend.schemas import WorkflowDefinition
from backend.subworkflows import workflow_node_type
from backend.workflows import WorkflowValidator


def _save_workflow(services: Any, definition: WorkflowDefinition) -> str:
    with services.session_factory() as session:
        workflow = Workflow(name=definition.name, description=definition.description)
        session.add(workflow)
        session.flush()
        session.add(
            WorkflowVersion(workflow_id=workflow.id, version=1, definition_json=definition.model_dump(mode="json"))
        )
        session.commit()
        return workflow.id


def _run(services: Any, workflow: WorkflowDefinition, inputs: dict[str, Any]) -> tuple[str, Any, str | None]:
    async def runner() -> tuple[str, Any, str | None]:
        run = await services.executor.start_run(workflow, inputs)
        for _ in range(60):
            await asyncio.sleep(0.05)
            current = services.executor.load_run(run.id)
            if current and current.status in {"completed", "failed", "cancelled"}:
                return current.status, current.output_json, current.error
        raise AssertionError("Run did not finish in time")

    return asyncio.run(runner())


GREETING = WorkflowDefinition.model_validate(
    {
        "name": "Greeting",
        "description": "Builds a greeting from a person record.",
        "nodes": [
            {
                "id": "person",
                "type": "workflow_input",
                "config": {"key": "person", "kind": "json", "label": "Person", "description": "Object with a name."},
            },
            {"id": "phrase", "type": "prompt_template", "config": {"template": "Hello {name}"}},
            {"id": "out", "type": "workflow_output", "config": {"key": "greeting"}},
        ],
        "edges": [
            {"source_node_id": "person", "source_port": "value", "target_node_id": "phrase", "target_port": "variables"},
            {"source_node_id": "phrase", "source_port": "prompt", "target_node_id": "out", "target_port": "value"},
        ],
    }
)


def test_signature_is_derived_from_interface_nodes(test_settings) -> None:
    services = create_backend_services(test_settings)
    validator = WorkflowValidator(services.registry, services.settings)

    result = validator.validate(GREETING)

    assert result.valid is True
    assert [port.key for port in result.signature.inputs] == ["person"]
    assert result.signature.inputs[0].kind == "json"
    assert result.signature.inputs[0].required is True
    assert [port.key for port in result.signature.outputs] == ["greeting"]


def test_duplicate_output_keys_are_rejected(test_settings) -> None:
    services = create_backend_services(test_settings)
    validator = WorkflowValidator(services.registry, services.settings)
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "Ambiguous",
            "nodes": [
                {"id": "value", "type": "text_input", "config": {"value": "x"}},
                {"id": "a", "type": "final_output"},
                {"id": "b", "type": "final_output"},
            ],
            "edges": [
                {"source_node_id": "value", "source_port": "value", "target_node_id": "a", "target_port": "content"},
                {"source_node_id": "value", "source_port": "value", "target_node_id": "b", "target_port": "content"},
            ],
        }
    )

    result = validator.validate(workflow)

    assert result.valid is False
    assert any("Duplicate workflow output key 'result'" in error for error in result.errors)


def test_repeated_input_keys_are_declared_once(test_settings) -> None:
    """A value several nodes need is entered once, not wired by hand to each of them."""
    services = create_backend_services(test_settings)
    validator = WorkflowValidator(services.registry, services.settings)
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "Shared input",
            "nodes": [
                {"id": "a", "type": "workflow_input", "config": {"key": "topic", "kind": "text", "label": "Topic"}},
                {"id": "b", "type": "workflow_input", "config": {"key": "topic", "kind": "text", "description": "What to write about."}},
                {"id": "out", "type": "workflow_output", "config": {"key": "result"}},
            ],
            "edges": [
                {"source_node_id": "a", "source_port": "value", "target_node_id": "out", "target_port": "value"},
            ],
        }
    )

    result = validator.validate(workflow)

    assert result.valid is True, result.errors
    assert [port.key for port in result.signature.inputs] == ["topic"]
    port = result.signature.inputs[0]
    assert port.label == "Topic"
    assert port.description == "What to write about."
    assert port.node_ids == ["a", "b"]


def test_conflicting_input_kinds_are_still_rejected(test_settings) -> None:
    services = create_backend_services(test_settings)
    validator = WorkflowValidator(services.registry, services.settings)
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "Conflicting input",
            "nodes": [
                {"id": "a", "type": "workflow_input", "config": {"key": "topic", "kind": "text"}},
                {"id": "b", "type": "workflow_input", "config": {"key": "topic", "kind": "json"}},
                {"id": "out", "type": "workflow_output", "config": {"key": "result"}},
            ],
            "edges": [
                {"source_node_id": "a", "source_port": "value", "target_node_id": "out", "target_port": "value"},
            ],
        }
    )

    result = validator.validate(workflow)

    assert result.valid is False
    assert any("declared as 'text' by a and as 'json' by b" in error for error in result.errors)


def test_shared_input_reaches_every_declaring_node(test_settings) -> None:
    services = create_backend_services(test_settings)
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "Shared input run",
            "nodes": [
                {"id": "a", "type": "workflow_input", "config": {"key": "topic", "kind": "text"}},
                {"id": "b", "type": "workflow_input", "config": {"key": "topic", "kind": "text"}},
                {"id": "join", "type": "merge", "config": {}},
                {"id": "out", "type": "workflow_output", "config": {"key": "joined"}},
            ],
            "edges": [
                {"source_node_id": "a", "source_port": "text", "target_node_id": "join", "target_port": "left"},
                {"source_node_id": "b", "source_port": "text", "target_node_id": "join", "target_port": "right"},
                {"source_node_id": "join", "source_port": "value", "target_node_id": "out", "target_port": "value"},
            ],
        }
    )

    status, output, error = _run(services, workflow, {"topic": "reactors"})

    assert status == "completed", error
    assert output["joined"] == {"left": "reactors", "right": "reactors"}


def test_named_outputs_are_returned_by_key(test_settings) -> None:
    services = create_backend_services(test_settings)
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "Two outputs",
            "nodes": [
                {"id": "value", "type": "workflow_input", "config": {"key": "value", "kind": "text"}},
                {"id": "upper", "type": "plain_text"},
                {"id": "raw_out", "type": "workflow_output", "config": {"key": "raw"}},
                {"id": "text_out", "type": "workflow_output", "config": {"key": "as_text"}},
            ],
            "edges": [
                {"source_node_id": "value", "source_port": "value", "target_node_id": "upper", "target_port": "value"},
                {"source_node_id": "value", "source_port": "value", "target_node_id": "raw_out", "target_port": "value"},
                {"source_node_id": "upper", "source_port": "text", "target_node_id": "text_out", "target_port": "value"},
            ],
        }
    )

    status, output, error = _run(services, workflow, {"value": "hi"})

    assert status == "completed", error
    assert output == {"raw": "hi", "as_text": "hi"}


def test_missing_required_input_fails_the_run(test_settings) -> None:
    services = create_backend_services(test_settings)

    status, _output, error = _run(services, GREETING, {})

    assert status == "failed"
    assert "person" in (error or "")


def test_saved_workflow_is_callable_as_a_node(test_settings) -> None:
    services = create_backend_services(test_settings)
    workflow_id = _save_workflow(services, GREETING)
    node_type = workflow_node_type(workflow_id)

    catalog = {entry.type: entry for entry in services.registry.catalog()}
    assert node_type in catalog
    assert [port.name for port in catalog[node_type].inputs] == ["person"]
    assert [port.name for port in catalog[node_type].outputs] == ["greeting"]

    caller = WorkflowDefinition.model_validate(
        {
            "name": "Caller",
            "nodes": [
                {"id": "who", "type": "workflow_input", "config": {"key": "who", "kind": "json"}},
                {"id": "greet", "type": node_type},
                {"id": "out", "type": "final_output"},
            ],
            "edges": [
                {"source_node_id": "who", "source_port": "value", "target_node_id": "greet", "target_port": "person"},
                {"source_node_id": "greet", "source_port": "greeting", "target_node_id": "out", "target_port": "content"},
            ],
        }
    )

    status, output, error = _run(services, caller, {"who": {"name": "Ada"}})

    assert status == "completed", error
    assert output == "Hello Ada"


def test_nested_workflow_node_runs_are_recorded(test_settings) -> None:
    services = create_backend_services(test_settings)
    workflow_id = _save_workflow(services, GREETING)
    caller = WorkflowDefinition.model_validate(
        {
            "name": "Caller",
            "nodes": [
                {"id": "who", "type": "workflow_input", "config": {"key": "who", "kind": "json"}},
                {"id": "greet", "type": workflow_node_type(workflow_id)},
                {"id": "out", "type": "final_output"},
            ],
            "edges": [
                {"source_node_id": "who", "source_port": "value", "target_node_id": "greet", "target_port": "person"},
                {"source_node_id": "greet", "source_port": "greeting", "target_node_id": "out", "target_port": "content"},
            ],
        }
    )

    async def runner() -> list[str]:
        run = await services.executor.start_run(caller, {"who": {"name": "Ada"}})
        for _ in range(60):
            await asyncio.sleep(0.05)
            current = services.executor.load_run(run.id)
            if current and current.status in {"completed", "failed", "cancelled"}:
                return [node_run.node_path for node_run in services.executor.load_node_runs(run.id)]
        raise AssertionError("Run did not finish in time")

    paths = asyncio.run(runner())

    assert any(path.startswith("greet::") for path in paths)


def test_self_referencing_workflow_is_rejected(test_settings) -> None:
    services = create_backend_services(test_settings)
    validator = WorkflowValidator(services.registry, services.settings)
    workflow_id = _save_workflow(services, GREETING)
    node_type = workflow_node_type(workflow_id)

    recursive = WorkflowDefinition.model_validate(
        {
            "name": "Greeting",
            "nodes": [
                {"id": "person", "type": "workflow_input", "config": {"key": "person", "kind": "json"}},
                {"id": "again", "type": node_type},
                {"id": "out", "type": "workflow_output", "config": {"key": "greeting"}},
            ],
            "edges": [
                {"source_node_id": "person", "source_port": "value", "target_node_id": "again", "target_port": "person"},
                {"source_node_id": "again", "source_port": "greeting", "target_node_id": "out", "target_port": "value"},
            ],
        }
    )
    with services.session_factory() as session:
        session.add(
            WorkflowVersion(workflow_id=workflow_id, version=2, definition_json=recursive.model_dump(mode="json"))
        )
        session.commit()

    caller = WorkflowDefinition.model_validate(
        {
            "name": "Caller",
            "nodes": [
                {"id": "who", "type": "workflow_input", "config": {"key": "who", "kind": "json"}},
                {"id": "greet", "type": node_type},
                {"id": "out", "type": "final_output"},
            ],
            "edges": [
                {"source_node_id": "who", "source_port": "value", "target_node_id": "greet", "target_port": "person"},
                {"source_node_id": "greet", "source_port": "greeting", "target_node_id": "out", "target_port": "content"},
            ],
        }
    )

    result = validator.validate(caller)

    assert result.valid is False
    assert any("cycle" in error.lower() for error in result.errors)


def test_deleted_workflow_reference_reports_a_clear_error(test_settings) -> None:
    services = create_backend_services(test_settings)
    validator = WorkflowValidator(services.registry, services.settings)
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "Dangling",
            "nodes": [{"id": "missing", "type": workflow_node_type("does-not-exist")}],
        }
    )

    result = validator.validate(workflow)

    assert result.valid is False
    assert any("no longer exists" in error for error in result.errors)


def test_starter_templates_expose_their_inputs(test_settings) -> None:
    services = create_backend_services(test_settings)

    with services.session_factory() as session:
        seeded = session.scalar(select(Workflow).where(Workflow.name == "Paper Q&A"))
    assert seeded is not None

    catalog = {entry.type: entry for entry in services.registry.catalog()}
    node_type = workflow_node_type(seeded.id)
    assert node_type in catalog
    assert {port.name for port in catalog[node_type].inputs} == {"document_id", "question"}
    assert {port.name for port in catalog[node_type].outputs} == {"result"}


def test_structured_output_can_feed_a_text_port(test_settings) -> None:
    """A JSON value reaching a text port is serialised instead of failing validation."""
    services = create_backend_services(test_settings)
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "Coerced",
            "nodes": [
                {"id": "raw", "type": "text_input", "config": {"value": '{"topic": "reactors"}'}},
                {"id": "parsed", "type": "json_parse"},
                {"id": "prompt", "type": "prompt_builder", "config": {"require_citations": False}},
                {"id": "out", "type": "workflow_output", "config": {"key": "prompt"}},
            ],
            "edges": [
                {"source_node_id": "raw", "source_port": "text", "target_node_id": "parsed", "target_port": "text"},
                {"source_node_id": "parsed", "source_port": "value", "target_node_id": "prompt", "target_port": "context"},
                {"source_node_id": "prompt", "source_port": "prompt", "target_node_id": "out", "target_port": "value"},
            ],
        }
    )

    validator = WorkflowValidator(services.registry, services.settings)
    result = validator.validate(workflow)
    assert result.valid is True, result.errors
    assert validator.edge_coercions(workflow) == {("parsed", "value", "prompt", "context"): "serialise"}

    status, output, error = _run(services, workflow, {})

    assert status == "completed", error
    assert '"topic": "reactors"' in output["prompt"]


def test_genuinely_incompatible_ports_are_still_rejected(test_settings) -> None:
    services = create_backend_services(test_settings)
    validator = WorkflowValidator(services.registry, services.settings)
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "Impossible",
            "nodes": [
                {"id": "indexer", "type": "index_chunks"},
                {"id": "prompt", "type": "prompt_template", "config": {"template": "x"}},
            ],
            "edges": [
                # number -> json has no meaningful conversion, so it must stay an error.
                {"source_node_id": "indexer", "source_port": "indexed_count", "target_node_id": "prompt", "target_port": "variables"},
            ],
        }
    )

    result = validator.validate(workflow)

    assert result.valid is False
    assert any("Incompatible connection indexer.indexed_count -> prompt.variables" in error for error in result.errors)
