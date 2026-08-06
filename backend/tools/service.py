from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from typing import Any

from agents import FunctionTool
from agents.tool_context import ToolContext
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from backend.agents.blueprint import FunctionToolSpec
from backend.core.config import Settings
from backend.core.errors import ValidationError
from backend.runtime.context import ScholarWeaveContext
from backend.tools.sandbox import SandboxError, SandboxLimits, run_python
from backend.tools.models import FunctionToolRecord, FunctionToolRevision
from backend.tools.repository import FunctionToolRepository

CUSTOM_TOOL_PREFIX = "custom:"


@dataclass(frozen=True, slots=True)
class FunctionToolDocument:
    record: FunctionToolRecord
    latest_revision: FunctionToolRevision


class FunctionToolService:
    def __init__(self, repository: FunctionToolRepository, settings: Settings) -> None:
        self._repository = repository
        self._settings = settings

    def list(self) -> list[FunctionToolDocument]:
        return [self._document(record) for record in self._repository.list()]

    def get(self, definition_id: str) -> FunctionToolDocument:
        return self._document(self._repository.get(definition_id))

    def create(
        self,
        *,
        name: str,
        description: str,
        parameters_schema: dict[str, Any],
        output_schema: dict[str, Any] | None,
        code: str,
        requires_approval: bool,
    ) -> FunctionToolDocument:
        self.validate_definition(parameters_schema, output_schema, code)
        return self._document(
            self._repository.create(
                name=name,
                description=description,
                parameters_schema=parameters_schema,
                output_schema=output_schema,
                code=code,
                requires_approval=requires_approval,
            )
        )

    def update(
        self,
        definition_id: str,
        *,
        name: str,
        description: str,
        parameters_schema: dict[str, Any],
        output_schema: dict[str, Any] | None,
        code: str,
        requires_approval: bool,
    ) -> FunctionToolDocument:
        self.validate_definition(parameters_schema, output_schema, code)
        return self._document(
            self._repository.add_revision(
                definition_id,
                name=name,
                description=description,
                parameters_schema=parameters_schema,
                output_schema=output_schema,
                code=code,
                requires_approval=requires_approval,
            )
        )

    def archive(self, definition_id: str) -> FunctionToolDocument:
        return self._document(self._repository.archive(definition_id))

    async def test(
        self,
        *,
        parameters_schema: dict[str, Any],
        output_schema: dict[str, Any] | None,
        code: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        self.validate_definition(parameters_schema, output_schema, code)
        self._validate_value(parameters_schema, arguments, "arguments")
        result = await self._execute(code, arguments)
        if output_schema is not None:
            self._validate_value(output_schema, result["output"], "output")
        return result

    def dynamic_factory(self, spec: FunctionToolSpec) -> FunctionTool | None:
        if not spec.catalog_id.startswith(CUSTOM_TOOL_PREFIX):
            return None
        revision_id = spec.catalog_id[len(CUSTOM_TOOL_PREFIX) :]
        revision = self._repository.get_revision(revision_id)
        definition = revision.definition

        async def invoke(
            _context: ToolContext[ScholarWeaveContext],
            raw_arguments: str,
        ) -> Any:
            arguments = json.loads(raw_arguments)
            result = await self._execute(revision.code, arguments)
            output = result["output"]
            if revision.output_schema_json is not None:
                self._validate_value(revision.output_schema_json, output, "output")
            return output

        return FunctionTool(
            name=spec.name or definition.name,
            description=spec.description or revision.description,
            params_json_schema=revision.parameters_schema_json,
            on_invoke_tool=invoke,
            strict_json_schema=True,
            needs_approval=spec.needs_approval or revision.requires_approval,
            output_json_schema=revision.output_schema_json,
        )

    @staticmethod
    def validate_definition(
        parameters_schema: dict[str, Any],
        output_schema: dict[str, Any] | None,
        code: str,
    ) -> None:
        try:
            Draft202012Validator.check_schema(parameters_schema)
            if output_schema is not None:
                Draft202012Validator.check_schema(output_schema)
        except SchemaError as exc:
            raise ValidationError(f"Invalid JSON Schema: {exc}") from exc
        if parameters_schema.get("type") != "object":
            raise ValidationError("Function-tool parameters schema must describe an object.")
        if parameters_schema.get("additionalProperties") is not False:
            raise ValidationError(
                "Function-tool parameters schema must set additionalProperties to false."
            )
        if output_schema is not None and output_schema.get("type") != "object":
            raise ValidationError(
                "Function-tool output schema must describe an object, as required by the SDK."
            )
        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            raise ValidationError(
                f"Python code has invalid syntax: {exc.msg} (line {exc.lineno})."
            ) from exc
        entrypoint = next(
            (
                node
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "invoke"
            ),
            None,
        )
        if entrypoint is None:
            raise ValidationError("Function-tool code must define invoke(arguments).")
        if isinstance(entrypoint, ast.AsyncFunctionDef):
            raise ValidationError("invoke(arguments) must be a regular function.")
        positional = [*entrypoint.args.posonlyargs, *entrypoint.args.args]
        if len(positional) != 1:
            raise ValidationError("invoke must accept exactly one arguments parameter.")

    async def _execute(self, code: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self._settings.python_tool_enabled:
            raise ValidationError("Custom Python function tools are disabled.")
        try:
            result = await run_python(
                code,
                arguments,
                limits=SandboxLimits(
                    timeout_seconds=self._settings.python_tool_timeout_seconds,
                    memory_mb=self._settings.python_tool_memory_mb,
                ),
                allowed_imports=self._settings.python_tool_allowed_imports,
                entrypoint="invoke",
            )
        except SandboxError as exc:
            raise ValidationError(f"Function tool failed: {exc}") from exc
        return {"output": result.value, "stdout": result.stdout}

    @staticmethod
    def _validate_value(schema: dict[str, Any], value: Any, label: str) -> None:
        errors = sorted(
            Draft202012Validator(schema).iter_errors(value),
            key=lambda error: list(error.path),
        )
        if errors:
            path = ".".join(str(part) for part in errors[0].path)
            location = f"{label}.{path}" if path else label
            raise ValidationError(f"Invalid {location}: {errors[0].message}")

    @staticmethod
    def _document(record: FunctionToolRecord) -> FunctionToolDocument:
        if not record.revisions:
            raise RuntimeError(f"Function tool '{record.id}' has no revisions.")
        return FunctionToolDocument(record=record, latest_revision=record.revisions[-1])
