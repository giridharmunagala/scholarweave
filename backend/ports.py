"""Port-kind compatibility and the safe coercions applied when values cross an edge.

Refusing every mismatched pair forces the author to insert conversion nodes for
conversions the runtime already knows how to do, so a small, explicit table of
lossless-or-obvious coercions is allowed instead. Anything outside the table stays a
validation error, and every coercion is reported so the UI can mark the edge.
"""

from __future__ import annotations

import json
from typing import Any

# (source kind, target kind) -> coercion name. Identity pairs are handled separately.
COERCIONS: dict[tuple[str, str], str] = {
    ("json", "text"): "serialise",
    ("list", "text"): "serialise",
    ("number", "text"): "serialise",
    ("text", "number"): "parse_number",
    ("text", "json"): "parse_json",
    ("list", "json"): "widen",
    ("json", "list"): "narrow",
}

#: Kinds carrying live Agents SDK objects. They only ever connect to their own kind —
#: not even to ``any`` — because letting an agent or tool reach a text port would
#: produce a confusing runtime failure instead of an obvious wiring error.
OPAQUE_KINDS = {"tool", "agent", "guardrail"}


def coercion_for(source_kind: str, target_kind: str) -> str | None:
    """Names the conversion an edge needs, or ``None`` when the value passes through."""
    if source_kind == target_kind or source_kind == "any" or target_kind == "any":
        return None
    return COERCIONS.get((source_kind, target_kind))


def kinds_compatible(source_kind: str, target_kind: str) -> bool:
    if source_kind == target_kind:
        return True
    if source_kind in OPAQUE_KINDS or target_kind in OPAQUE_KINDS:
        return False
    if source_kind == "any" or target_kind == "any":
        return True
    return (source_kind, target_kind) in COERCIONS


def _as_json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        return str(value)


def apply_coercion(value: Any, coercion: str | None, label: str) -> Any:
    """Converts a value for the port it is about to enter, raising a named error on failure."""
    if coercion is None or value is None:
        return value
    if coercion == "serialise":
        return _as_json_text(value)
    if coercion == "widen":
        return value
    if coercion == "parse_number":
        text = str(value).strip()
        try:
            return float(text) if "." in text or "e" in text.lower() else int(text)
        except ValueError as exc:
            raise ValueError(f"{label} expects a number but received {value!r}") from exc
    if coercion == "parse_json":
        text = value if isinstance(value, str) else str(value)
        try:
            return json.loads(text)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} expects JSON but the connected text is not valid JSON") from exc
    if coercion == "narrow":
        if isinstance(value, list):
            return value
        # A single object standing in for a one-item list is the common case here and
        # wrapping it is what the author almost always means.
        return [value]
    return value
