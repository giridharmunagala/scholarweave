from __future__ import annotations

import json
from typing import Any

from agents import (
    GuardrailFunctionOutput,
    InputGuardrail,
    OutputGuardrail,
    ToolGuardrailFunctionOutput,
    ToolInputGuardrail,
    ToolOutputGuardrail,
)

from backend.agents.blueprint import GuardrailSpec
from backend.agents.catalog import GuardrailCatalog, GuardrailDefinition
from backend.core.errors import ValidationError
from backend.runtime.serialization import to_jsonable

_DEFAULT_MAX_CHARACTERS = 50_000
_CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "max_characters": {
            "type": "integer",
            "minimum": 1,
            "maximum": 1_000_000,
            "default": _DEFAULT_MAX_CHARACTERS,
        }
    },
    "additionalProperties": False,
}


def create_guardrail_catalog() -> GuardrailCatalog:
    catalog = GuardrailCatalog()
    definitions = (
        GuardrailDefinition(
            catalog_id="content.max_characters",
            kind="input",
            label="Input length",
            description="Trip the SDK input guardrail when run input exceeds the configured size.",
            config_schema=_CONFIG_SCHEMA,
            factory=_input_length_guardrail,
        ),
        GuardrailDefinition(
            catalog_id="content.max_characters",
            kind="output",
            label="Output length",
            description="Trip the SDK output guardrail when final output exceeds the configured size.",
            config_schema=_CONFIG_SCHEMA,
            factory=_output_length_guardrail,
        ),
        GuardrailDefinition(
            catalog_id="content.max_characters",
            kind="tool_input",
            label="Tool input length",
            description="Reject a FunctionTool call whose JSON arguments exceed the configured size.",
            config_schema=_CONFIG_SCHEMA,
            factory=_tool_input_length_guardrail,
        ),
        GuardrailDefinition(
            catalog_id="content.max_characters",
            kind="tool_output",
            label="Tool output length",
            description="Reject FunctionTool output that exceeds the configured size.",
            config_schema=_CONFIG_SCHEMA,
            factory=_tool_output_length_guardrail,
        ),
    )
    for definition in definitions:
        catalog.register(definition)
    return catalog


def _input_length_guardrail(spec: GuardrailSpec) -> InputGuardrail[Any]:
    limit = _max_characters(spec)

    async def check(_context, _agent, input_value):
        actual = len(_text(input_value))
        return GuardrailFunctionOutput(
            output_info={"actual_characters": actual, "max_characters": limit},
            tripwire_triggered=actual > limit,
        )

    return InputGuardrail(check, name=spec.id, run_in_parallel=False)


def _output_length_guardrail(spec: GuardrailSpec) -> OutputGuardrail[Any]:
    limit = _max_characters(spec)

    async def check(_context, _agent, output):
        actual = len(_text(output))
        return GuardrailFunctionOutput(
            output_info={"actual_characters": actual, "max_characters": limit},
            tripwire_triggered=actual > limit,
        )

    return OutputGuardrail(check, name=spec.id)


def _tool_input_length_guardrail(spec: GuardrailSpec) -> ToolInputGuardrail[Any]:
    limit = _max_characters(spec)

    async def check(data):
        actual = len(data.context.tool_arguments)
        info = {"actual_characters": actual, "max_characters": limit}
        if actual > limit:
            return ToolGuardrailFunctionOutput.reject_content(
                "Tool arguments exceeded the configured character limit.",
                info,
            )
        return ToolGuardrailFunctionOutput.allow(info)

    return ToolInputGuardrail(check, name=spec.id)


def _tool_output_length_guardrail(spec: GuardrailSpec) -> ToolOutputGuardrail[Any]:
    limit = _max_characters(spec)

    async def check(data):
        actual = len(_text(data.output))
        info = {"actual_characters": actual, "max_characters": limit}
        if actual > limit:
            return ToolGuardrailFunctionOutput.reject_content(
                "Tool output exceeded the configured character limit.",
                info,
            )
        return ToolGuardrailFunctionOutput.allow(info)

    return ToolOutputGuardrail(check, name=spec.id)


def _max_characters(spec: GuardrailSpec) -> int:
    unknown = set(spec.config) - {"max_characters"}
    if unknown:
        raise ValidationError(
            "Guardrail configuration is invalid.",
            issues=[
                f"Guardrail '{spec.id}' has unknown config field '{field}'."
                for field in sorted(unknown)
            ],
        )
    value = spec.config.get("max_characters", _DEFAULT_MAX_CHARACTERS)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1_000_000:
        raise ValidationError(
            "Guardrail configuration is invalid.",
            issues=[
                f"Guardrail '{spec.id}' max_characters must be an integer from 1 to 1000000."
            ],
        )
    return value


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(to_jsonable(value), ensure_ascii=False, sort_keys=True)
