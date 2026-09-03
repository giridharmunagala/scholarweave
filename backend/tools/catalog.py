from __future__ import annotations

import copy
import inspect
import json
from typing import Any

from agents import FunctionTool
from agents.tool_context import ToolContext

from backend.agents.blueprint import FunctionToolSpec
from backend.agents.catalog import FunctionToolDefinition, ToolCatalog
from backend.runtime.context import ScholarWeaveContext
from backend.tools.failures import recoverable_tool_invoker, tool_enabled_after_failures


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
        "research.sources.search",
        "search_research_sources",
        "Search one external source: the web, arXiv, or Wikipedia.",
        _object_schema(
            {
                "provider": {
                    "type": "string",
                    "enum": ["web", "arxiv", "wikipedia"],
                },
                "query": {"type": "string", "minLength": 1},
            },
            required=["provider", "query"],
        ),
        True,
    ),
    (
        "research.sources.acquire",
        "acquire_research_source",
        "Download and prepare either a public PDF or an HTML web page.",
        _object_schema(
            {
                "kind": {"type": "string", "enum": ["paper", "web_page"]},
                "url": {"type": "string", "minLength": 1},
                "title": {"type": ["string", "null"], "maxLength": 300},
            },
            required=["kind", "url", "title"],
        ),
        True,
    ),
    (
        "research.library.search",
        "search_research_library",
        "List papers, inspect one paper, or search indexed paper text.",
        _object_schema(
            {
                "query": {"type": ["string", "null"]},
                "document_id": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            required=["query", "document_id", "limit"],
        ),
        True,
    ),
    (
        "research.paper.read",
        "read_research_paper",
        "Inspect, prepare, or read cited pages or chunks from one paper.",
        _object_schema(
            {
                "document_id": {"type": "string", "minLength": 1},
                "action": {
                    "type": "string",
                    "enum": ["inspect", "prepare", "pages", "chunks"],
                },
                "start": {"type": ["integer", "null"], "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            required=["document_id", "action", "start", "limit"],
        ),
        True,
    ),
    (
        "research.web.read",
        "read_research_web_page",
        "Read or search a previously acquired web page, retaining its source URL.",
        _object_schema(
            {
                "source_id": {"type": "string", "minLength": 1},
                "query": {"type": ["string", "null"]},
                "start": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            required=["source_id", "query", "start", "limit"],
        ),
        True,
    ),
    (
        "research.notes.search",
        "search_research_notes",
        "Search durable research notes by content, type, or tags.",
        _object_schema(
            {
                "query": {"type": ["string", "null"]},
                "kinds": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [
                            "note",
                            "paper_summary",
                            "paper_notes",
                            "paper_file",
                            "file",
                        ],
                    },
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 64},
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            required=["query", "kinds", "tags", "limit"],
        ),
        True,
    ),
    (
        "research.notes.read",
        "read_research_note",
        "Read one durable research note.",
        _object_schema(
            {"path": {"type": "string", "minLength": 1}},
            required=["path"],
        ),
        True,
    ),
    (
        "research.notes.save",
        "save_research_note",
        "Create a note, or append to or overwrite a paper note or known note path.",
        _object_schema(
            {
                "target": {
                    "type": "string",
                    "enum": ["new_note", "paper_notes", "path"],
                },
                "mode": {"type": "string", "enum": ["append", "overwrite"]},
                "document_id": {"type": ["string", "null"]},
                "path": {"type": ["string", "null"]},
                "name": {"type": ["string", "null"], "maxLength": 300},
                "content": {"type": "string", "minLength": 1, "maxLength": 200000},
                "tags": {
                    "type": "array",
                    "maxItems": 32,
                    "items": {"type": "string", "minLength": 1, "maxLength": 64},
                },
            },
            required=[
                "target",
                "mode",
                "document_id",
                "path",
                "name",
                "content",
                "tags",
            ],
        ),
        True,
    ),
    (
        "tool.results.read",
        "read_tool_result",
        "Read a bounded slice of a large tool result by its result reference.",
        _object_schema(
            {
                "result_ref": {"type": "string", "minLength": 1},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 256, "maximum": 16_000},
            },
            required=["result_ref", "offset", "limit"],
        ),
        True,
    ),
)


def create_tool_catalog() -> ToolCatalog:
    catalog = ToolCatalog()
    for catalog_id, name, description, schema, strict in APPLICATION_TOOLS:
        documented_schema = _with_parameter_descriptions(schema)
        catalog.register_function_tool(
            FunctionToolDefinition(
                catalog_id=catalog_id,
                label=name.replace("_", " ").title(),
                description=description,
                name=name,
                parameters_schema=documented_schema,
                strict_json_schema=strict,
                factory=_factory(
                    catalog_id,
                    name,
                    description,
                    documented_schema,
                    strict,
                ),
            )
        )
    return catalog


def _with_parameter_descriptions(schema: dict[str, Any]) -> dict[str, Any]:
    documented = copy.deepcopy(schema)

    def visit(value: Any) -> None:
        if not isinstance(value, dict):
            return
        properties = value.get("properties")
        if isinstance(properties, dict):
            for parameter_name, parameter_schema in properties.items():
                if isinstance(parameter_schema, dict):
                    parameter_schema.setdefault(
                        "description",
                        f"{parameter_name.replace('_', ' ').capitalize()}.",
                    )
                    visit(parameter_schema)
        items = value.get("items")
        if isinstance(items, dict):
            visit(items)

    visit(documented)
    return documented


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
            invoker = context.context.tool_runtime.invoke
            if "tool_call_id" in inspect.signature(invoker).parameters:
                return await invoker(
                    catalog_id,
                    arguments,
                    context.context,
                    tool_call_id=context.tool_call_id,
                )
            return await invoker(catalog_id, arguments, context.context)

        tool_name = spec.name or default_name
        return FunctionTool(
            name=tool_name,
            description=spec.description or default_description,
            params_json_schema=parameters_schema,
            on_invoke_tool=recoverable_tool_invoker(
                tool_name,
                invoke,
                catalog_id=catalog_id,
            ),
            strict_json_schema=strict_json_schema,
            needs_approval=spec.needs_approval,
            is_enabled=tool_enabled_after_failures(catalog_id),
        )

    return build
