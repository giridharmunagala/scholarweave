from __future__ import annotations

from backend.app import create_backend_services
from backend.schemas import WorkflowDefinition
from backend.templates import STARTER_WORKFLOWS
from backend.workflows import WorkflowValidator


def test_workflow_validator_accepts_simple_graph(test_settings) -> None:
    services = create_backend_services(test_settings)
    validator = WorkflowValidator(services.registry, services.settings)
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "Simple",
            "nodes": [
                {"id": "message", "type": "text_input", "config": {"input_key": "message"}},
                {"id": "output", "type": "final_output"},
            ],
            "edges": [
                {"source_node_id": "message", "source_port": "text", "target_node_id": "output", "target_port": "content"}
            ],
        }
    )

    result = validator.validate(workflow)

    assert result.valid is True
    assert result.order == ["message", "output"]


def test_workflow_validator_rejects_cycles(test_settings) -> None:
    services = create_backend_services(test_settings)
    validator = WorkflowValidator(services.registry, services.settings)
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "Cyclic",
            "nodes": [
                {"id": "source", "type": "text_input", "config": {"value": "hello"}},
                {"id": "formatter", "type": "plain_text"},
            ],
            "edges": [
                {"source_node_id": "source", "source_port": "text", "target_node_id": "formatter", "target_port": "value"},
                {"source_node_id": "formatter", "source_port": "text", "target_node_id": "formatter", "target_port": "value"},
            ],
        }
    )

    result = validator.validate(workflow)

    assert result.valid is False
    assert any("cycle" in error.lower() for error in result.errors)


def test_workflow_validator_rejects_invalid_node_config(test_settings) -> None:
    services = create_backend_services(test_settings)
    validator = WorkflowValidator(services.registry, services.settings)
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "Invalid config",
            "nodes": [
                {"id": "prompt", "type": "prompt_template", "config": {}},
            ],
        }
    )

    result = validator.validate(workflow)

    assert result.valid is False
    assert any("Invalid config for prompt" in error for error in result.errors)


def test_all_starter_workflows_are_valid(test_settings) -> None:
    services = create_backend_services(test_settings)
    validator = WorkflowValidator(services.registry, services.settings)

    results = {
        workflow.name: validator.validate(workflow)
        for workflow in STARTER_WORKFLOWS
    }

    assert {
        name: result.errors
        for name, result in results.items()
        if not result.valid
    } == {}


def test_validator_checks_known_condition_input_roots(test_settings) -> None:
    services = create_backend_services(test_settings)
    validator = WorkflowValidator(services.registry, services.settings)
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "Invalid condition input",
            "nodes": [
                {
                    "id": "format",
                    "type": "plain_text",
                    "static_inputs": {"value": "x"},
                    "run_when": {
                        "type": "predicate",
                        "source": "inputs",
                        "path": "unknown.nested",
                        "operator": "exists",
                    },
                }
            ],
        }
    )

    result = validator.validate(workflow)

    assert not result.valid
    assert "unknown node input 'unknown'" in result.errors[0]
