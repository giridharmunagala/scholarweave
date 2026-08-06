from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from agents import (
    FunctionTool,
    InputGuardrail,
    OutputGuardrail,
    ToolInputGuardrail,
    ToolOutputGuardrail,
)

from backend.agents.blueprint import FunctionToolSpec, GuardrailSpec
from backend.core.errors import ValidationError

FunctionToolFactory = Callable[[FunctionToolSpec], FunctionTool]
DynamicFunctionToolFactory = Callable[[FunctionToolSpec], FunctionTool | None]
InputGuardrailFactory = Callable[[GuardrailSpec], InputGuardrail[Any]]
OutputGuardrailFactory = Callable[[GuardrailSpec], OutputGuardrail[Any]]
ToolInputGuardrailFactory = Callable[[GuardrailSpec], ToolInputGuardrail[Any]]
ToolOutputGuardrailFactory = Callable[[GuardrailSpec], ToolOutputGuardrail[Any]]
GuardrailKind = Literal["input", "output", "tool_input", "tool_output"]


@dataclass(frozen=True, slots=True)
class FunctionToolDefinition:
    catalog_id: str
    label: str
    description: str
    factory: FunctionToolFactory
    name: str | None = None
    parameters_schema: dict[str, Any] | None = None
    strict_json_schema: bool = True


@dataclass(frozen=True, slots=True)
class GuardrailDefinition:
    catalog_id: str
    kind: GuardrailKind
    label: str
    description: str
    config_schema: dict[str, Any]
    factory: (
        InputGuardrailFactory
        | OutputGuardrailFactory
        | ToolInputGuardrailFactory
        | ToolOutputGuardrailFactory
    )


class ToolCatalog:
    def __init__(self) -> None:
        self._function_tools: dict[str, FunctionToolDefinition] = {}
        self._dynamic_factories: list[DynamicFunctionToolFactory] = []

    def register_function_tool(self, definition: FunctionToolDefinition) -> None:
        if definition.catalog_id in self._function_tools:
            raise ValueError(f"Tool catalog ID '{definition.catalog_id}' is already registered.")
        self._function_tools[definition.catalog_id] = definition

    def build_function_tool(self, spec: FunctionToolSpec) -> FunctionTool:
        definition = self._function_tools.get(spec.catalog_id)
        if definition is not None:
            return definition.factory(spec)
        for factory in self._dynamic_factories:
            tool = factory(spec)
            if tool is not None:
                return tool
        raise ValidationError(
            "Agent blueprint references an unknown function tool.",
            issues=[f"Unknown function tool catalog ID '{spec.catalog_id}'."],
        )

    def register_dynamic_factory(self, factory: DynamicFunctionToolFactory) -> None:
        self._dynamic_factories.append(factory)

    def definitions(self) -> tuple[FunctionToolDefinition, ...]:
        return tuple(sorted(self._function_tools.values(), key=lambda item: item.catalog_id))


class GuardrailCatalog:
    def __init__(self) -> None:
        self._definitions: dict[tuple[GuardrailKind, str], GuardrailDefinition] = {}

    def register(self, definition: GuardrailDefinition) -> None:
        key = (definition.kind, definition.catalog_id)
        if key in self._definitions:
            raise ValueError(
                f"{definition.kind.replace('_', ' ').title()} guardrail "
                f"'{definition.catalog_id}' is already registered."
            )
        self._definitions[key] = definition

    def definitions(self) -> tuple[GuardrailDefinition, ...]:
        return tuple(
            sorted(
                self._definitions.values(),
                key=lambda item: (item.kind, item.catalog_id),
            )
        )

    def build_input(self, spec: GuardrailSpec) -> InputGuardrail[Any]:
        return self._build(spec, "input")

    def build_output(self, spec: GuardrailSpec) -> OutputGuardrail[Any]:
        return self._build(spec, "output")

    def build_tool_input(self, spec: GuardrailSpec) -> ToolInputGuardrail[Any]:
        return self._build(spec, "tool_input")

    def build_tool_output(self, spec: GuardrailSpec) -> ToolOutputGuardrail[Any]:
        return self._build(spec, "tool_output")

    def _build(self, spec: GuardrailSpec, kind: GuardrailKind):
        definition = self._definitions.get((kind, spec.catalog_id))
        if definition is None:
            raise ValidationError(
                f"Agent blueprint references an unknown {kind.replace('_', ' ')} guardrail.",
                issues=[
                    f"Unknown {kind.replace('_', ' ')} guardrail catalog ID "
                    f"'{spec.catalog_id}'."
                ],
            )
        return definition.factory(spec)
