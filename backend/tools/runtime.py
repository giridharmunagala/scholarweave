from __future__ import annotations

import hashlib
import inspect
import json
import uuid
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from backend.agents.blueprint import AgentBlueprint
from backend.agents.catalog import ToolCatalog
from backend.agents.service import AgentService
from backend.agents.templates import starter_blueprints
from backend.autonomous.work import (
    budget_status,
    consume_tool_safety_limit,
    create_work_plan,
    list_work_notes,
    read_work_note,
    save_work_note,
    update_work_item,
)
from backend.builder.todos import (
    create_builder_todo_plan,
    finish_builder_run,
    update_builder_todo,
)
from backend.core.config import Settings
from backend.core.text import clean_filename
from backend.conversations.memory import ConversationMemoryService
from backend.documents import DocumentService
from backend.documents.paper import manifest_pages
from backend.direct_agents.repository import DirectAgentRepository
from backend.documents.retrieval import RetrievalService
from backend.runtime.context import ScholarWeaveContext, ToolReceipt
from backend.runtime.serialization import to_jsonable
from backend.persistence.files import SafeStorage
from backend.research.search import ResearchSearchService
from backend.research.sources import SourceDownloadService
from backend.runtime.priorities import list_priority_decisions, mark_tool_progress
from backend.tools.service import FunctionToolService
from backend.tools.sandbox import SandboxLimits, run_python
from backend.core.json import dumps_json
from backend.workspace.service import WorkspaceService


_WEB_SEARCH_USAGE_KEY = "web_search_requests_used"
_WEB_SEARCH_CACHE_KEY = "web_search_results"
_WEB_SEARCH_ATTEMPTS_KEY = "web_search_attempted_queries"


class ApplicationToolRuntime:
    def __init__(
        self,
        *,
        settings: Settings,
        documents: DocumentService,
        retrieval: RetrievalService,
        storage: SafeStorage,
        workspace: WorkspaceService,
        research_search: ResearchSearchService,
        source_downloads: SourceDownloadService,
        agent_service: AgentService | None = None,
        function_tool_service: FunctionToolService | None = None,
        tool_catalog: ToolCatalog | None = None,
        direct_agent_repository: DirectAgentRepository | None = None,
        conversation_memory: ConversationMemoryService | None = None,
    ) -> None:
        self._settings = settings
        self._documents = documents
        self._retrieval = retrieval
        self._storage = storage
        self._workspace = workspace
        self._research_search = research_search
        self._source_downloads = source_downloads
        self._agents = agent_service
        self._function_tools = function_tool_service
        self._catalog = tool_catalog
        self._direct_agents = direct_agent_repository
        self._conversation_memory = conversation_memory

    async def invoke(
        self,
        catalog_id: str,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> Any:
        handlers = {
            "tool.results.read": self._read_tool_result,
            "builder.todos.create": create_builder_todo_plan,
            "builder.todos.update": update_builder_todo,
            "builder.finish": finish_builder_run,
            "extended.budget.status": budget_status,
            "extended.priorities.list": list_priority_decisions,
            "extended.plan.create": create_work_plan,
            "extended.plan.update": update_work_item,
            "extended.notes.save": self._save_extended_work_note,
            "extended.notes.list": list_work_notes,
            "extended.notes.read": read_work_note,
            "tools.search": self._search_tools,
            "research.pages.read_all": self._read_all_paper_pages,
            "research.pages.read_retained": self._read_retained_paper_pages,
            "research.page_decisions.save": self._save_page_decisions,
            "research.summaries.save": self._save_paper_summary,
            "research.summaries.list": self._list_paper_summaries,
            "documents.list": self._list_documents,
            "documents.inspect": self._inspect_paper,
            "documents.ingest": self._ingest_paper,
            "documents.read_pages": self._read_paper_pages,
            "documents.read_chunks": self._read_document_chunks,
            "retrieval.keyword_search": self._keyword_search,
            "documents.download": self._download_paper,
            "webpage.download": self._download_web_page,
            "webpage.list": self._list_web_pages,
            "webpage.read": self._read_web_page,
            "webpage.search": self._search_web_page,
            "webpage.notes.save": self._save_web_page_note,
            "web.search": self._search_web,
            "arxiv.search": self._search_arxiv,
            "wikipedia.search": self._search_wikipedia,
            "workspace.list": self._list_workspace,
            "workspace.search": self._search_workspace,
            "workspace.read": self._read_workspace,
            "workspace.write": self._write_workspace,
            "workspace.markdown.replace": self._replace_workspace_markdown,
            "workspace.markdown.append": self._append_workspace_markdown,
            "workspace.tags.set": self._set_workspace_tags,
            "workspace.tags.search": self._search_workspace_tags,
            "workspace.note.create": self._create_workspace_note,
            "workspace.paper.ensure": self._ensure_paper_workspace,
            "workspace.paper.name.set": self._set_paper_workspace_name,
            "artifacts.write": self._write_artifact,
            "conversation.memory.search": self._search_conversation_memory,
            "conversation.memory.read": self._read_conversation_memory,
            "python.execute": self._execute_python,
            "sdk.catalog": self._sdk_catalog,
            "agents.list": self._list_agents,
            "agents.get": self._get_agent,
            "agents.validate": self._validate_agent,
            "agents.save": self._save_agent,
            "function_tools.save": self._save_function_tool,
        }
        handler = handlers.get(catalog_id)
        if handler is None:
            raise ValueError(f"Unknown application tool '{catalog_id}'.")
        consume_tool_safety_limit(catalog_id, context)
        result = handler(arguments, context)
        if inspect.isawaitable(result):
            result = await result
        bounded_result = await self.bound_tool_result(catalog_id, result, context)
        mark_tool_progress(catalog_id, context)
        if catalog_id in {"builder.todos.create", "builder.todos.update"}:
            await context.emit("builder.todos.updated", bounded_result)
        if catalog_id in {"extended.plan.create", "extended.plan.update"}:
            await context.emit("extended.plan.updated", bounded_result)
        if catalog_id == "extended.notes.save":
            await context.emit("extended.note.saved", bounded_result)
        await context.emit("tool.application_completed", {"catalog_id": catalog_id})
        return bounded_result

    def store_context_checkpoint(
        self,
        checkpoint: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        checkpoint_id = str(checkpoint["checkpoint_id"])
        relative_path = f"runs/{context.run_id}/checkpoints/{checkpoint_id}.json"
        stored = self._storage.write_json(
            self._settings.artifacts_dir,
            relative_path,
            checkpoint,
        )
        artifact = self._documents.create_artifact_record(
            owner_type="agent_run",
            kind="context_checkpoint",
            relative_path=relative_path,
            media_type="application/json",
            stored=stored,
            metadata={
                "agent_run_id": context.run_id,
                "checkpoint_id": checkpoint_id,
            },
        )
        return {
            "artifact_id": artifact.id,
            "path": relative_path,
            "size_bytes": stored.size_bytes,
        }

    async def bound_tool_result(
        self,
        catalog_id: str,
        result: Any,
        context: ScholarWeaveContext,
        *,
        max_tokens: int | None = None,
    ) -> Any:
        jsonable = to_jsonable(result)
        serialized = json.dumps(
            jsonable,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        max_characters = (max_tokens or self._settings.tool_result_max_tokens) * 4
        if len(serialized) <= max_characters:
            return result

        fingerprint = hashlib.sha256(
            f"{catalog_id}\0{max_characters}\0{serialized}".encode("utf-8")
        ).hexdigest()
        cache = context.metadata.setdefault("_bounded_tool_results", {})
        if not isinstance(cache, dict):
            cache = {}
            context.metadata["_bounded_tool_results"] = cache
        cached = cache.get(fingerprint)
        if isinstance(cached, dict):
            return cached

        result_id = str(uuid.uuid4())
        relative_path = f"runs/{context.run_id}/tool-results/{result_id}.json"
        stored = self._storage.write_text(
            self._settings.artifacts_dir,
            relative_path,
            serialized,
        )
        artifact = self._documents.create_artifact_record(
            owner_type="agent_run",
            kind="tool_result",
            relative_path=relative_path,
            media_type="application/json",
            stored=stored,
            metadata={
                "agent_run_id": context.run_id,
                "catalog_id": catalog_id,
                "original_characters": len(serialized),
            },
        )
        compacted = {
            "truncated": True,
            "catalog_id": catalog_id,
            "result_ref": artifact.id,
            "original_characters": len(serialized),
            "preview": _compact_tool_result(jsonable),
            "read_more": {
                "tool": "read_tool_result",
                "arguments": {
                    "result_ref": artifact.id,
                    "start": 0,
                    "max_characters": min(max_characters, 12_000),
                },
            },
        }
        preview = compacted["preview"]
        while (
            len(json.dumps(compacted, ensure_ascii=False, separators=(",", ":")))
            > max_characters
        ):
            if preview["excerpts"]:
                preview["excerpts"].pop()
            elif preview["identifiers"]:
                preview["identifiers"].pop()
            elif preview["references"]:
                preview["references"].pop()
            else:
                break
        cache[fingerprint] = compacted
        while len(cache) > 100:
            cache.pop(next(iter(cache)))
        await context.emit(
            "tool.result_truncated",
            {
                "catalog_id": catalog_id,
                "result_ref": artifact.id,
                "original_characters": len(serialized),
                "returned_characters": len(
                    json.dumps(compacted, ensure_ascii=False, separators=(",", ":"))
                ),
            },
        )
        return compacted

    def _read_tool_result(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        result_ref = str(arguments["result_ref"])
        artifact = self._documents.get_artifact(result_ref)
        metadata = artifact.metadata_json if artifact is not None else None
        if (
            artifact is None
            or artifact.owner_type != "agent_run"
            or artifact.kind != "tool_result"
            or not isinstance(metadata, dict)
            or metadata.get("agent_run_id") != context.run_id
        ):
            raise ValueError("Tool result reference was not found for this run.")
        content = self._documents.artifact_bytes(artifact).decode("utf-8")
        start = int(arguments["start"])
        max_result_characters = self._settings.tool_result_max_tokens * 4
        limit = min(
            int(arguments["max_characters"]),
            max_result_characters,
        )
        excerpt = content[start : start + limit]
        while True:
            end = start + len(excerpt)
            response = {
                "result_ref": result_ref,
                "start": start,
                "end": end,
                "total_characters": len(content),
                "content": excerpt,
                "has_more": end < len(content),
                "next_start": end,
            }
            response_characters = len(
                json.dumps(response, ensure_ascii=False, separators=(",", ":"))
            )
            if response_characters <= max_result_characters or not excerpt:
                return response
            excerpt = excerpt[
                : max(0, len(excerpt) - (response_characters - max_result_characters) - 32)
            ]

    def _search_conversation_memory(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        memory = self._require_conversation_memory()
        query = str(arguments["query"]).strip()
        matches = memory.search(
            query,
            exclude_conversation_id=context.conversation_id,
            limit=int(arguments["limit"]),
        )
        return {
            "query": query,
            "matches": matches,
            "reuse_guidance": (
                "Read and reuse only when the request, assumptions, and evidence clearly match. "
                "Otherwise update the work."
            ),
        }

    def _read_conversation_memory(
        self,
        arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        return self._require_conversation_memory().read(str(arguments["run_id"]))

    async def _execute_python(
        self,
        arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        if not self._settings.python_tool_enabled:
            raise ValueError("Sandboxed Python execution is disabled in Settings.")
        result = await run_python(
            str(arguments["code"]),
            dict(arguments["inputs"]),
            limits=SandboxLimits(
                timeout_seconds=self._settings.python_tool_timeout_seconds,
                memory_mb=self._settings.python_tool_memory_mb,
            ),
            allowed_imports=self._settings.python_tool_allowed_imports,
            entrypoint="compute",
        )
        return {"output": result.value, "stdout": result.stdout}

    def _search_tools(
        self,
        arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        from backend.autonomous.service import AUTONOMOUS_TOOL_IDS

        available_catalog_ids = {catalog_id for _, catalog_id in AUTONOMOUS_TOOL_IDS}
        query = str(arguments.get("query") or "").strip().casefold()
        definitions = self._catalog.definitions() if self._catalog is not None else ()
        tools = [
            {
                "catalog_id": definition.catalog_id,
                "name": definition.name,
                "description": definition.description,
                "parameters_schema": definition.parameters_schema,
                "requires_approval": False,
            }
            for definition in definitions
            if definition.catalog_id in available_catalog_ids
        ]
        if self._function_tools is not None:
            tools.extend(
                {
                    "catalog_id": f"custom:{document.latest_revision.id}",
                    "name": document.record.name,
                    "description": document.latest_revision.description,
                    "parameters_schema": document.latest_revision.parameters_schema_json,
                    "requires_approval": document.latest_revision.requires_approval,
                }
                for document in self._function_tools.list()
            )
        if query:
            tools = [
                tool
                for tool in tools
                if query
                in " ".join(
                    str(tool.get(field) or "")
                    for field in ("catalog_id", "name", "description")
                ).casefold()
            ]
        return {"query": query or None, "count": len(tools), "tools": tools}

    def _read_all_paper_pages(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        document_id = self._scoped_document(arguments, context)
        result = self._read_pages(document_id, arguments, retained_only=False)
        self._record_read_pages(context, result)
        return result

    def _read_retained_paper_pages(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        document_id = self._scoped_document(arguments, context)
        result = self._read_pages(document_id, arguments, retained_only=True)
        self._record_read_pages(context, result)
        return result

    def _read_pages(
        self,
        document_id: str,
        arguments: dict[str, Any],
        *,
        retained_only: bool,
    ) -> dict[str, Any]:
        details = self._documents.get_document_details(document_id)
        if details is None:
            raise ValueError("Paper was not found.")
        document, artifacts, _ = details
        manifest_artifact = next(
            (artifact for artifact in artifacts if artifact.kind == "extracted_manifest"),
            None,
        )
        if document.status != "ready" or manifest_artifact is None:
            raise ValueError("Paper must be ingested before its pages can be read.")
        manifest = self._documents.artifact_content(manifest_artifact)
        if not isinstance(manifest, dict) or not isinstance(manifest.get("pages"), list):
            raise ValueError("Paper page manifest is invalid.")
        raw_pages = [page for page in manifest["pages"] if isinstance(page, dict)]
        decisions = self._require_direct_agents().page_decisions(document_id)
        pages = [
            {
                "page_number": int(page["page"]),
                "text": str(page.get("text") or ""),
                "decision": decisions.get(int(page["page"]), "unreviewed"),
            }
            for page in raw_pages
            if not retained_only or decisions.get(int(page["page"])) != "no_keep"
        ]
        start_page = int(arguments["start_page"])
        limit = int(arguments["limit"])
        available = [page for page in pages if page["page_number"] >= start_page]
        selected = available[:limit]
        next_page = selected[-1]["page_number"] + 1 if selected else start_page
        return {
            "document_id": document_id,
            "title": document.title,
            "total_pages": len(raw_pages),
            "available_pages": len(pages),
            "pages": selected,
            "has_more": len(available) > len(selected),
            "next_page": next_page,
        }

    def _save_page_decisions(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        document_id = self._scoped_document(arguments, context)
        if context.metadata.get("direct_agent_key") != "paper_cleaner":
            raise ValueError("Only the paper cleaner can save page decisions.")
        decisions = list(arguments["decisions"])
        document = self._documents.get_document(document_id)
        if document is None or document.page_count is None:
            raise ValueError("Paper must be ingested before page decisions can be saved.")
        page_numbers = [int(decision["page_number"]) for decision in decisions]
        if len(page_numbers) != len(set(page_numbers)):
            raise ValueError("A decision batch cannot contain duplicate page numbers.")
        if any(page < 1 or page > document.page_count for page in page_numbers):
            raise ValueError("A page decision is outside the paper's page range.")
        staged = context.metadata.get("paper_page_decisions")
        staged_by_page = dict(staged) if isinstance(staged, dict) else {}
        for decision in decisions:
            staged_by_page[str(int(decision["page_number"]))] = decision
        context.metadata["paper_page_decisions"] = staged_by_page
        decided_pages = set(context.metadata.get("paper_pages_decided", []))
        decided_pages.update(page_numbers)
        context.metadata["paper_pages_decided"] = sorted(decided_pages)
        return {
            "document_id": document_id,
            "saved_count": len(decisions),
            "total_saved": len(staged_by_page),
            "total_pages": document.page_count,
            "status": "staged_until_run_completes",
        }

    def _save_paper_summary(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        document_id = self._scoped_document(arguments, context)
        if context.metadata.get("direct_agent_key") != "summary":
            raise ValueError("Only the summary agent can save paper summaries.")
        context.metadata["paper_summary"] = {
            "contribution": str(arguments["contribution"]),
            "contributions_detail": str(arguments["contributions_detail"]),
            "experimentation_results": str(arguments["experimentation_results"]),
            "open_areas": list(arguments["open_areas"]),
        }
        context.metadata["paper_summary_saved"] = True
        return {
            "document_id": document_id,
            "saved": True,
            "status": "staged_until_run_completes",
        }

    def _list_paper_summaries(
        self,
        _arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> list[dict[str, Any]]:
        if context.metadata.get("direct_agent_key") != "open_areas":
            raise ValueError("Only the open-areas agent can list stored paper summaries.")
        context.metadata["paper_summaries_listed"] = True
        return self._require_direct_agents().list_summaries()

    @staticmethod
    def _scoped_document(
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> str:
        document_id = str(arguments["document_id"])
        allowed = context.metadata.get("direct_agent_document_ids")
        if not isinstance(allowed, list) or document_id not in allowed:
            raise ValueError("The requested paper is outside this direct-agent conversation.")
        return document_id

    @staticmethod
    def _record_read_pages(
        context: ScholarWeaveContext,
        result: dict[str, Any],
    ) -> None:
        context.metadata["paper_pages_tool_called"] = True
        read_pages = set(context.metadata.get("paper_pages_read", []))
        read_pages.update(
            int(page["page_number"])
            for page in result["pages"]
            if isinstance(page, dict) and "page_number" in page
        )
        context.metadata["paper_pages_read"] = sorted(read_pages)

    def _list_documents(
        self,
        _arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> list[dict[str, Any]]:
        documents = []
        for document in self._documents.list_documents():
            details = self._documents.get_document_details(document.id)
            artifacts = details[1] if details else []
            chunks = details[2] if details else []
            has_manifest = any(
                artifact.kind == "extracted_manifest" for artifact in artifacts
            )
            readable = document.status == "ready" and has_manifest and bool(chunks)
            documents.append(
                {
                    "id": document.id,
                    "title": document.title,
                    "status": document.status,
                    "page_count": document.page_count,
                    "readable": readable,
                    "chunk_count": len(chunks),
                    "next_action": None if readable else "inspect_paper",
                }
            )
        return documents

    def _inspect_paper(
        self,
        arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        document_id = str(arguments["document_id"])
        details = self._documents.get_document_details(document_id)
        if details is None:
            raise ValueError("Paper was not found.")
        document, artifacts, chunks = details
        source = next(
            (artifact for artifact in artifacts if artifact.kind == "source_pdf"),
            None,
        )
        manifest_artifact = next(
            (artifact for artifact in artifacts if artifact.kind == "extracted_manifest"),
            None,
        )
        manifest = (
            self._documents.artifact_content(manifest_artifact)
            if manifest_artifact is not None
            else None
        )
        content = manifest.get("content", {}) if isinstance(manifest, dict) else {}
        sections = manifest.get("sections", []) if isinstance(manifest, dict) else []
        chunk_chars = sum(len(chunk.text) for chunk in chunks)
        readable = (
            document.status == "ready"
            and manifest_artifact is not None
            and bool(chunks)
            and bool(content.get("char_count") or chunk_chars)
        )
        return {
            "document_id": document.id,
            "title": document.title,
            "source_filename": document.source_filename,
            "status": document.status,
            "page_count": document.page_count,
            "source_available": source is not None,
            "readable": readable,
            "content": {
                "char_count": int(content.get("char_count") or chunk_chars),
                "chunk_count": len(chunks),
                "nonempty_page_count": content.get("nonempty_page_count"),
            },
            "sections": sections,
            "next_action": (
                None
                if readable
                else "Call ingest_paper with mode='embedded', or mode='ocr' for scanned pages."
            ),
        }

    async def _ingest_paper(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        document_id = str(arguments["document_id"])
        mode = str(arguments["mode"])
        await self._documents.ingest_document(document_id, force_ocr=mode == "ocr")
        return self._inspect_paper({"document_id": document_id}, context)

    def _read_paper_pages(
        self,
        arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        document_id = str(arguments["document_id"])
        details = self._documents.get_document_details(document_id)
        if details is None:
            raise ValueError("Paper was not found.")
        document, artifacts, chunks = details
        manifest_artifact = next(
            (artifact for artifact in artifacts if artifact.kind == "extracted_manifest"),
            None,
        )
        if document.status != "ready" or manifest_artifact is None or not chunks:
            raise ValueError(
                "Paper has no readable extracted content. Inspect and ingest it before reading."
            )
        pages = manifest_pages(self._documents.artifact_content(manifest_artifact))
        start_page = int(arguments["start_page"])
        limit = int(arguments["limit"])
        available = [
            page for page in pages if int(page.get("page") or 0) >= start_page
        ]
        selected = available[:limit]
        return {
            "document_id": document_id,
            "title": document.title,
            "page_count": len(pages),
            "pages": [
                {
                    "page_number": int(page["page"]),
                    "citation": str(page.get("citation") or f"p.{int(page['page'])}"),
                    "text": str(page.get("text") or ""),
                }
                for page in selected
            ],
            "has_more": len(available) > len(selected),
            "next_page": (
                int(selected[-1]["page"]) + 1 if selected else start_page
            ),
        }

    def _read_document_chunks(
        self,
        arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> list[dict[str, Any]]:
        start = int(arguments.get("start") or 0)
        limit = int(arguments.get("limit") or 20)
        document_id = str(arguments["document_id"])
        document = self._documents.get_document(document_id)
        if document is None:
            raise ValueError("Paper was not found.")
        chunks = self._retrieval.fetch_document_chunks(document_id)
        if document.status != "ready" or not chunks:
            raise ValueError(
                "Paper has no readable extracted content. Inspect and ingest it before reading."
            )
        return [
            {
                "chunk_id": chunk.id,
                "chunk_index": chunk.chunk_index,
                "section_title": chunk.section_title,
                "citation": chunk.citation,
                "page_start": chunk.page_start,
                "page_end": chunk.page_end,
                "text": chunk.text,
            }
            for chunk in chunks[start : start + limit]
        ]

    def _keyword_search(
        self,
        arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> list[dict[str, Any]]:
        return self._retrieval.keyword_search(
            str(arguments["query"]),
            document_id=arguments.get("document_id"),
            top_k=int(arguments.get("top_k") or 5),
        )

    async def _download_paper(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        document = await self._source_downloads.download_pdf(
            str(arguments["pdf_url"]),
            title=str(arguments["title"]) if arguments.get("title") else None,
            arxiv_only=True,
        )
        return self._inspect_paper({"document_id": document.id}, context)

    async def _download_web_page(
        self,
        arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        source = await self._source_downloads.download_web_page(str(arguments["url"]))
        return self._web_source_result(source)

    async def _list_web_pages(
        self,
        _arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> list[dict[str, Any]]:
        return [
            self._web_source_result(source)
            for source in await self._source_downloads.list_web_sources()
        ]

    async def _read_web_page(
        self,
        arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        source = await self._source_downloads.get_web_source(str(arguments["source_id"]))
        start = int(arguments["start"])
        limit = int(arguments["limit"])
        selected = source.chunks[start : start + limit]
        return {
            **self._web_source_result(source),
            "chunks": [
                {
                    "chunk_index": start + offset,
                    "citation": source.url,
                    "text": text,
                }
                for offset, text in enumerate(selected)
            ],
            "has_more": start + len(selected) < len(source.chunks),
            "next_start": start + len(selected),
        }

    async def _search_web_page(
        self,
        arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> list[dict[str, Any]]:
        return await self._source_downloads.search_web_source(
            str(arguments["source_id"]),
            str(arguments["query"]),
            top_k=int(arguments["top_k"]),
        )

    async def _save_web_page_note(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        document = await self._source_downloads.save_web_note(
            str(arguments["source_id"]),
            name=str(arguments["name"]),
            content=str(arguments["content"]),
            tags=list(arguments["tags"]),
        )
        self._append_workspace_receipt(context, document, "Created")
        return {
            **self._workspace_result(document),
            "note_id": document.note_id,
            "name": document.note_name,
            "kind": document.kind,
        }

    async def _search_web(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        query = " ".join(
            str(arguments["query"])
            .translate(str.maketrans("", "", "\"\u201c\u201d"))
            .split()
        )
        limit = int(arguments["limit"])
        if not query:
            raise ValueError("Search query cannot be empty.")
        if limit != 10:
            raise ValueError("Agent web searches must request exactly 10 results.")

        cache = context.metadata.setdefault(_WEB_SEARCH_CACHE_KEY, {})
        if not isinstance(cache, dict):
            raise ValueError("The web-search session cache is invalid.")
        cache_key = query.casefold()
        attempts = context.metadata.setdefault(_WEB_SEARCH_ATTEMPTS_KEY, [])
        if not isinstance(attempts, list) or any(
            not isinstance(item, str) for item in attempts
        ):
            raise ValueError("The web-search session attempt history is invalid.")
        cached = cache.get(cache_key)
        if cached is not None:
            if not isinstance(cached, dict):
                raise ValueError("The cached web-search result is invalid.")
            cached_limit = cached.get("limit")
            cached_result = cached.get("result")
            cached_results = (
                cached_result.get("results") if isinstance(cached_result, dict) else None
            )
            if (
                isinstance(cached_limit, int)
                and cached_limit >= limit
                and isinstance(cached_result, dict)
                and isinstance(cached_results, list)
            ):
                return self._web_search_result(
                    {
                        **cached_result,
                        "query": query,
                        "results": cached_results[:limit],
                    },
                    context,
                    cached=True,
                )
        if cache_key in attempts:
            raise ValueError(
                "This normalized web query was already attempted in the current session. "
                "Use its earlier results instead of retrying it with a different result limit."
            )

        used = self._web_search_requests_used(context)
        maximum = self._settings.web_search_max_requests_per_session
        if used >= maximum:
            raise ValueError(
                f"The web-search session limit was reached ({used}/{maximum}). "
                "Use the results already gathered instead of rephrasing or retrying searches."
            )
        context.metadata[_WEB_SEARCH_USAGE_KEY] = used + 1
        attempts.append(cache_key)
        result = await self._research_search.search_web(query, limit)
        cache[cache_key] = {"limit": limit, "result": result}
        return self._web_search_result(result, context, cached=False)

    def _web_search_result(
        self,
        result: dict[str, Any],
        context: ScholarWeaveContext,
        *,
        cached: bool,
    ) -> dict[str, Any]:
        used = self._web_search_requests_used(context)
        maximum = self._settings.web_search_max_requests_per_session
        return {
            **result,
            "cached": cached,
            "search_budget": {
                "used": used,
                "limit": maximum,
                "remaining": max(0, maximum - used),
            },
        }

    @staticmethod
    def _web_search_requests_used(context: ScholarWeaveContext) -> int:
        value = context.metadata.get(_WEB_SEARCH_USAGE_KEY, 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("The web-search session usage counter is invalid.")
        return value

    async def _search_arxiv(
        self,
        arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        return await self._research_search.search_arxiv(
            str(arguments["query"]),
            int(arguments["limit"]),
        )

    async def _search_wikipedia(
        self,
        arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        return await self._research_search.search_wikipedia(
            str(arguments["query"]),
            int(arguments["limit"]),
        )

    @staticmethod
    def _web_source_result(source) -> dict[str, Any]:
        return {
            "source_id": source.id,
            "title": source.title,
            "url": source.url,
            "chunk_count": len(source.chunks),
            "created_at": source.created_at.isoformat(),
            "expires_at": source.expires_at.isoformat(),
        }

    def _list_workspace(
        self,
        _arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> list[dict[str, Any]]:
        return [
            {"path": document.path, "tags": list(document.tags)}
            for document in self._workspace.list_files()
        ]

    def _read_workspace(
        self,
        arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        document = self._workspace.read_file(str(arguments["path"]))
        return {
            "path": document.path,
            "media_type": document.media_type,
            "tags": list(document.tags),
            "content": document.content,
        }

    def _search_workspace(
        self,
        arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> list[dict[str, Any]]:
        return [
            {
                "path": document.path,
                "name": document.note_name or document.paper_name or document.name,
                "kind": document.kind,
                "tags": list(document.tags),
                "modified_at": document.modified_at.isoformat(),
            }
            for document in self._workspace.search(
                query=arguments.get("query"),
                kinds=list(arguments["kinds"]),
                tags=list(arguments["tags"]),
                limit=int(arguments["limit"]),
                offset=int(arguments["offset"]),
            )
        ]

    def _write_workspace(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        document = self._workspace.write_file(
            str(arguments["path"]),
            arguments["content"],
        )
        receipt = ToolReceipt(
            kind="file",
            title=f"Wrote {document.path}",
            href=f"/workspace?path={document.path}",
            metadata={"sha256": document.sha256},
        )
        context.receipts.append(receipt)
        return {
            "path": document.path,
            "size_bytes": document.size_bytes,
            "sha256": document.sha256,
            "tags": list(document.tags),
        }

    def _replace_workspace_markdown(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        document = self._workspace.replace_markdown(
            str(arguments["path"]),
            str(arguments["old_text"]),
            str(arguments["new_text"]),
            replace_all=bool(arguments["replace_all"]),
        )
        self._append_workspace_receipt(context, document, "Updated")
        return self._workspace_result(document)

    def _append_workspace_markdown(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        document = self._workspace.append_markdown(
            str(arguments["path"]),
            str(arguments["content"]),
        )
        self._append_workspace_receipt(context, document, "Updated")
        return self._workspace_result(document)

    def _set_workspace_tags(
        self,
        arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        document = self._workspace.set_tags(
            str(arguments["path"]),
            list(arguments["tags"]),
        )
        return {"path": document.path, "tags": list(document.tags)}

    def _search_workspace_tags(
        self,
        arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> list[dict[str, Any]]:
        return [
            {"path": document.path, "tags": list(document.tags)}
            for document in self._workspace.list_files(tags=list(arguments["tags"]))
        ]

    def _ensure_paper_workspace(
        self,
        arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        document_id = str(arguments["document_id"])
        document = self._documents.get_document(document_id)
        if document is None:
            raise ValueError("Paper was not found.")
        return self._workspace.ensure_paper_folder(document.id, document.title)

    def _create_workspace_note(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        document = self._workspace.create_note(
            name=str(arguments["name"]),
            content=str(arguments["content"]),
            tags=list(arguments["tags"]),
        )
        self._append_workspace_receipt(context, document, "Created")
        return {
            **self._workspace_result(document),
            "note_id": document.note_id,
            "name": document.note_name,
            "kind": document.kind,
        }

    def _save_extended_work_note(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        result = save_work_note(arguments, context)
        sources = list(result["sources"])
        source_section = (
            "\n\n## Sources\n\n" + "\n".join(f"- {source}" for source in sources)
            if sources
            else ""
        )
        folder = clean_filename(context.run_id)
        filename = clean_filename(f"{result['note_id']}-{result['title']}")
        path = f"extended-work-notes/{folder}/{filename}.md"
        try:
            document = self._workspace.write_file(
                path,
                f"# {result['title']}\n\n{result['content']}{source_section}\n",
                tags=["extended-work", f"run:{context.run_id}"],
            )
        except Exception:
            notes = context.metadata.get("extended_work_notes")
            if isinstance(notes, dict):
                notes.pop(result["note_id"], None)
            raise
        self._append_workspace_receipt(context, document, "Created")
        return {
            **result,
            "workspace_path": document.path,
        }

    def _set_paper_workspace_name(
        self,
        arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> dict[str, str]:
        document_id = str(arguments["document_id"])
        document = self._documents.get_document(document_id)
        if document is None:
            raise ValueError("Paper was not found.")
        self._workspace.ensure_paper_folder(document.id, document.title)
        return self._workspace.set_paper_name(
            document.id,
            str(arguments["paper_name"]),
        )

    @staticmethod
    def _workspace_result(document) -> dict[str, Any]:
        return {
            "path": document.path,
            "size_bytes": document.size_bytes,
            "sha256": document.sha256,
            "tags": list(document.tags),
        }

    @staticmethod
    def _append_workspace_receipt(
        context: ScholarWeaveContext,
        document,
        action: str,
    ) -> None:
        context.receipts.append(
            ToolReceipt(
                kind="file",
                title=f"{action} {document.path}",
                href=f"/workspace?path={document.path}",
                metadata={"sha256": document.sha256},
            )
        )
    def _write_artifact(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        media_type = str(arguments["media_type"])
        content = arguments["content"]
        if media_type == "application/json":
            serialized = dumps_json(content)
        elif isinstance(content, str):
            serialized = content
        else:
            serialized = dumps_json(content)
        relative_path = f"runs/{context.run_id}/{arguments['path']}"
        stored = self._storage.write_text(
            self._settings.artifacts_dir,
            relative_path,
            serialized,
        )
        artifact = self._documents.create_artifact_record(
            owner_type="agent_run",
            kind="generated",
            relative_path=relative_path,
            media_type=media_type,
            stored=stored,
            metadata={"agent_run_id": context.run_id},
        )
        receipt = ToolReceipt(
            kind="artifact",
            title=f"Created {arguments['path']}",
            href=f"/api/artifacts/{artifact.id}",
            metadata={"artifact_id": artifact.id},
        )
        context.receipts.append(receipt)
        return {
            "artifact_id": artifact.id,
            "path": relative_path,
            "media_type": media_type,
            "size_bytes": stored.size_bytes,
        }

    def _sdk_catalog(
        self,
        _arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        definitions = self._catalog.definitions() if self._catalog is not None else ()
        return {
            "primitives": [
                "Agent",
                "FunctionTool",
                "Agent.as_tool",
                "Handoff",
                "InputGuardrail",
                "OutputGuardrail",
                "Agent.output_type",
                "ModelSettings",
                "RunConfig",
                "Session",
            ],
            "function_tools": [
                {
                    "catalog_id": definition.catalog_id,
                    "name": definition.name,
                    "description": definition.description,
                    "parameters_schema": definition.parameters_schema,
                }
                for definition in definitions
            ],
            "agent_blueprint_schema": AgentBlueprint.model_json_schema(by_alias=True),
            "agent_blueprint_examples": [
                blueprint.model_dump(mode="json", by_alias=True)
                for blueprint in starter_blueprints()
            ],
        }

    def _list_agents(
        self,
        _arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> list[dict[str, Any]]:
        agents = self._require_agents()
        return [
            {
                "id": document.record.id,
                "name": document.record.name,
                "description": document.record.description,
                "revision": document.latest_revision.revision,
            }
            for document in agents.list()
        ]

    def _get_agent(
        self,
        arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        document = self._require_agents().get(str(arguments["agent_id"]))
        return {
            "id": document.record.id,
            "revision_id": document.latest_revision.id,
            "revision": document.latest_revision.revision,
            "blueprint": document.latest_revision.blueprint_json,
            "presentation": document.latest_revision.presentation_json,
        }

    def _validate_agent(
        self,
        arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        try:
            blueprint = AgentBlueprint.model_validate(arguments["blueprint"])
        except PydanticValidationError as exc:
            return {
                "valid": False,
                "issues": [
                    f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                    for error in exc.errors(include_url=False, include_input=False)
                ],
            }
        issues = self._require_agents().validate(blueprint)
        return {"valid": not issues, "issues": list(issues)}

    def _save_agent(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        agents = self._require_agents()
        blueprint = AgentBlueprint.model_validate(arguments["blueprint"])
        agent_id = arguments.get("agent_id")
        if agent_id:
            document = agents.update(
                str(agent_id),
                blueprint,
                presentation=arguments.get("presentation") or {},
            )
        else:
            document = agents.create(
                blueprint,
                presentation=arguments.get("presentation") or {},
            )
        context.receipts.append(
            ToolReceipt(
                kind="agent",
                title=f"Saved {document.record.name}",
                href=f"/agents/{document.record.id}",
                metadata={"revision_id": document.latest_revision.id},
            )
        )
        return {
            "agent_id": document.record.id,
            "revision_id": document.latest_revision.id,
            "revision": document.latest_revision.revision,
        }

    def _save_function_tool(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        service = self._require_function_tools()
        definition_id = arguments.get("definition_id")
        payload = {
            "name": str(arguments["name"]),
            "description": str(arguments.get("description") or ""),
            "parameters_schema": arguments["parameters_schema"],
            "output_schema": arguments.get("output_schema"),
            "code": str(arguments["code"]),
            "requires_approval": bool(arguments.get("requires_approval", False)),
        }
        document = (
            service.update(str(definition_id), **payload)
            if definition_id
            else service.create(**payload)
        )
        context.receipts.append(
            ToolReceipt(
                kind="function_tool",
                title=f"Saved {document.record.name}",
                href="/tools",
                metadata={"revision_id": document.latest_revision.id},
            )
        )
        return {
            "definition_id": document.record.id,
            "revision_id": document.latest_revision.id,
            "revision": document.latest_revision.revision,
            "catalog_id": f"custom:{document.latest_revision.id}",
        }

    def _require_agents(self) -> AgentService:
        if self._agents is None:
            raise RuntimeError("Agent tools are not configured.")
        return self._agents

    def _require_function_tools(self) -> FunctionToolService:
        if self._function_tools is None:
            raise RuntimeError("Function-tool authoring is not configured.")
        return self._function_tools

    def _require_conversation_memory(self) -> ConversationMemoryService:
        if self._conversation_memory is None:
            raise RuntimeError("Conversation memory is unavailable.")
        return self._conversation_memory

    def _require_direct_agents(self) -> DirectAgentRepository:
        if self._direct_agents is None:
            raise RuntimeError("Direct research-agent tools are not configured.")
        return self._direct_agents


def _compact_tool_result(value: Any) -> dict[str, Any]:
    identifiers: list[dict[str, Any]] = []
    excerpts: list[dict[str, str]] = []
    references: list[str] = []
    seen: set[str] = set()

    def collect(item: Any) -> None:
        if len(identifiers) >= 30 and len(excerpts) >= 12:
            return
        if isinstance(item, dict):
            selected = {
                str(key): _compact_scalar(child)
                for key, child in item.items()
                if str(key).casefold()
                in {
                    "id",
                    "document_id",
                    "source_id",
                    "artifact_id",
                    "title",
                    "name",
                    "url",
                    "citation",
                    "score",
                    "rank",
                    "status",
                }
                and isinstance(child, (str, int, float, bool))
            }
            if selected:
                fingerprint = json.dumps(selected, sort_keys=True, ensure_ascii=False)
                if fingerprint not in seen:
                    seen.add(fingerprint)
                    identifiers.append(selected)
            for key, child in item.items():
                normalized = str(key).casefold()
                if normalized in {
                    "url",
                    "citation",
                    "result_ref",
                    "artifact_id",
                } and isinstance(child, str):
                    if child not in references:
                        references.append(child)
                if normalized in {
                    "text",
                    "content",
                    "summary",
                    "abstract",
                    "snippet",
                    "description",
                } and isinstance(child, str):
                    collapsed = " ".join(child.split())
                    excerpt = (
                        collapsed
                        if len(collapsed) <= 320
                        else f"{collapsed[:319]}…"
                    )
                    entry = {"field": str(key), "excerpt": excerpt}
                    if entry not in excerpts:
                        excerpts.append(entry)
                collect(child)
        elif isinstance(item, list):
            for child in item:
                collect(child)

    collect(value)
    return {
        "identifiers": identifiers[:8],
        "references": [_compact_scalar(value) for value in references[:12]],
        "excerpts": excerpts[:8],
    }


def _compact_scalar(value: Any, limit: int = 240) -> Any:
    if not isinstance(value, str):
        return value
    collapsed = " ".join(value.split())
    return collapsed if len(collapsed) <= limit else f"{collapsed[: limit - 1]}…"
