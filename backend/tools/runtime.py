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
    ) -> None:
        self._settings = settings
        self._documents = documents
        self._retrieval = retrieval
        self._storage = storage
        self._workspace = workspace
        self._agents = agent_service
        self._function_tools = function_tool_service
        self._catalog = tool_catalog

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
