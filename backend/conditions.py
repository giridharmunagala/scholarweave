"""Safe evaluation for the workflow builder's declarative condition rules.

Missing paths only satisfy ``not_exists``. Other predicates against a missing
path are false. Equality is type-strict (``True`` is not equal to ``1``), and
operators that need a particular type raise ``ConditionEvaluationError`` rather
than silently converting data.
"""

from __future__ import annotations

from typing import Any, Iterator

from backend.schemas import ConditionGroup, ConditionPredicate, ConditionRule


class ConditionEvaluationError(ValueError):
    """A rule is valid JSON but cannot be applied to the runtime value."""


_MISSING = object()


def condition_predicates(rule: ConditionRule) -> Iterator[ConditionPredicate]:
    """Yields each leaf predicate in a condition tree."""
    if isinstance(rule, ConditionPredicate):
        yield rule
        return
    for child in rule.conditions:
        yield from condition_predicates(child)


def evaluate_condition(
    rule: ConditionRule,
    *,
    workflow: dict[str, Any],
    inputs: dict[str, Any],
) -> bool:
    """Evaluates a condition without executing code or coercing values."""
    if isinstance(rule, ConditionGroup):
        results = (evaluate_condition(child, workflow=workflow, inputs=inputs) for child in rule.conditions)
        return all(results) if rule.operator == "and" else any(results)
    value = _resolve_path(workflow if rule.source == "workflow" else inputs, rule.path)
    return _evaluate_predicate(rule, value)


def _resolve_path(root: dict[str, Any], path: str) -> Any:
    value: Any = root
    for segment in path.split("."):
        if isinstance(value, dict):
            if segment not in value:
                return _MISSING
            value = value[segment]
        elif isinstance(value, (list, tuple)):
            if not segment.isdecimal():
                return _MISSING
            index = int(segment)
            if index >= len(value):
                return _MISSING
            value = value[index]
        else:
            return _MISSING
    return value


def _evaluate_predicate(predicate: ConditionPredicate, actual: Any) -> bool:
    operator = predicate.operator
    if actual is _MISSING:
        return operator == "not_exists"
    if operator == "exists":
        return True
    if operator == "not_exists":
        return False
    if operator in {"equals", "not_equals"}:
        matches = type(actual) is type(predicate.value) and actual == predicate.value
        return matches if operator == "equals" else not matches
    if operator in {"truthy", "falsy"}:
        _require_type(actual, bool, predicate)
        return actual if operator == "truthy" else not actual
    if operator in {"empty", "not_empty"}:
        _require_type(actual, (str, list, tuple, dict), predicate)
        is_empty = len(actual) == 0
        return is_empty if operator == "empty" else not is_empty
    if operator in {"greater_than", "greater_than_or_equal", "less_than", "less_than_or_equal"}:
        _require_number(actual, predicate)
        _require_number(predicate.value, predicate, operand=True)
        if operator == "greater_than":
            return actual > predicate.value
        if operator == "greater_than_or_equal":
            return actual >= predicate.value
        if operator == "less_than":
            return actual < predicate.value
        return actual <= predicate.value
    if operator in {"starts_with", "ends_with"}:
        _require_type(actual, str, predicate)
        _require_type(predicate.value, str, predicate, operand=True)
        return actual.startswith(predicate.value) if operator == "starts_with" else actual.endswith(predicate.value)
    if operator in {"contains", "not_contains"}:
        contains = _contains(actual, predicate.value, predicate)
        return contains if operator == "contains" else not contains
    if operator in {"in", "not_in"}:
        contained = any(type(actual) is type(item) and actual == item for item in predicate.value)
        return contained if operator == "in" else not contained
    raise ConditionEvaluationError(f"Unsupported condition operator '{operator}'")


def _contains(actual: Any, expected: Any, predicate: ConditionPredicate) -> bool:
    if isinstance(actual, str):
        _require_type(expected, str, predicate, operand=True)
        return expected in actual
    if isinstance(actual, (list, tuple)):
        return any(type(expected) is type(item) and expected == item for item in actual)
    if isinstance(actual, dict):
        _require_type(expected, str, predicate, operand=True)
        return expected in actual
    raise ConditionEvaluationError(
        f"Condition {predicate.source}.{predicate.path} uses '{predicate.operator}' on "
        f"{type(actual).__name__}; expected a string, list, tuple, or object"
    )


def _require_number(value: Any, predicate: ConditionPredicate, *, operand: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _type_error(value, "a number", predicate, operand)


def _require_type(
    value: Any,
    expected: type[Any] | tuple[type[Any], ...],
    predicate: ConditionPredicate,
    *,
    operand: bool = False,
) -> None:
    if not isinstance(value, expected):
        labels = "/".join(item.__name__ for item in expected) if isinstance(expected, tuple) else expected.__name__
        _type_error(value, labels, predicate, operand)


def _type_error(value: Any, expected: str, predicate: ConditionPredicate, operand: bool) -> None:
    side = "rule value" if operand else f"{predicate.source}.{predicate.path}"
    raise ConditionEvaluationError(
        f"Condition {side} for '{predicate.operator}' must be {expected}; received {type(value).__name__}"
    )
