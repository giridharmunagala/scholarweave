from __future__ import annotations

import json
from typing import Any

from agents import AgentOutputSchemaBase
from jsonschema import Draft202012Validator, ValidationError as JsonSchemaValidationError


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
        value = json.loads(json_str)
        try:
            self._validator.validate(value)
        except JsonSchemaValidationError as exc:
            raise ValueError(f"Structured output failed JSON Schema validation: {exc.message}") from exc
        return value
