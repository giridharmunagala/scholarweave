from __future__ import annotations

import copy
import inspect
import json
from typing import Any

from backend.agents.blueprint import FunctionToolSpec
from backend.agents.catalog import FunctionToolDefinition, ToolCatalog
from backend.agents.harness import FunctionTool, ToolInvocation
from backend.prompting.registry import PromptRegistry
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


ApplicationToolDefinition = tuple[str, str, str, dict[str, Any], bool, str]


APPLICATION_TOOLS: tuple[ApplicationToolDefinition, ...] = (
    (
        "conversation.title.set",
        "set_conversation_title",
        "Set a concise title for this conversation during its first turn.",
        _object_schema(
            {
                "title": {"type": "string", "minLength": 1, "maxLength": 120},
            },
            required=["title"],
        ),
        True,
        "_set_conversation_title",
    ),
    (
        "work.plan.create",
        "create_work_plan",
        "Create the tracked work items for an autonomous run.",
        _object_schema(
            {
                "items": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 10,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "minLength": 1, "maxLength": 80},
                            "title": {"type": "string", "minLength": 1, "maxLength": 300},
                        },
                        "required": ["id", "title"],
                        "additionalProperties": False,
                    },
                }
            },
            required=["items"],
        ),
        True,
        "_create_work_plan",
    ),
    (
        "work.plan.update",
        "update_work_item",
        "Update one tracked work item as work progresses.",
        _object_schema(
            {
                "id": {"type": "string", "minLength": 1, "maxLength": 80},
                "status": {
                    "type": "string",
                    "enum": ["in_progress", "completed", "blocked"],
                },
                "summary": {"type": "string", "maxLength": 4000},
            },
            required=["id", "status", "summary"],
        ),
        True,
        "_update_work_item",
    ),
    (
        "work.plan.read",
        "read_work_plan",
        "Read the tracked work plan and its pending items.",
        _object_schema({}),
        True,
        "_read_work_plan",
    ),
    (
        "research.sources.search",
        "search_research_sources",
        "Search DuckDuckGo, arXiv, or Wikipedia.",
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
        "_search_research_sources",
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
        "_acquire_research_source",
    ),
    (
        "research.library.search",
        "search_research_library",
        "List papers, inspect one paper, or return the three best metadata word matches.",
        _object_schema(
            {
                "query": {"type": ["string", "null"]},
                "document_id": {"type": ["string", "null"]},
                "ignore_document_ids": {
                    "type": ["array", "null"],
                    "items": {"type": "string", "minLength": 1},
                    "maxItems": 100,
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 3},
            },
            required=["query", "document_id", "ignore_document_ids", "limit"],
        ),
        True,
        "_search_research_library",
    ),
    (
        "research.library.organize",
        "organize_research_library",
        "List paper folders, create a folder, or move a paper.",
        _object_schema(
            {
                "action": {
                    "type": "string",
                    "enum": ["list", "create_folder", "move_paper"],
                },
                "folder_name": {"type": ["string", "null"], "maxLength": 100},
                "document_id": {"type": ["string", "null"]},
                "folder_id": {"type": ["string", "null"]},
            },
            required=["action", "folder_name", "document_id", "folder_id"],
        ),
        True,
        "_organize_research_library",
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
        "_read_research_paper",
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
        "_read_research_web_page",
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
        "_search_research_notes",
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
        "_read_research_note",
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
        "_save_research_note",
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
        "_read_tool_result",
    ),
    (
        "research.summary.read",
        "read_paper_summary_batch",
        "Inspect or prepare one paper, or read the next summary batch with one-page overlap.",
        _object_schema(
            {
                "document_id": {"type": "string", "minLength": 1},
                "action": {
                    "type": "string",
                    "enum": ["inspect", "prepare", "pages", "chunks"],
                },
                "start": {"type": ["integer", "null"], "minimum": 0},
            },
            required=["document_id", "action", "start"],
        ),
        True,
        "_read_paper_summary_batch",
    ),
    (
        "research.summary.checkpoint",
        "paper_summary_checkpoint",
        "Append evidence to or read the temporary checkpoint for this paper-summary run.",
        _object_schema(
            {
                "document_id": {"type": "string", "minLength": 1},
                "action": {"type": "string", "enum": ["append", "read"]},
                "content": {"type": ["string", "null"], "maxLength": 50000},
                "offset": {"type": ["integer", "null"], "minimum": 0},
                "limit": {
                    "type": ["integer", "null"],
                    "minimum": 256,
                    "maximum": 8000,
                },
            },
            required=["document_id", "action", "content", "offset", "limit"],
        ),
        True,
        "_paper_summary_checkpoint",
    ),
    (
        "research.summary.save",
        "save_paper_summary_version",
        "Save one reviewed paper summary as an immutable version and update the canonical summary.",
        _object_schema(
            {
                "document_id": {"type": "string", "minLength": 1},
                "content": {"type": "string", "minLength": 200, "maxLength": 200000},
                "review_summary": {"type": "string", "minLength": 1, "maxLength": 4000},
            },
            required=["document_id", "content", "review_summary"],
        ),
        True,
        "_save_paper_summary_version",
    ),
)
APPLICATION_TOOL_HANDLERS = {
    catalog_id: handler_name
    for catalog_id, _name, _description, _schema, _strict, handler_name in APPLICATION_TOOLS
}


def create_tool_catalog(prompts: PromptRegistry | None = None) -> ToolCatalog:
    catalog = ToolCatalog()
    for catalog_id, name, description, schema, strict, _handler_name in APPLICATION_TOOLS:
        prompt_document = prompts.tool_document(catalog_id) if prompts is not None else None
        effective_description = (
            prompt_document.description if prompt_document is not None else description
        )
        documented_schema = _with_parameter_descriptions(
            schema,
            prompt_document.parameter_descriptions if prompt_document is not None else None,
        )
        catalog.register_function_tool(
            FunctionToolDefinition(
                catalog_id=catalog_id,
                label=(
                    prompt_document.label
                    if prompt_document is not None
                    else name.replace("_", " ").title()
                ),
                description=effective_description,
                name=name,
                parameters_schema=documented_schema,
                strict_json_schema=strict,
                factory=_factory(
                    catalog_id,
                    name,
                    effective_description,
                    documented_schema,
                    strict,
                ),
            )
        )
    return catalog


def _with_parameter_descriptions(
    schema: dict[str, Any],
    parameter_descriptions: dict[str, str] | None = None,
) -> dict[str, Any]:
    documented = copy.deepcopy(schema)
    descriptions = parameter_descriptions or {}

    def visit(value: Any) -> None:
        if not isinstance(value, dict):
            return
        properties = value.get("properties")
        if isinstance(properties, dict):
            for parameter_name, parameter_schema in properties.items():
                if isinstance(parameter_schema, dict):
                    parameter_schema.setdefault(
                        "description",
                        descriptions.get(
                            parameter_name,
                            f"{parameter_name.replace('_', ' ').capitalize()}.",
                        ),
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
        async def invoke(invocation: ToolInvocation, raw_arguments: str) -> Any:
            arguments = json.loads(raw_arguments or "{}")
            invoker = invocation.context.tool_runtime.invoke
            if "tool_call_id" in inspect.signature(invoker).parameters:
                return await invoker(
                    catalog_id,
                    arguments,
                    invocation.context,
                    tool_call_id=invocation.tool_call_id,
                )
            return await invoker(catalog_id, arguments, invocation.context)

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
            is_enabled=tool_enabled_after_failures(catalog_id),
        )

    return build
