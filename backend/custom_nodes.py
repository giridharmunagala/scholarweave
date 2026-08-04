"""Persisted, revision-pinned Python transforms exposed as workflow nodes."""

from __future__ import annotations

import ast
from dataclasses import asdict
from typing import Any, Callable

from jsonschema import Draft202012Validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import CustomNodeDefinition, CustomNodeRevision
from backend.nodes import coerce_port_value
from backend.registry import BaseNode, NodeExecutionContext, NodeProvider, PortDefinition
from backend.sandbox import SandboxError, SandboxLimits, run_python
from backend.schemas import (
    CustomNodeConfigField,
    CustomNodePort,
    CustomNodeRevisionResponse,
    CustomNodeSpec,
    NodeDefinitionResponse,
    PortDefinitionResponse,
)

CUSTOM_NODE_PREFIX = "custom:"
_SAFE_PORT_KINDS = {"any", "text", "number", "json", "list"}
_RESERVED_INPUT_NAMES = {"workflow"}


class CustomNodeExecutionError(ValueError):
    def __init__(self, message: str, *, stdout: str = "") -> None:
        super().__init__(message)
        self.stdout = stdout


def custom_node_type(revision_id: str) -> str:
    return f"{CUSTOM_NODE_PREFIX}{revision_id}"


def _ports(values: list[CustomNodePort], role: str) -> list[PortDefinition]:
    return [
        PortDefinition(
            name=value.name,
            kind=value.kind,
            item_kind=value.item_kind,
            description=value.description,
            required=value.required,
        )
        for value in values
    ]


def compile_config_schema(fields: list[CustomNodeConfigField]) -> dict[str, Any]:
    """Compiles the small author-facing field vocabulary into deterministic JSON Schema."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    type_map = {
        "text": "string",
        "number": "number",
        "integer": "integer",
        "boolean": "boolean",
    }
    for field in fields:
        schema: dict[str, Any] = {}
        if field.kind in type_map:
            schema["type"] = type_map[field.kind]
        elif field.kind == "json":
            # JSON values can be an object, array, scalar, boolean, or null.
            schema["type"] = ["object", "array", "string", "number", "integer", "boolean", "null"]
        else:  # select
            schema["type"] = "string"
            schema["enum"] = list(field.options)
        if field.label:
            schema["title"] = field.label
        if field.description:
            schema["description"] = field.description
        if field.default is not None:
            schema["default"] = field.default
        properties[field.name] = schema
        if field.required and field.default is None:
            required.append(field.name)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def validate_spec(spec: CustomNodeSpec) -> dict[str, Any]:
    """Reject malformed ports, fields, schemas, and contracts before persistence."""
    for role, ports in (("input", spec.inputs), ("output", spec.outputs)):
        seen: set[str] = set()
        for port in ports:
            if port.name in seen:
                raise ValueError(f"Duplicate {role} port name '{port.name}'")
            seen.add(port.name)
            if role == "input" and port.name in _RESERVED_INPUT_NAMES:
                raise ValueError(f"'{port.name}' is reserved for workflow inputs")
            if port.kind not in _SAFE_PORT_KINDS:
                raise ValueError(f"Unsupported {role} port kind '{port.kind}'")
            if port.item_kind is not None:
                if port.kind != "list":
                    raise ValueError(f"{role} port '{port.name}' may only set item_kind when kind is 'list'")
                if port.item_kind not in _SAFE_PORT_KINDS:
                    raise ValueError(f"Unsupported item kind '{port.item_kind}' on port '{port.name}'")
    field_names = [field.name for field in spec.config_fields]
    if len(field_names) != len(set(field_names)):
        raise ValueError("Config field names must be unique")
    for field in spec.config_fields:
        if field.kind == "select" and not field.options:
            raise ValueError(f"Select config field '{field.name}' needs at least one option")
        if field.kind == "select" and not all(isinstance(option, str) for option in field.options):
            raise ValueError(f"Select config field '{field.name}' options must be strings")
        if field.kind != "select" and field.options:
            raise ValueError(f"Only select config fields may define options ('{field.name}')")
    schema = compile_config_schema(spec.config_fields)
    defaults = {field.name: field.default for field in spec.config_fields if field.default is not None}
    errors = sorted(Draft202012Validator(schema).iter_errors(defaults), key=lambda error: list(error.path))
    if errors:
        raise ValueError(f"Invalid default for config field '{errors[0].path[0] if errors[0].path else '?'}': {errors[0].message}")
    try:
        tree = ast.parse(spec.code)
    except SyntaxError as exc:
        raise ValueError(f"Python code has invalid syntax: {exc.msg} (line {exc.lineno})") from exc
    function = next(
        (node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "transform"),
        None,
    )
    if function is None:
        raise ValueError("The code must define transform(inputs, config)")
    if isinstance(function, ast.AsyncFunctionDef):
        raise ValueError("transform(inputs, config) must be a regular (not async) function")
    positional = [*function.args.posonlyargs, *function.args.args]
    if len(positional) < 2:
        raise ValueError("transform must accept both inputs and config arguments")
    return schema


def validate_config(config: dict[str, Any], fields: list[CustomNodeConfigField], schema: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise ValueError("Custom-node config must be an object")
    resolved = {
        field.name: field.default
        for field in fields
        if field.default is not None and field.name not in config
    }
    resolved.update(config)
    errors = sorted(Draft202012Validator(schema).iter_errors(resolved), key=lambda error: list(error.path))
    if errors:
        error = errors[0]
        path = ".".join(str(part) for part in error.path) or "config"
        raise ValueError(f"Invalid config at {path}: {error.message}")
    return resolved


def _validate_value(value: Any, port: PortDefinition, label: str) -> Any:
    if port.kind == "any":
        return value
    # Output values deliberately have no implicit edge-style coercion. Custom-code
    # authors get a clear error at their boundary rather than a later graph surprise.
    if port.kind == "text" and not isinstance(value, str):
        raise ValueError(f"{label} must be text, got {type(value).__name__}")
    if port.kind == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
        raise ValueError(f"{label} must be a number, got {type(value).__name__}")
    if port.kind == "json" and not isinstance(value, (dict, list)):
        raise ValueError(f"{label} must be a JSON object or array, got {type(value).__name__}")
    if port.kind == "list":
        if not isinstance(value, list):
            raise ValueError(f"{label} must be a list, got {type(value).__name__}")
        if port.item_kind:
            for index, item in enumerate(value):
                try:
                    _validate_value(item, PortDefinition("item", port.item_kind), f"{label}[{index}]")
                except ValueError as exc:
                    raise ValueError(str(exc)) from exc
    return value


def validate_inputs(inputs: dict[str, Any], ports: list[PortDefinition]) -> dict[str, Any]:
    declared = {port.name: port for port in ports}
    unknown = sorted(set(inputs) - set(declared))
    if unknown:
        raise ValueError(f"Undeclared input keys: {', '.join(unknown)}")
    missing = [port.name for port in ports if port.required and port.name not in inputs]
    if missing:
        raise ValueError(f"Missing required inputs: {', '.join(missing)}")
    return {name: coerce_port_value(value, declared[name].kind, f"Input '{name}'") for name, value in inputs.items()}


async def execute_spec(
    spec: CustomNodeSpec,
    *,
    inputs: dict[str, Any],
    workflow_inputs: dict[str, Any],
    config: dict[str, Any],
    settings: Any,
) -> tuple[dict[str, Any], str]:
    """Runs a validated spec through the same path used by saved runtime nodes."""
    schema = validate_spec(spec)
    ports = _ports(spec.inputs, "input")
    resolved_inputs = validate_inputs(inputs, ports)
    resolved_config = validate_config(config, spec.config_fields, schema)
    if not settings.python_node_enabled:
        raise ValueError("Python nodes are turned off. Enable them under Settings if you trust the code.")
    payload = {**resolved_inputs, "workflow": dict(workflow_inputs)}
    try:
        outcome = await run_python(
            spec.code,
            payload,
            config=resolved_config,
            limits=SandboxLimits(
                timeout_seconds=settings.python_node_timeout_seconds,
                memory_mb=settings.python_node_memory_mb,
            ),
            allowed_imports=settings.python_node_allowed_imports,
        )
    except SandboxError as exc:
        raise CustomNodeExecutionError(str(exc), stdout=exc.stdout) from exc
    if not isinstance(outcome.value, dict):
        raise ValueError("transform must return an object whose keys are declared output ports")
    output_ports = _ports(spec.outputs, "output")
    declared = {port.name: port for port in output_ports}
    unknown = sorted(set(outcome.value) - set(declared))
    if unknown:
        raise ValueError(f"transform returned undeclared output keys: {', '.join(unknown)}")
    missing = [port.name for port in output_ports if port.required and port.name not in outcome.value]
    if missing:
        raise ValueError(f"transform did not return required outputs: {', '.join(missing)}")
    result = {
        name: _validate_value(value, declared[name], f"Output '{name}'")
        for name, value in outcome.value.items()
    }
    return result, outcome.stdout


class CustomNode(BaseNode):
    config_model = None

    def __init__(self, revision: CustomNodeRevision) -> None:
        self.revision_id = revision.id
        self.definition_id = revision.definition_id
        self.revision = revision.revision
        self.type_name = custom_node_type(revision.id)
        self.label = revision.label
        self.description = revision.description
        self.category = revision.category
        self.tags = list(revision.tags_json or [])
        self.inputs = _ports([CustomNodePort.model_validate(item) for item in revision.inputs_json or []], "input")
        self.outputs = _ports([CustomNodePort.model_validate(item) for item in revision.outputs_json or []], "output")
        self.config_fields = [CustomNodeConfigField.model_validate(item) for item in revision.config_fields_json or []]
        self.config_schema = revision.config_schema_json or compile_config_schema(self.config_fields)
        self.code = revision.code

    def validate_config(self, config: dict[str, Any]) -> dict[str, Any]:
        return validate_config(config, self.config_fields, self.config_schema)

    def catalog_entry(self) -> NodeDefinitionResponse:
        return NodeDefinitionResponse(
            type=self.type_name,
            label=self.label,
            description=self.description,
            category=self.category,
            tags=list(self.tags),
            inputs=[PortDefinitionResponse(**asdict(port)) for port in self.inputs],
            outputs=[PortDefinitionResponse(**asdict(port)) for port in self.outputs],
            config_schema=self.config_schema,
        )

    async def execute(self, context: NodeExecutionContext, inputs: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        spec = CustomNodeSpec(
            label=self.label,
            description=self.description,
            category=self.category,
            tags=self.tags,
            inputs=[CustomNodePort(**asdict(port)) for port in self.inputs],
            outputs=[CustomNodePort(**asdict(port)) for port in self.outputs],
            config_fields=self.config_fields,
            code=self.code,
        )
        try:
            result, stdout = await execute_spec(
                spec,
                inputs=inputs,
                workflow_inputs=context.run_inputs,
                config=config,
                settings=context.services.settings,
            )
        except CustomNodeExecutionError as exc:
            if exc.stdout:
                await context.emit("stdout", {"node_path": context.node_path, "text": exc.stdout})
            raise
        if stdout:
            await context.emit("stdout", {"node_path": context.node_path, "text": stdout})
        return result


class CustomNodeProvider(NodeProvider):
    """Resolves every immutable revision while exposing only active latest revisions."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self.session_factory = session_factory

    def resolve(self, type_name: str) -> BaseNode | None:
        if not type_name.startswith(CUSTOM_NODE_PREFIX):
            return None
        revision_id = type_name[len(CUSTOM_NODE_PREFIX) :]
        with self.session_factory() as session:
            revision = session.get(CustomNodeRevision, revision_id)
            return CustomNode(revision) if revision else None

    def describe_missing(self, type_name: str) -> str | None:
        if type_name.startswith(CUSTOM_NODE_PREFIX):
            return f"Referenced custom-node revision {type_name[len(CUSTOM_NODE_PREFIX):]} no longer exists"
        return None

    def catalog(self) -> list[NodeDefinitionResponse]:
        entries: list[NodeDefinitionResponse] = []
        with self.session_factory() as session:
            definitions = session.scalars(
                select(CustomNodeDefinition).where(CustomNodeDefinition.archived.is_(False)).order_by(CustomNodeDefinition.name)
            )
            for definition in definitions:
                revision = session.scalar(
                    select(CustomNodeRevision)
                    .where(CustomNodeRevision.definition_id == definition.id)
                    .order_by(CustomNodeRevision.revision.desc())
                    .limit(1)
                )
                if revision:
                    entries.append(CustomNode(revision).catalog_entry())
        return entries


def revision_response(revision: CustomNodeRevision) -> CustomNodeRevisionResponse:
    return CustomNodeRevisionResponse(
        id=revision.id,
        definition_id=revision.definition_id,
        revision=revision.revision,
        node_type=custom_node_type(revision.id),
        label=revision.label,
        description=revision.description,
        category=revision.category,
        tags=list(revision.tags_json or []),
        inputs=[CustomNodePort.model_validate(item) for item in revision.inputs_json or []],
        outputs=[CustomNodePort.model_validate(item) for item in revision.outputs_json or []],
        config_fields=[CustomNodeConfigField.model_validate(item) for item in revision.config_fields_json or []],
        config_schema=revision.config_schema_json or {},
        code=revision.code,
        created_at=revision.created_at,
    )
