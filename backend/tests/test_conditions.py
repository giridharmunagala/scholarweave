from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.conditions import ConditionEvaluationError, evaluate_condition
from backend.schemas import ConditionPredicate, WorkflowNode


def _predicate(operator: str, value=...):
    payload = {"type": "predicate", "source": "workflow", "path": "value", "operator": operator}
    if value is not ...:
        payload["value"] = value
    return ConditionPredicate.model_validate(payload)


@pytest.mark.parametrize(
    ("operator", "value", "actual", "expected"),
    [
        ("equals", 4, 4, True),
        ("not_equals", 4, 5, True),
        ("exists", ..., "present", True),
        ("not_exists", ..., "present", False),
        ("truthy", ..., True, True),
        ("falsy", ..., False, True),
        ("empty", ..., [], True),
        ("not_empty", ..., "x", True),
        ("greater_than", 3, 4, True),
        ("greater_than_or_equal", 4, 4, True),
        ("less_than", 5, 4, True),
        ("less_than_or_equal", 4, 4, True),
        ("starts_with", "pre", "prefix", True),
        ("ends_with", "fix", "prefix", True),
        ("contains", "ef", "prefix", True),
        ("not_contains", "z", "prefix", True),
        ("in", ["a", "b"], "a", True),
        ("not_in", ["a", "b"], "c", True),
    ],
)
def test_condition_operator_categories(operator, value, actual, expected) -> None:
    assert evaluate_condition(_predicate(operator, value), workflow={"value": actual}, inputs={}) is expected


def test_condition_groups_and_dotted_paths() -> None:
    node = WorkflowNode.model_validate(
        {
            "id": "step",
            "type": "plain_text",
            "run_when": {
                "type": "group",
                "operator": "and",
                "conditions": [
                    {"type": "predicate", "source": "workflow", "path": "items.0.name", "operator": "equals", "value": "a"},
                    {
                        "type": "group",
                        "operator": "or",
                        "conditions": [
                            {"type": "predicate", "source": "inputs", "path": "enabled", "operator": "truthy"},
                            {"type": "predicate", "source": "workflow", "path": "fallback", "operator": "truthy"},
                        ],
                    },
                ],
            },
        }
    )

    assert evaluate_condition(
        node.run_when, workflow={"items": [{"name": "a"}], "fallback": False}, inputs={"enabled": True}
    )


def test_missing_paths_have_explicit_semantics() -> None:
    assert not evaluate_condition(_predicate("exists"), workflow={}, inputs={})
    assert evaluate_condition(_predicate("not_exists"), workflow={}, inputs={})
    assert not evaluate_condition(_predicate("not_equals", "x"), workflow={}, inputs={})
    assert not evaluate_condition(_predicate("empty"), workflow={}, inputs={})


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "predicate", "source": "workflow", "path": "value", "operator": "equals"},
        {"type": "predicate", "source": "workflow", "path": "value", "operator": "in", "value": "not-a-list"},
        {"type": "group", "operator": "and", "conditions": []},
        {"type": "predicate", "source": "arbitrary", "path": "value", "operator": "exists"},
        {"type": "expression", "source": "workflow", "path": "value", "operator": "exists"},
        {"type": "predicate", "source": "workflow", "path": "value", "operator": "equals", "value": object()},
    ],
)
def test_malformed_rules_are_rejected(payload) -> None:
    with pytest.raises(ValidationError):
        WorkflowNode.model_validate({"id": "step", "type": "plain_text", "run_when": payload})


def test_type_mismatches_are_actionable_and_no_boolean_number_coercion() -> None:
    with pytest.raises(ConditionEvaluationError, match="must be a number"):
        evaluate_condition(_predicate("greater_than", 1), workflow={"value": True}, inputs={})
    assert not evaluate_condition(_predicate("equals", 1), workflow={"value": True}, inputs={})
    with pytest.raises(ConditionEvaluationError, match="expected a string"):
        evaluate_condition(_predicate("contains", "x"), workflow={"value": 1}, inputs={})
