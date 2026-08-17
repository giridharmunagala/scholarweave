from __future__ import annotations

import json
from typing import Any

from agents import AgentOutputSchemaBase, ModelBehaviorError
from jsonschema import Draft202012Validator, ValidationError as JsonSchemaValidationError


def with_json_schema_output_instructions(
    instructions: str,
    schema_name: str,
    schema: dict[str, Any],
) -> str:
    serialized_schema = json.dumps(
        schema,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        f"{instructions.rstrip()}\n\n"
        "Structured output requirement:\n"
        f"Return only one JSON value matching the {schema_name} schema below. "
        "Do not return Markdown fences, headings, commentary, or an answer to the user's "
        "request outside that JSON value. This requirement applies even when the provider "
        "does not enforce its response-format setting.\n"
        f"{serialized_schema}"
    )


class JsonSchemaOutput(AgentOutputSchemaBase):
    """SDK output schema backed by a persisted JSON Schema document."""

    def __init__(self, schema_name: str, schema: dict[str, Any], *, strict: bool) -> None:
        Draft202012Validator.check_schema(schema)
        self._schema_name = schema_name
        self._schema = schema
        self._strict = strict
        self._validator = Draft202012Validator(schema)

    def is_plain_text(self) -> bool:
        return False

    def is_strict_json_schema(self) -> bool:
        return self._strict

    def json_schema(self) -> dict[str, Any]:
        return self._schema

    def name(self) -> str:
        return self._schema_name

    def validate_json(self, json_str: str) -> Any:
        payload = _strip_json_fence(json_str)
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            if not payload:
                detail = "the model returned an empty response"
            else:
                preview = payload[:160].replace("\n", "\\n")
                detail = f"the response started with {preview!r}"
            raise ModelBehaviorError(
                f"Model returned invalid JSON for structured output "
                f"'{self._schema_name}': {exc.msg}; {detail}."
            ) from exc
        try:
            self._validator.validate(value)
        except JsonSchemaValidationError as exc:
            raise ModelBehaviorError(
                f"Structured output '{self._schema_name}' failed JSON Schema validation: "
                f"{exc.message}"
            ) from exc
        return value


def _strip_json_fence(value: str) -> str:
    stripped = value.strip()
    if not stripped.startswith("```") or not stripped.endswith("```"):
        return stripped
    first_newline = stripped.find("\n")
    if first_newline == -1:
        return stripped
    return stripped[first_newline + 1 : -3].strip()
