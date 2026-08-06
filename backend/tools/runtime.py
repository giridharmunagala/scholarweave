from __future__ import annotations

from typing import Any

from pydantic import ValidationError as PydanticValidationError

from backend.agents.blueprint import AgentBlueprint
from backend.agents.catalog import ToolCatalog
from backend.agents.service import AgentService
from backend.agents.templates import starter_blueprints
from backend.builder.todos import (
    create_builder_todo_plan,
    finish_builder_run,
    update_builder_todo,
)
from backend.core.config import Settings
from backend.documents import DocumentService
from backend.direct_agents.repository import DirectAgentRepository
from backend.documents.retrieval import RetrievalService
from backend.runtime.context import ScholarWeaveContext, ToolReceipt
from backend.persistence.files import SafeStorage
from backend.tools.service import FunctionToolService
from backend.core.json import dumps_json
from backend.workspace.service import WorkspaceService


class ApplicationToolRuntime:
    def __init__(
        self,
        *,
        settings: Settings,
        documents: DocumentService,
        retrieval: RetrievalService,
        storage: SafeStorage,
        workspace: WorkspaceService,
        agent_service: AgentService | None = None,
        function_tool_service: FunctionToolService | None = None,
        tool_catalog: ToolCatalog | None = None,
        direct_agent_repository: DirectAgentRepository | None = None,
    ) -> None:
        self._settings = settings
        self._documents = documents
        self._retrieval = retrieval
        self._storage = storage
        self._workspace = workspace
        self._agents = agent_service
        self._function_tools = function_tool_service
        self._catalog = tool_catalog
        self._direct_agents = direct_agent_repository

    async def invoke(
        self,
        catalog_id: str,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> Any:
        handlers = {
            "builder.todos.create": create_builder_todo_plan,
            "builder.todos.update": update_builder_todo,
            "builder.finish": finish_builder_run,
            "research.pages.read_all": self._read_all_paper_pages,
            "research.pages.read_retained": self._read_retained_paper_pages,
            "research.page_decisions.save": self._save_page_decisions,
            "research.summaries.save": self._save_paper_summary,
            "research.summaries.list": self._list_paper_summaries,
            "documents.list": self._list_documents,
            "documents.read_chunks": self._read_document_chunks,
            "retrieval.keyword_search": self._keyword_search,
            "workspace.list": self._list_workspace,
            "workspace.read": self._read_workspace,
            "workspace.write": self._write_workspace,
            "artifacts.write": self._write_artifact,
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
        result = handler(arguments, context)
        if catalog_id in {"builder.todos.create", "builder.todos.update"}:
            await context.emit("builder.todos.updated", result)
        await context.emit("tool.application_completed", {"catalog_id": catalog_id})
        return result

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
        return [
            {
                "id": document.id,
                "title": document.title,
                "status": document.status,
                "page_count": document.page_count,
            }
            for document in self._documents.list_documents()
        ]

    def _read_document_chunks(
        self,
        arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> list[dict[str, Any]]:
        start = int(arguments.get("start") or 0)
        limit = int(arguments.get("limit") or 20)
        chunks = self._retrieval.fetch_document_chunks(str(arguments["document_id"]))
        return [
            {
                "chunk_id": chunk.id,
                "chunk_index": chunk.chunk_index,
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

    def _list_workspace(
        self,
        _arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> list[str]:
        return [document.path for document in self._workspace.list_files()]

    def _read_workspace(
        self,
        arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        document = self._workspace.read_file(str(arguments["path"]))
        return {
            "path": document.path,
            "media_type": document.media_type,
            "content": document.content,
        }

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
        }

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

    def _require_direct_agents(self) -> DirectAgentRepository:
        if self._direct_agents is None:
            raise RuntimeError("Direct research-agent tools are not configured.")
        return self._direct_agents
