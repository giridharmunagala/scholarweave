from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from backend.agents.blueprint import FunctionToolSpec
from backend.agents.harness import FunctionTool
from backend.core.errors import ValidationError

FunctionToolFactory = Callable[[FunctionToolSpec], FunctionTool]
DynamicFunctionToolFactory = Callable[[FunctionToolSpec], FunctionTool | None]


@dataclass(frozen=True, slots=True)
class FunctionToolDefinition:
    catalog_id: str
    label: str
    description: str
    factory: FunctionToolFactory
    name: str | None = None
    parameters_schema: dict[str, Any] | None = None
    strict_json_schema: bool = True


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
