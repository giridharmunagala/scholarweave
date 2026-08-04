from __future__ import annotations

import pytest

from backend.ports import apply_coercion, coercion_for, kinds_compatible


def test_identical_and_wildcard_kinds_pass_through() -> None:
    assert coercion_for("text", "text") is None
    assert coercion_for("any", "json") is None
    assert coercion_for("list", "any") is None
    assert kinds_compatible("text", "text") is True


@pytest.mark.parametrize(
    ("source", "target", "expected"),
    [
        ("json", "text", "serialise"),
        ("list", "text", "serialise"),
        ("number", "text", "serialise"),
        ("text", "json", "parse_json"),
        ("text", "number", "parse_number"),
        ("json", "list", "narrow"),
        ("list", "json", "widen"),
    ],
)
def test_supported_coercions(source: str, target: str, expected: str) -> None:
    assert coercion_for(source, target) == expected
    assert kinds_compatible(source, target) is True


def test_unsupported_pairs_stay_incompatible() -> None:
    assert kinds_compatible("number", "list") is False
    assert kinds_compatible("list", "number") is False


def test_serialise_renders_structures_as_readable_text() -> None:
    assert apply_coercion({"a": 1}, "serialise", "n.p") == '{\n  "a": 1\n}'
    assert apply_coercion(4, "serialise", "n.p") == "4"
    assert apply_coercion("already text", "serialise", "n.p") == "already text"


def test_parse_json_reports_the_port_that_rejected_the_value() -> None:
    assert apply_coercion('{"a": 1}', "parse_json", "n.p") == {"a": 1}
    with pytest.raises(ValueError, match="n.p expects JSON"):
        apply_coercion("not json", "parse_json", "n.p")


def test_parse_number_accepts_ints_and_floats() -> None:
    assert apply_coercion("12", "parse_number", "n.p") == 12
    assert apply_coercion("1.5", "parse_number", "n.p") == 1.5
    with pytest.raises(ValueError, match="expects a number"):
        apply_coercion("twelve", "parse_number", "n.p")


def test_narrow_wraps_a_lone_value_and_leaves_lists_alone() -> None:
    assert apply_coercion([1, 2], "narrow", "n.p") == [1, 2]
    assert apply_coercion({"a": 1}, "narrow", "n.p") == [{"a": 1}]


def test_none_is_never_coerced() -> None:
    assert apply_coercion(None, "parse_json", "n.p") is None
