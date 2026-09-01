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
from backend.tools.failures import (
    recoverable_tool_invoker,
    tool_enabled_after_failures,
)


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
        "extended.plan.update",
        "update_goal_plan",
        "Replace the durable goal plan and mark completed steps.",
        _object_schema(
            {
                "steps": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 12,
                    "items": _object_schema(
                        {
                            "id": {"type": "string", "minLength": 1},
                            "title": {"type": "string", "minLength": 1},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed", "blocked"],
                            },
                        },
                        required=["id", "title", "status"],
                    ),
                },
                "summary": {"type": ["string", "null"]},
            },
            required=["steps", "summary"],
        ),
        True,
    ),
    (
        "extended.block",
        "request_clarification_or_block",
        "Record a blocker only when progress requires missing user input or access.",
        _object_schema(
            {
                "reason": {"type": "string", "minLength": 1},
                "question": {"type": ["string", "null"]},
            },
            required=["reason", "question"],
        ),
        True,
    ),
    (
        "extended.finish",
        "finish_goal",
        "Mark the goal complete with a concise outcome and durable result references.",
        _object_schema(
            {
                "summary": {"type": "string", "minLength": 1},
                "result_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 20,
                },
            },
            required=["summary", "result_refs"],
        ),
        True,
    ),
    (
        "tool.result.read",
        "read_tool_result",
        "Read a bounded slice of a large tool result by its result_ref.",
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
    (
        "tools.search",
        "search_available_tools",
        "Find tools available to the autonomous agent by keyword, name, or catalog ID.",
        _object_schema(
            {
                "query": {
                    "type": ["string", "null"],
                    "description": "Keyword to search for, or null to list every available tool.",
                }
            },
            required=["query"],
        ),
        True,
    ),
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
        "research.pages.read_all",
        "read_all_paper_pages",
        "Read exact extracted page text, including pages previously marked no-keep.",
        _object_schema(
            {
                "document_id": {"type": "string"},
                "start_page": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            required=["document_id", "start_page", "limit"],
        ),
        True,
    ),
    (
        "research.pages.read_retained",
        "read_retained_paper_pages",
        "Read exact page text while excluding pages marked no-keep by the paper cleaner.",
        _object_schema(
            {
                "document_id": {"type": "string"},
                "start_page": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            required=["document_id", "start_page", "limit"],
        ),
        True,
    ),
    (
        "research.page_decisions.save",
        "save_paper_page_decisions",
        "Persist keep or no-keep decisions for reviewed paper pages.",
        _object_schema(
            {
                "document_id": {"type": "string"},
                "decisions": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 10,
                    "items": _object_schema(
                        {
                            "page_number": {"type": "integer", "minimum": 1},
                            "decision": {"type": "string", "enum": ["keep", "no_keep"]},
                            "reason": {"type": "string", "minLength": 1},
                        },
                        required=["page_number", "decision", "reason"],
                    ),
                },
            },
            required=["document_id", "decisions"],
        ),
        True,
    ),
    (
        "research.summaries.save",
        "save_paper_summary",
        "Persist the fixed four-part summary for a paper.",
        _object_schema(
            {
                "document_id": {"type": "string"},
                "contribution": {"type": "string", "minLength": 1},
                "contributions_detail": {"type": "string", "minLength": 1},
                "experimentation_results": {"type": "string", "minLength": 1},
                "open_areas": {
                    "type": "array",
                    "items": _object_schema(
                        {
                            "statement": {"type": "string", "minLength": 1},
                            "citation": {"type": "string", "minLength": 1},
                        },
                        required=["statement", "citation"],
                    ),
                },
            },
            required=[
                "document_id",
                "contribution",
                "contributions_detail",
                "experimentation_results",
                "open_areas",
            ],
        ),
        True,
    ),
    (
        "research.summaries.list",
        "list_paper_summaries",
        "List all saved four-part paper summaries in the repository.",
        _object_schema({}),
        True,
    ),
    (
        "documents.list",
        "list_documents",
        "List papers and report whether each one has readable extracted content.",
        _object_schema({}),
        True,
    ),
    (
        "documents.inspect",
        "inspect_paper",
        "Inspect a paper's source, ingestion state, content statistics, and section outline.",
        _object_schema(
            {"document_id": {"type": "string", "minLength": 1}},
            required=["document_id"],
        ),
        True,
    ),
    (
        "documents.ingest",
        "ingest_paper",
        "Extract and index a paper, preserving native PDF text and using OCR only where needed.",
        _object_schema(
            {"document_id": {"type": "string", "minLength": 1}},
            required=["document_id"],
        ),
        True,
    ),
    (
        "documents.read_pages",
        "read_paper_pages",
        "Read exact paper text by page with stable page citations.",
        _object_schema(
            {
                "document_id": {"type": "string", "minLength": 1},
                "start_page": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            required=["document_id", "start_page", "limit"],
        ),
        True,
    ),
    (
        "documents.read_chunks",
        "read_document_chunks",
        "Read ordered section-aware text chunks with stable page citations.",
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
        "documents.download",
        "download_paper",
        "Download a public PDF URL, extract and index it, and add it to the paper library.",
        _object_schema(
            {
                "pdf_url": {"type": "string", "minLength": 1},
                "title": {"type": ["string", "null"], "maxLength": 300},
            },
            required=["pdf_url", "title"],
        ),
        True,
    ),
    (
        "webpage.download",
        "download_web_page",
        (
            "Temporarily download and extract a public HTML page for chat Q&A. "
            "Inaccessible or empty pages return status 'unavailable' without disabling this tool."
        ),
        _object_schema(
            {"url": {"type": "string", "minLength": 1}},
            required=["url"],
        ),
        True,
    ),
    (
        "webpage.list",
        "list_downloaded_web_pages",
        "List HTML pages currently available in temporary chat storage.",
        _object_schema({}),
        True,
    ),
    (
        "webpage.read",
        "read_downloaded_web_page",
        "Read ordered extracted chunks from a temporarily downloaded HTML page.",
        _object_schema(
            {
                "source_id": {"type": "string", "minLength": 1},
                "start": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            required=["source_id", "start", "limit"],
        ),
        True,
    ),
    (
        "webpage.search",
        "search_downloaded_web_page",
        "Search one temporarily downloaded HTML page for relevant extracted text.",
        _object_schema(
            {
                "source_id": {"type": "string", "minLength": 1},
                "query": {"type": "string", "minLength": 1},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            required=["source_id", "query", "top_k"],
        ),
        True,
    ),
    (
        "webpage.notes.save",
        "save_web_page_note",
        "Persist a Markdown note with the temporary page's title and source URL.",
        _object_schema(
            {
                "source_id": {"type": "string", "minLength": 1},
                "name": {"type": "string", "minLength": 1, "maxLength": 300},
                "content": {"type": "string", "minLength": 1, "maxLength": 200000},
                "tags": {
                    "type": "array",
                    "maxItems": 32,
                    "items": {"type": "string", "minLength": 1, "maxLength": 64},
                },
            },
            required=["source_id", "name", "content", "tags"],
        ),
        True,
    ),
    (
        "web.search",
        "search_web",
        "Search DuckDuckGo and return up to 10 results.",
        _object_schema(
            {"query": {"type": "string", "minLength": 1}},
            required=["query"],
        ),
        True,
    ),
    (
        "arxiv.search",
        "search_arxiv",
        "Search arXiv papers and return metadata, abstracts, and source URLs.",
        _object_schema(
            {
                "query": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            required=["query", "limit"],
        ),
        True,
    ),
    (
        "wikipedia.search",
        "search_wikipedia",
        "Search Wikipedia and return introductory extracts with article URLs.",
        _object_schema(
            {
                "query": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            required=["query", "limit"],
        ),
        True,
    ),
    (
        "workspace.list",
        "list_workspace_files",
        "List every indexed workspace file. Prefer workspace search for discovery in large repositories.",
        _object_schema({}),
        True,
    ),
    (
        "workspace.search",
        "search_workspace",
        "Search indexed notes and paper files by name, content, tags, or document kind.",
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
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "offset": {"type": "integer", "minimum": 0},
            },
            required=["query", "kinds", "tags", "limit", "offset"],
        ),
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
        "workspace.markdown.replace",
        "replace_workspace_markdown",
        "Replace one exact Markdown selection without rewriting the rest of the file.",
        _object_schema(
            {
                "path": {"type": "string", "minLength": 1},
                "old_text": {"type": "string", "minLength": 1},
                "new_text": {"type": "string"},
                "replace_all": {"type": "boolean"},
            },
            required=["path", "old_text", "new_text", "replace_all"],
        ),
        True,
    ),
    (
        "workspace.markdown.append",
        "append_workspace_markdown",
        "Append text to an existing Markdown file without rewriting its current content.",
        _object_schema(
            {
                "path": {"type": "string", "minLength": 1},
                "content": {"type": "string", "minLength": 1},
            },
            required=["path", "content"],
        ),
        True,
    ),
    (
        "workspace.tags.set",
        "set_workspace_file_tags",
        "Replace the searchable tags associated with one workspace file.",
        _object_schema(
            {
                "path": {"type": "string", "minLength": 1},
                "tags": {
                    "type": "array",
                    "maxItems": 32,
                    "items": {"type": "string", "minLength": 1, "maxLength": 64},
                },
            },
            required=["path", "tags"],
        ),
        True,
    ),
    (
        "workspace.tags.search",
        "search_workspace_file_tags",
        "Find workspace files that contain all requested tags.",
        _object_schema(
            {
                "tags": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 32,
                    "items": {"type": "string", "minLength": 1, "maxLength": 64},
                },
            },
            required=["tags"],
        ),
        True,
    ),
    (
        "workspace.note.create",
        "create_workspace_note",
        (
            "Create a named generic Markdown note under notes/<server-generated-uuid>/note.md. "
            "The server generates the ID; provide a concise human-readable name."
        ),
        _object_schema(
            {
                "name": {"type": "string", "minLength": 1, "maxLength": 300},
                "content": {"type": "string"},
                "tags": {
                    "type": "array",
                    "maxItems": 32,
                    "items": {"type": "string", "minLength": 1, "maxLength": 64},
                },
            },
            required=["name", "content", "tags"],
        ),
        True,
    ),
    (
        "workspace.paper.ensure",
        "ensure_paper_workspace",
        "Create or resolve the canonical summary and notes folder for one stored paper.",
        _object_schema(
            {"document_id": {"type": "string", "minLength": 1}},
            required=["document_id"],
        ),
        True,
    ),
    (
        "workspace.paper.name.set",
        "set_paper_workspace_name",
        "Set the human-readable display name for a paper's canonical workspace folder.",
        _object_schema(
            {
                "document_id": {"type": "string", "minLength": 1},
                "paper_name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 300,
                },
            },
            required=["document_id", "paper_name"],
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
