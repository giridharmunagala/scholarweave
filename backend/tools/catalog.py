from __future__ import annotations

import json
from typing import Any

from agents import FunctionTool
from agents.tool_context import ToolContext

from backend.agents.blueprint import FunctionToolSpec
from backend.agents.catalog import FunctionToolDefinition, ToolCatalog
from backend.runtime.context import ScholarWeaveContext


def _object_schema(
    properties: dict[str, Any],
    *,
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


APPLICATION_TOOLS: tuple[tuple[str, str, str, dict[str, Any], bool], ...] = (
    (
        "builder.todos.create",
        "create_builder_todo_plan",
        "Create the ordered TODO plan for one actionable builder request.",
        _object_schema(
            {
                "tasks": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 12,
                    "items": _object_schema(
                        {
                            "id": {"type": "string", "minLength": 1},
                            "title": {"type": "string", "minLength": 1},
                        },
                        required=["id", "title"],
                    ),
                }
            },
            required=["tasks"],
        ),
        True,
    ),
    (
        "builder.todos.update",
        "update_builder_todo",
        "Complete or block the current builder TODO and advance the plan.",
        _object_schema(
            {
                "id": {"type": "string", "minLength": 1},
                "status": {"type": "string", "enum": ["completed", "blocked"]},
                "note": {"type": ["string", "null"]},
            },
            required=["id", "status", "note"],
        ),
        True,
    ),
    (
        "builder.finish",
        "finish_builder_run",
        "Finish a builder request only after its TODOs and save are complete.",
        _object_schema(
            {
                "outcome": {
                    "type": "string",
                    "enum": ["saved", "informational"],
                },
                "summary": {"type": "string", "minLength": 1},
            },
            required=["outcome", "summary"],
        ),
        True,
    ),
    (
        "documents.list",
        "list_documents",
        "List papers",
        _object_schema({}),
        True,
    ),
    (
        "documents.read_chunks",
        "read_document_chunks",
        "Read ordered text chunks from a paper.",
        _object_schema(
            {
                "document_id": {"type": "string"},
                "start": {"type": ["integer", "null"], "minimum": 0},
                "limit": {"type": ["integer", "null"], "minimum": 1, "maximum": 100},
            },
            required=["document_id", "start", "limit"],
        ),
        True,
    ),
    (
        "retrieval.keyword_search",
        "search_papers",
        "Search indexed paper text using keywords.",
        _object_schema(
            {
                "query": {"type": "string", "minLength": 1},
                "document_id": {"type": ["string", "null"]},
                "top_k": {"type": ["integer", "null"], "minimum": 1, "maximum": 20},
            },
            required=["query", "document_id", "top_k"],
        ),
        True,
    ),
    (
        "workspace.list",
        "list_workspace_files",
        "List safe text, Markdown, and JSON files in the workspace.",
        _object_schema({}),
        True,
    ),
    (
        "workspace.read",
        "read_workspace_file",
        "Read one safe workspace file.",
        _object_schema(
            {"path": {"type": "string", "minLength": 1}},
            required=["path"],
        ),
        True,
    ),
    (
        "workspace.write",
        "write_workspace_file",
        "Write one safe text, Markdown, or JSON workspace file.",
        _object_schema(
            {
                "path": {"type": "string", "minLength": 1},
                "content": {
                    "type": ["object", "array", "string", "number", "boolean", "null"],
                    "items": {
                        "type": ["object", "string", "number", "boolean", "null"]
                    },
                },
            },
            required=["path", "content"],
        ),
        True,
    ),
    (
        "artifacts.write",
        "write_artifact",
        "Write a generated text, Markdown, or JSON artifact.",
        _object_schema(
            {
                "path": {"type": "string", "minLength": 1},
                "content": {
                    "type": ["object", "array", "string", "number", "boolean", "null"],
                    "items": {
                        "type": ["object", "string", "number", "boolean", "null"]
                    },
                },
                "media_type": {
                    "type": "string",
                    "enum": ["text/plain", "text/markdown", "application/json"],
                },
            },
            required=["path", "content", "media_type"],
        ),
        True,
    ),
    (
        "sdk.catalog",
        "list_sdk_primitives",
        "List the OpenAI Agents SDK primitives and ScholarWeave function tools available to the builder.",
        _object_schema({}),
        True,
    ),
    (
        "agents.list",
        "list_saved_agents",
        "List saved SDK agent blueprints.",
        _object_schema({}),
        True,
    ),
    (
        "agents.get",
        "get_saved_agent",
        "Get one saved SDK agent blueprint.",
        _object_schema(
            {"agent_id": {"type": "string", "minLength": 1}},
            required=["agent_id"],
        ),
        True,
    ),
    (
        "agents.validate",
        "validate_agent_blueprint",
        (
            "Validate a complete nested SDK agent blueprint before saving it. Required top-level "
            "fields include name, entry_agent_id, and agents; tool entries use kind, not type."
        ),
        _object_schema(
            {"blueprint": {"type": "object"}},
            required=["blueprint"],
        ),
        False,
    ),
    (
        "agents.save",
        "save_agent_blueprint",
        (
            "Create or revise a complete, validated nested SDK agent blueprint. Keep instructions, "
            "model, model_settings, and output inside each agents entry."
        ),
        _object_schema(
            {
                "agent_id": {"type": ["string", "null"]},
                "blueprint": {"type": "object"},
                "presentation": {"type": "object"},
            },
            required=["agent_id", "blueprint", "presentation"],
        ),
        False,
    ),
    (
        "function_tools.save",
        "save_custom_function_tool",
        "Create or revise a sandboxed SDK FunctionTool.",
        _object_schema(
            {
                "definition_id": {"type": ["string", "null"]},
                "name": {"type": "string", "minLength": 1},
                "description": {"type": "string"},
                "parameters_schema": {"type": "object"},
                "output_schema": {"type": ["object", "null"]},
                "code": {"type": "string", "minLength": 1},
                "requires_approval": {"type": "boolean"},
            },
            required=[
                "definition_id",
                "name",
                "description",
                "parameters_schema",
                "output_schema",
                "code",
                "requires_approval",
            ],
        ),
        False,
    ),
)


def create_tool_catalog() -> ToolCatalog:
    catalog = ToolCatalog()
    for catalog_id, name, description, schema, strict in APPLICATION_TOOLS:
        catalog.register_function_tool(
            FunctionToolDefinition(
                catalog_id=catalog_id,
                label=name.replace("_", " ").title(),
                description=description,
                name=name,
                parameters_schema=schema,
                strict_json_schema=strict,
                factory=_factory(catalog_id, name, description, schema, strict),
            )
        )
    return catalog


def _factory(
    catalog_id: str,
    default_name: str,
    default_description: str,
    parameters_schema: dict[str, Any],
    strict_json_schema: bool,
):
    def build(spec: FunctionToolSpec) -> FunctionTool:
        async def invoke(
            context: ToolContext[ScholarWeaveContext],
            raw_arguments: str,
        ) -> Any:
            arguments = json.loads(raw_arguments)
            return await context.context.tool_runtime.invoke(
                catalog_id,
                arguments,
                context.context,
            )

        return FunctionTool(
            name=spec.name or default_name,
            description=spec.description or default_description,
            params_json_schema=parameters_schema,
            on_invoke_tool=invoke,
            strict_json_schema=strict_json_schema,
            needs_approval=spec.needs_approval,
        )

    return build
