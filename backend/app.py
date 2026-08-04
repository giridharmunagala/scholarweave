from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAIError
from sqlalchemy import delete, func, select, update

from backend.agent_runtime import provider_profile, verify_provider, verify_resolved_provider
from backend.config import Settings
from backend.custom_nodes import (
    CustomNodeProvider,
    execute_spec,
    revision_response,
    validate_config as validate_custom_config,
    validate_inputs as validate_custom_inputs,
    validate_spec as validate_custom_spec,
)
from backend.db import create_session_factory
from backend.documents import DocumentProcessingError, DocumentService
from backend.engine import WorkflowExecutor
from backend.export_python import export_workflow
from backend.events import EventBroker
from backend.models import (
    AppSetting,
    Artifact,
    CustomNodeDefinition,
    CustomNodeRevision,
    Document,
    DocumentChunk,
    NodeRun,
    ProviderProfile,
    Run,
    RunEvent,
    Workflow,
    WorkflowVersion,
)
from backend.nodes import BUILTIN_NODES
from backend.ollama import OllamaClient, OllamaError
from backend.provider_runtime import ModelRuntime, ProviderRuntimeError
from backend.registry import NodeRegistry, PortDefinition
from backend.retrieval import RetrievalService
from backend.schemas import (
    ArtifactContentResponse,
    ArtifactResponse,
    CustomNodeCreate,
    CustomNodeResponse,
    CustomNodeSampleRequest,
    CustomNodeSampleResponse,
    CustomNodeUpdate,
    DocumentChunkResponse,
    DocumentResponse,
    HealthResponse,
    ModelInfo,
    ModelReference,
    NodeRunResponse,
    ProviderCheckResponse,
    ProviderModelEntry,
    ProviderModelsResponse,
    ProviderProfileCreate,
    ProviderProfileResponse,
    ProviderProfileUpdate,
    ProviderVerifyRequest,
    PythonExportResponse,
    RunCreateRequest,
    RunEventResponse,
    RunResponse,
    SettingsResponse,
    SettingsUpdate,
    WorkflowCreateRequest,
    WorkflowDefinition,
    WorkflowResponse,
    WorkflowValidationResult,
    WorkflowVersionResponse,
    WorkspaceNoteContentResponse,
    WorkspaceNoteResponse,
)
from backend.storage import SafeStorage, StorageError
from backend.subworkflows import WorkflowNodeProvider
from backend.templates import STARTER_WORKFLOWS
from backend.utils import utcnow
from backend.workflows import WorkflowValidator


@dataclass(slots=True)
class BackendServices:
    settings: Settings
    session_factory: Any
    storage: SafeStorage
    registry: NodeRegistry
    ollama: OllamaClient
    retrieval: RetrievalService
    documents: DocumentService
    events: EventBroker
    executor: WorkflowExecutor
    model_runtime: ModelRuntime


PERSISTED_SETTING_KEYS = {
    "ollama_base_url",
    "default_generation_model",
    "default_embedding_model",
    "default_model_references",
    "request_timeout_seconds",
    "max_context_chars",
    "max_chunk_chars",
    "max_map_items",
    "max_repeat_iterations",
    "max_concurrent_nodes",
    "max_subworkflow_depth",
    "ocr_llm_enhancement_enabled",
    "ocr_llm_model",
    "ocr_llm_triage_model",
    "agent_provider",
    "openai_api_key",
    "openai_base_url",
    "agent_max_turns",
    "agent_tracing_enabled",
    "python_node_enabled",
    "python_node_timeout_seconds",
    "python_node_memory_mb",
    "python_node_allowed_imports",
}

#: Bumped when the node vocabulary changes so much that saved graphs cannot be read.
#: Graphs written against the retired hand-rolled LLM nodes are removed rather than
#: half-translated, so nobody opens a workflow that silently no longer means what it did.
SCHEMA_GENERATION = 2
_GENERATION_KEY = "schema_generation"
#: Node types the agent runtime replaced.
_RETIRED_NODE_TYPES = {"ollama_generate", "ollama_embed", "prompt_builder", "prompt_template"}


def create_backend_services(settings: Settings | None = None) -> BackendServices:
    resolved = settings or Settings()
    resolved.ensure_directories()
    session_factory = create_session_factory(resolved)
    _load_persisted_settings(session_factory, resolved)
    _seed_default_ollama_profile(session_factory, resolved)
    _recover_interrupted_runs(session_factory)
    storage = SafeStorage(resolved)
    registry = NodeRegistry()
    for node in BUILTIN_NODES:
        registry.register(node)
    registry.add_provider(CustomNodeProvider(session_factory))
    registry.add_provider(WorkflowNodeProvider(session_factory, WorkflowValidator(registry, resolved)))
    ollama = OllamaClient(resolved)
    model_runtime = ModelRuntime(session_factory, resolved, ollama)
    retrieval = RetrievalService(session_factory, resolved)
    documents = DocumentService(session_factory, resolved, storage, retrieval, ollama, model_runtime)
    events = EventBroker()
    services = BackendServices(
        settings=resolved,
        session_factory=session_factory,
        storage=storage,
        registry=registry,
        ollama=ollama,
        retrieval=retrieval,
        documents=documents,
        events=events,
        executor=None,  # type: ignore[arg-type]
        model_runtime=model_runtime,
    )
    services.executor = WorkflowExecutor(services, events)
    _retire_legacy_workflows(session_factory, resolved)
    _seed_templates(services)
    return services


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="ScholarWeave API", version="0.1.0")
    services = create_backend_services(settings)
    app.state.services = services
    validator = WorkflowValidator(services.registry, services.settings)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI includes the rejected input in its stock 422 body. That is helpful
        # for ordinary forms, but could reflect an API key supplied to a profile route.
        detail = [{key: value for key, value in error.items() if key != "input"} for error in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": detail})

    @app.get(f"{services.settings.api_prefix}/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            database_path=str(services.settings.database_path),
            data_dir=str(services.settings.data_dir),
            frontend_available=services.settings.frontend_dist_dir.exists(),
            ocr_available=services.documents.ocr_available(),
        )

    @app.get(f"{services.settings.api_prefix}/settings", response_model=SettingsResponse)
    async def get_settings() -> SettingsResponse:
        return _settings_response(services)

    @app.put(f"{services.settings.api_prefix}/settings", response_model=SettingsResponse)
    async def update_settings(update: SettingsUpdate) -> SettingsResponse:
        values = update.model_dump(exclude_none=True)
        if "default_model_references" in values:
            values["default_model_references"] = {
                capability: reference.model_dump(mode="json")
                for capability, reference in (update.default_model_references or {}).items()
            }
        for model_key in {
            "default_generation_model",
            "default_embedding_model",
            "ocr_llm_model",
            "ocr_llm_triage_model",
        }:
            if model_key in update.model_fields_set:
                values[model_key] = getattr(update, model_key)
        for key, value in values.items():
            if key in PERSISTED_SETTING_KEYS:
                with services.session_factory() as session:
                    if value is None:
                        existing = session.get(AppSetting, key)
                        if existing is not None:
                            session.delete(existing)
                    else:
                        session.merge(AppSetting(key=key, value_json=value))
                    session.commit()
                setattr(services.settings, key, value)
                if key == "ollama_base_url":
                    with services.session_factory() as session:
                        default_profile = session.scalar(
                            select(ProviderProfile).where(ProviderProfile.name == "Default Ollama")
                        )
                        if default_profile is not None:
                            default_profile.base_url = value.rstrip("/")
                            session.commit()
        return _settings_response(services)

    @app.get(f"{services.settings.api_prefix}/nodes", response_model=list)
    async def node_catalog() -> list[dict[str, Any]]:
        return [entry.model_dump(mode="json") for entry in services.registry.catalog()]

    @app.get(f"{services.settings.api_prefix}/custom-nodes", response_model=list[CustomNodeResponse])
    async def list_custom_nodes(include_archived: bool = False) -> list[CustomNodeResponse]:
        with services.session_factory() as session:
            statement = select(CustomNodeDefinition).order_by(CustomNodeDefinition.name)
            if not include_archived:
                statement = statement.where(CustomNodeDefinition.archived.is_(False))
            definitions = list(session.scalars(statement))
            return [_custom_node_response(session, definition) for definition in definitions]

    @app.post(f"{services.settings.api_prefix}/custom-nodes", response_model=CustomNodeResponse)
    async def create_custom_node(payload: CustomNodeCreate) -> CustomNodeResponse:
        try:
            schema = validate_custom_spec(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        with services.session_factory() as session:
            if session.scalar(select(CustomNodeDefinition).where(CustomNodeDefinition.name == payload.name)):
                raise HTTPException(status_code=409, detail="A custom node with this name already exists")
            definition = CustomNodeDefinition(name=payload.name)
            session.add(definition)
            session.flush()
            revision = _new_custom_revision(definition.id, 1, payload, schema)
            session.add(revision)
            session.commit()
            session.refresh(definition)
            return _custom_node_response(session, definition)

    @app.get(f"{services.settings.api_prefix}/custom-nodes/{{definition_id}}", response_model=CustomNodeResponse)
    async def get_custom_node(definition_id: str) -> CustomNodeResponse:
        with services.session_factory() as session:
            definition = session.get(CustomNodeDefinition, definition_id)
            if definition is None:
                raise HTTPException(status_code=404, detail="Custom node not found")
            return _custom_node_response(session, definition)

    @app.put(f"{services.settings.api_prefix}/custom-nodes/{{definition_id}}", response_model=CustomNodeResponse)
    async def update_custom_node(definition_id: str, payload: CustomNodeUpdate) -> CustomNodeResponse:
        try:
            schema = validate_custom_spec(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        with services.session_factory() as session:
            definition = session.get(CustomNodeDefinition, definition_id)
            if definition is None:
                raise HTTPException(status_code=404, detail="Custom node not found")
            if definition.archived:
                raise HTTPException(status_code=409, detail="Archived custom nodes cannot be updated")
            if payload.name and payload.name != definition.name:
                duplicate = session.scalar(
                    select(CustomNodeDefinition).where(
                        CustomNodeDefinition.name == payload.name, CustomNodeDefinition.id != definition_id
                    )
                )
                if duplicate:
                    raise HTTPException(status_code=409, detail="A custom node with this name already exists")
                definition.name = payload.name
            latest = session.scalar(
                select(CustomNodeRevision.revision)
                .where(CustomNodeRevision.definition_id == definition_id)
                .order_by(CustomNodeRevision.revision.desc())
                .limit(1)
            )
            session.add(_new_custom_revision(definition_id, (latest or 0) + 1, payload, schema))
            definition.updated_at = utcnow()
            session.commit()
            session.refresh(definition)
            return _custom_node_response(session, definition)

    @app.post(f"{services.settings.api_prefix}/custom-nodes/{{definition_id}}/archive", response_model=CustomNodeResponse)
    async def archive_custom_node(definition_id: str) -> CustomNodeResponse:
        with services.session_factory() as session:
            definition = session.get(CustomNodeDefinition, definition_id)
            if definition is None:
                raise HTTPException(status_code=404, detail="Custom node not found")
            if not definition.archived:
                definition.archived = True
                definition.archived_at = utcnow()
                session.commit()
                session.refresh(definition)
            return _custom_node_response(session, definition)

    @app.post(f"{services.settings.api_prefix}/custom-nodes/sample", response_model=CustomNodeSampleResponse)
    @app.post(f"{services.settings.api_prefix}/custom-nodes/test", response_model=CustomNodeSampleResponse)
    async def sample_custom_node(payload: CustomNodeSampleRequest) -> CustomNodeSampleResponse:
        assert payload.spec is not None
        try:
            schema = validate_custom_spec(payload.spec)
            validate_custom_inputs(
                payload.inputs,
                [PortDefinition(**port.model_dump()) for port in payload.spec.inputs],
            )
            validate_custom_config(payload.config, payload.spec.config_fields, schema)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            output, stdout = await execute_spec(
                payload.spec,
                inputs=payload.inputs,
                workflow_inputs=payload.workflow_inputs,
                config=payload.config,
                settings=services.settings,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return CustomNodeSampleResponse(output=output, stdout=stdout)

    @app.get(f"{services.settings.api_prefix}/models", response_model=list[ModelInfo])
    async def list_models() -> list[ModelInfo]:
        try:
            models = await services.ollama.list_models()
        except OllamaError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return [ModelInfo.model_validate(model) for model in models]

    @app.get(f"{services.settings.api_prefix}/providers", response_model=list[ProviderProfileResponse])
    async def list_provider_profiles(include_archived: bool = False) -> list[ProviderProfileResponse]:
        return [_provider_profile_response(profile) for profile in services.model_runtime.profiles(include_archived=include_archived)]

    @app.post(f"{services.settings.api_prefix}/providers", response_model=ProviderProfileResponse)
    async def create_provider_profile(payload: ProviderProfileCreate) -> ProviderProfileResponse:
        with services.session_factory() as session:
            existing = session.scalar(select(ProviderProfile).where(ProviderProfile.name == payload.name))
            if existing is not None:
                raise HTTPException(status_code=409, detail="A provider profile with this name already exists")
            profile = ProviderProfile(
                name=payload.name,
                kind=payload.kind,
                base_url=payload.base_url.rstrip("/"),
                api_version=payload.api_version,
                api_key=payload.api_key or None,
                models_json=[entry.model_dump(mode="json") for entry in payload.models],
                state="active",
            )
            session.add(profile)
            session.commit()
            session.refresh(profile)
            return _provider_profile_response(profile)

    @app.get(f"{services.settings.api_prefix}/providers/{{profile_id}}", response_model=ProviderProfileResponse)
    async def get_provider_profile(profile_id: str) -> ProviderProfileResponse:
        try:
            return _provider_profile_response(services.model_runtime.profile(profile_id, include_archived=True))
        except ProviderRuntimeError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.put(f"{services.settings.api_prefix}/providers/{{profile_id}}", response_model=ProviderProfileResponse)
    async def update_provider_profile(profile_id: str, payload: ProviderProfileUpdate) -> ProviderProfileResponse:
        with services.session_factory() as session:
            profile = session.get(ProviderProfile, profile_id)
            if profile is None:
                raise HTTPException(status_code=404, detail="Provider profile not found")
            values = payload.model_dump(exclude_unset=True)
            if "name" in values:
                duplicate = session.scalar(
                    select(ProviderProfile).where(ProviderProfile.name == values["name"], ProviderProfile.id != profile_id)
                )
                if duplicate is not None:
                    raise HTTPException(status_code=409, detail="A provider profile with this name already exists")
            for key in ("name", "kind", "api_version"):
                if key in values:
                    setattr(profile, key, values[key])
            if "base_url" in values:
                profile.base_url = values["base_url"].rstrip("/")
            if "api_key" in values:
                profile.api_key = values["api_key"] or None
            if "models" in values:
                profile.models_json = [entry.model_dump(mode="json") for entry in payload.models or []]
            session.commit()
            session.refresh(profile)
            return _provider_profile_response(profile)

    @app.delete(f"{services.settings.api_prefix}/providers/{{profile_id}}", response_model=ProviderProfileResponse)
    async def archive_provider_profile(profile_id: str) -> ProviderProfileResponse:
        with services.session_factory() as session:
            profile = session.get(ProviderProfile, profile_id)
            if profile is None:
                raise HTTPException(status_code=404, detail="Provider profile not found")
            profile.state = "archived"
            session.commit()
            session.refresh(profile)
            return _provider_profile_response(profile)

    @app.get(f"{services.settings.api_prefix}/providers/{{profile_id}}/models", response_model=ProviderModelsResponse)
    async def discover_provider_models(profile_id: str) -> ProviderModelsResponse:
        try:
            profile = services.model_runtime.profile(profile_id)
        except ProviderRuntimeError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        try:
            models = await services.model_runtime.discover(profile)
            return ProviderModelsResponse(models=models)
        except Exception as exc:  # discovery is intentionally best effort
            manual = [ProviderModelEntry.model_validate(item) for item in (profile.models_json or [])]
            return ProviderModelsResponse(models=manual, discovery_error=f"{type(exc).__name__}: {exc}")

    @app.post(f"{services.settings.api_prefix}/providers/{{profile_id}}/verify", response_model=ProviderCheckResponse)
    async def verify_provider_profile(profile_id: str, request: ProviderVerifyRequest) -> ProviderCheckResponse:
        try:
            profile = services.model_runtime.profile(profile_id)
        except ProviderRuntimeError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        model = request.model
        if not model:
            manual = [ProviderModelEntry.model_validate(item) for item in (profile.models_json or [])]
            target = next((entry for entry in manual if "chat" in entry.capabilities or "tools" in entry.capabilities), None)
            model = target.name if target else None
        entries = [ProviderModelEntry.model_validate(item) for item in (profile.models_json or [])]
        entry = next((candidate for candidate in entries if candidate.name == model), None)
        capabilities = entry.capabilities if entry else set()
        capability = (
            "chat"
            if not capabilities or capabilities & {"chat", "tools"}
            else "embedding"
            if "embedding" in capabilities
            else "vision"
        )
        try:
            resolved = services.model_runtime.resolve(
                capability,
                node_reference=ModelReference(provider_profile_id=profile_id, model=model),
            )
        except (ProviderRuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if capability == "embedding":
            try:
                embeddings = await services.model_runtime.embed(resolved, "Provider connection check")
            except (ProviderRuntimeError, OllamaError, OpenAIError) as exc:
                return ProviderCheckResponse(
                    provider=resolved.kind,
                    base_url=resolved.base_url,
                    model=resolved.model,
                    reachable=False,
                    tool_calling=False,
                    detail=f"{type(exc).__name__}: {exc}",
                )
            reachable = bool(embeddings and embeddings[0])
            return ProviderCheckResponse(
                provider=resolved.kind,
                base_url=resolved.base_url,
                model=resolved.model,
                reachable=reachable,
                tool_calling=False,
                detail="Provider returned an embedding vector." if reachable else "Provider returned an empty embedding vector.",
            )
        if capability == "vision":
            try:
                await services.model_runtime.generate(
                    resolved,
                    "Describe the single pixel in one short sentence.",
                    images=[
                        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
                    ],
                )
            except (ProviderRuntimeError, OllamaError, OpenAIError) as exc:
                return ProviderCheckResponse(
                    provider=resolved.kind,
                    base_url=resolved.base_url,
                    model=resolved.model,
                    reachable=False,
                    tool_calling=False,
                    detail=f"{type(exc).__name__}: {exc}",
                )
            return ProviderCheckResponse(
                provider=resolved.kind,
                base_url=resolved.base_url,
                model=resolved.model,
                reachable=True,
                tool_calling=False,
                detail="Provider accepted an image prompt.",
            )
        report = await verify_resolved_provider(services.settings, services.model_runtime, resolved)
        return ProviderCheckResponse(**report)

    @app.get(f"{services.settings.api_prefix}/notes", response_model=list[WorkspaceNoteResponse])
    async def list_notes() -> list[WorkspaceNoteResponse]:
        return [
            WorkspaceNoteResponse(
                path=note.relative_path,
                name=Path(note.relative_path).stem,
                size_bytes=note.size_bytes,
                modified_at=note.modified_at,
            )
            for note in services.storage.list_workspace_markdown()
        ]

    @app.get(f"{services.settings.api_prefix}/notes/content", response_model=WorkspaceNoteContentResponse)
    async def get_note_content(path: str) -> WorkspaceNoteContentResponse:
        try:
            note, content = services.storage.read_workspace_markdown(path)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Note not found") from exc
        except (StorageError, UnicodeDecodeError, IsADirectoryError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return WorkspaceNoteContentResponse(
            path=note.relative_path,
            name=Path(note.relative_path).stem,
            size_bytes=note.size_bytes,
            modified_at=note.modified_at,
            content=content,
        )

    @app.delete(f"{services.settings.api_prefix}/notes", status_code=204)
    async def delete_note(path: str) -> Response:
        try:
            services.storage.delete_workspace_markdown(path)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Note not found") from exc
        except (StorageError, IsADirectoryError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return Response(status_code=204)

    @app.get(f"{services.settings.api_prefix}/documents", response_model=list[DocumentResponse])
    async def list_documents() -> list[DocumentResponse]:
        return [_document_response(services, document.id) for document in services.documents.list_documents()]

    @app.post(f"{services.settings.api_prefix}/documents", response_model=DocumentResponse)
    async def upload_document(file: UploadFile = File(...), title: str | None = Form(default=None)) -> DocumentResponse:
        if file.content_type not in {"application/pdf", "application/octet-stream"}:
            raise HTTPException(status_code=400, detail="Only PDF uploads are supported")
        try:
            document = await services.documents.create_document_from_upload(file, title=title)
        except StorageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _document_response(services, document.id)

    @app.get(f"{services.settings.api_prefix}/documents/{{document_id}}", response_model=DocumentResponse)
    async def get_document(document_id: str) -> DocumentResponse:
        try:
            return _document_response(services, document_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.delete(f"{services.settings.api_prefix}/documents/{{document_id}}", status_code=204)
    async def delete_document(document_id: str) -> Response:
        if not services.documents.delete_document(document_id):
            raise HTTPException(status_code=404, detail="Document not found")
        return Response(status_code=204)

    @app.post(f"{services.settings.api_prefix}/documents/{{document_id}}/ingest", response_model=DocumentResponse)
    async def ingest_document(document_id: str) -> DocumentResponse:
        try:
            await services.documents.ingest_document(document_id)
        except DocumentProcessingError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _document_response(services, document_id)

    @app.get(f"{services.settings.api_prefix}/artifacts/{{artifact_id}}", response_model=ArtifactResponse)
    async def get_artifact(artifact_id: str) -> ArtifactResponse:
        artifact = services.documents.get_artifact(artifact_id)
        if not artifact:
            raise HTTPException(status_code=404, detail="Artifact not found")
        return _artifact_response(artifact)

    @app.get(f"{services.settings.api_prefix}/artifacts/{{artifact_id}}/content", response_model=ArtifactContentResponse)
    async def get_artifact_content(artifact_id: str) -> ArtifactContentResponse:
        artifact = services.documents.get_artifact(artifact_id)
        if not artifact:
            raise HTTPException(status_code=404, detail="Artifact not found")
        return ArtifactContentResponse(artifact=_artifact_response(artifact), content=services.documents.artifact_content(artifact))

    @app.get(f"{services.settings.api_prefix}/artifacts/{{artifact_id}}/raw")
    async def get_artifact_raw(artifact_id: str) -> Response:
        artifact = services.documents.get_artifact(artifact_id)
        if not artifact:
            raise HTTPException(status_code=404, detail="Artifact not found")
        return Response(
            content=services.documents.artifact_bytes(artifact),
            media_type=artifact.media_type,
            headers={"Content-Disposition": f'inline; filename="{Path(artifact.relative_path).name}"'},
        )

    @app.get(f"{services.settings.api_prefix}/workflows", response_model=list[WorkflowResponse])
    async def list_workflows() -> list[WorkflowResponse]:
        with services.session_factory() as session:
            workflows = list(session.scalars(select(Workflow).order_by(Workflow.updated_at.desc())))
        return [_workflow_response(services, workflow.id) for workflow in workflows]

    @app.get(f"{services.settings.api_prefix}/workflows/templates", response_model=list[WorkflowDefinition])
    async def workflow_templates() -> list[WorkflowDefinition]:
        return STARTER_WORKFLOWS

    @app.post(f"{services.settings.api_prefix}/workflows/validate", response_model=WorkflowValidationResult)
    async def validate_workflow(definition: WorkflowDefinition) -> WorkflowValidationResult:
        return validator.validate(definition)

    @app.post(f"{services.settings.api_prefix}/workflows/export/python", response_model=PythonExportResponse)
    async def export_workflow_python(definition: WorkflowDefinition) -> PythonExportResponse:
        """Renders the graph on the canvas as a standalone Agents SDK script."""
        profile = provider_profile(services.settings)
        source = export_workflow(
            definition,
            services.registry,
            base_url=profile.base_url,
            model=profile.default_model or "gemma4:e2b",
        )
        slug = re.sub(r"[^a-z0-9]+", "_", definition.name.lower()).strip("_") or "workflow"
        return PythonExportResponse(filename=f"{slug}.py", source=source)

    @app.post(f"{services.settings.api_prefix}/agents/verify", response_model=ProviderCheckResponse)
    async def verify_agent_provider() -> ProviderCheckResponse:
        """Verifies the effective application-default chat model."""
        try:
            resolved = services.model_runtime.resolve("chat")
            report = await verify_resolved_provider(services.settings, services.model_runtime, resolved)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user as a failed check
            legacy = provider_profile(services.settings)
            return ProviderCheckResponse(
                provider=legacy.name,
                base_url=legacy.base_url,
                model=services.settings.default_generation_model,
                reachable=False,
                tool_calling=False,
                detail=f"{type(exc).__name__}: {exc}",
            )
        return ProviderCheckResponse(
            provider=report.get("provider", "effective-default"),
            base_url=report.get("base_url", ""),
            model=report.get("model"),
            reachable=bool(report.get("reachable")),
            tool_calling=bool(report.get("tool_calling")),
            detail=report.get("detail"),
        )

    @app.post(f"{services.settings.api_prefix}/workflows", response_model=WorkflowResponse)
    async def create_workflow(request: WorkflowCreateRequest) -> WorkflowResponse:
        validation = validator.validate(request.definition)
        if not validation.valid:
            raise HTTPException(status_code=400, detail=validation.errors)
        with services.session_factory() as session:
            workflow = session.scalar(select(Workflow).where(Workflow.name == request.name))
            if workflow is None:
                workflow = Workflow(name=request.name, description=request.description, is_template=request.is_template)
                session.add(workflow)
                session.flush()
                next_version = 1
            else:
                workflow.description = request.description
                workflow.is_template = request.is_template
                next_version = (session.scalar(select(func.max(WorkflowVersion.version)).where(WorkflowVersion.workflow_id == workflow.id)) or 0) + 1
            version = WorkflowVersion(workflow_id=workflow.id, version=next_version, definition_json=request.definition.model_dump(mode="json"))
            session.add(version)
            session.commit()
            session.refresh(workflow)
        return _workflow_response(services, workflow.id)

    @app.get(f"{services.settings.api_prefix}/workflows/{{workflow_id}}", response_model=WorkflowResponse)
    async def get_workflow(workflow_id: str) -> WorkflowResponse:
        try:
            return _workflow_response(services, workflow_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.delete(f"{services.settings.api_prefix}/workflows/{{workflow_id}}", status_code=204)
    async def delete_workflow(workflow_id: str) -> Response:
        with services.session_factory() as session:
            workflow = session.get(Workflow, workflow_id)
            if workflow is None:
                raise HTTPException(status_code=404, detail="Workflow not found")
            version_ids = list(
                session.scalars(
                    select(WorkflowVersion.id).where(WorkflowVersion.workflow_id == workflow_id)
                )
            )
            if version_ids:
                session.execute(
                    update(Run)
                    .where(Run.workflow_version_id.in_(version_ids))
                    .values(workflow_version_id=None)
                )
            session.execute(delete(WorkflowVersion).where(WorkflowVersion.workflow_id == workflow_id))
            session.delete(workflow)
            session.commit()
        return Response(status_code=204)

    @app.get(f"{services.settings.api_prefix}/workflows/{{workflow_id}}/versions", response_model=list[WorkflowVersionResponse])
    async def list_workflow_versions(workflow_id: str) -> list[WorkflowVersionResponse]:
        with services.session_factory() as session:
            versions = list(session.scalars(select(WorkflowVersion).where(WorkflowVersion.workflow_id == workflow_id).order_by(WorkflowVersion.version.desc())))
        if not versions:
            raise HTTPException(status_code=404, detail="Workflow not found")
        return [_workflow_version_response(version) for version in versions]

    @app.get(f"{services.settings.api_prefix}/runs", response_model=list[RunResponse])
    async def list_runs() -> list[RunResponse]:
        return [_run_response(services, run.id) for run in services.executor.list_runs()]

    @app.post(f"{services.settings.api_prefix}/runs", response_model=RunResponse)
    async def start_run(request: RunCreateRequest) -> RunResponse:
        workflow_version_id = request.workflow_version_id
        if workflow_version_id:
            with services.session_factory() as session:
                version = session.get(WorkflowVersion, workflow_version_id)
                if not version:
                    raise HTTPException(status_code=404, detail="Workflow version not found")
                workflow = WorkflowDefinition.model_validate(version.definition_json)
        else:
            workflow = request.workflow
            assert workflow is not None
        validation = validator.validate(workflow)
        if not validation.valid:
            raise HTTPException(status_code=400, detail=validation.errors)
        run = await services.executor.start_run(workflow, request.inputs, workflow_version_id=workflow_version_id)
        return _run_response(services, run.id)

    @app.get(f"{services.settings.api_prefix}/runs/{{run_id}}", response_model=RunResponse)
    async def get_run(run_id: str) -> RunResponse:
        try:
            return _run_response(services, run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.delete(f"{services.settings.api_prefix}/runs/{{run_id}}", status_code=204)
    async def delete_run(run_id: str) -> Response:
        with services.session_factory() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise HTTPException(status_code=404, detail="Run not found")
            if run.status in {"pending", "running"}:
                raise HTTPException(status_code=409, detail="Cancel the active run before deleting it")
        services.documents.delete_run_artifact_files(run_id)
        with services.session_factory() as session:
            session.execute(delete(Artifact).where(Artifact.run_id == run_id))
            session.execute(delete(RunEvent).where(RunEvent.run_id == run_id))
            session.execute(delete(NodeRun).where(NodeRun.run_id == run_id))
            run = session.get(Run, run_id)
            if run is not None:
                session.delete(run)
            session.commit()
        return Response(status_code=204)

    @app.post(f"{services.settings.api_prefix}/runs/{{run_id}}/cancel", response_model=RunResponse)
    async def cancel_run(run_id: str) -> RunResponse:
        try:
            await services.executor.cancel_run(run_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _run_response(services, run_id)

    @app.get(f"{services.settings.api_prefix}/runs/{{run_id}}/events")
    async def stream_run_events(run_id: str, request: Request, after: int | None = None) -> StreamingResponse:
        if not services.executor.load_run(run_id):
            raise HTTPException(status_code=404, detail="Run not found")

        async def event_stream() -> Any:
            for event in services.executor.load_events(run_id, after_id=after):
                payload = RunEventResponse(id=event.id, run_id=event.run_id, event_type=event.event_type, payload=event.payload_json, created_at=event.created_at)
                yield _format_sse(payload.model_dump(mode="json"))
            async with services.events.subscribe(run_id) as queue:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    yield _format_sse(event)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    if services.settings.frontend_dist_dir.exists():
        app.mount("/assets", StaticFiles(directory=services.settings.frontend_dist_dir / "assets"), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def serve_frontend(full_path: str) -> Any:
            candidate = services.settings.frontend_dist_dir / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            index = services.settings.frontend_dist_dir / "index.html"
            if index.exists():
                return FileResponse(index)
            raise HTTPException(status_code=404, detail="Frontend build not found")

    return app


def _retire_legacy_workflows(session_factory: Any, settings: Settings) -> None:
    """Clears out graphs written against the retired hand-rolled LLM nodes.

    Runs once. Translating those graphs would produce workflows that look familiar but
    behave differently, so they are deleted instead — the starter templates are reseeded
    on the agent primitives immediately afterwards.
    """
    with session_factory() as session:
        marker = session.get(AppSetting, _GENERATION_KEY)
        if marker is not None and marker.value_json == SCHEMA_GENERATION:
            return
        stale_ids = {
            version.workflow_id
            for version in session.scalars(select(WorkflowVersion))
            if any(
                node.get("type") in _RETIRED_NODE_TYPES
                for node in (version.definition_json or {}).get("nodes", [])
            )
        }
        if stale_ids:
            version_ids = [
                version.id
                for version in session.scalars(
                    select(WorkflowVersion).where(WorkflowVersion.workflow_id.in_(stale_ids))
                )
            ]
            run_ids = [
                run.id
                for run in session.scalars(select(Run).where(Run.workflow_version_id.in_(version_ids)))
            ]
            if run_ids:
                session.execute(delete(RunEvent).where(RunEvent.run_id.in_(run_ids)))
                session.execute(delete(NodeRun).where(NodeRun.run_id.in_(run_ids)))
                session.execute(delete(Run).where(Run.id.in_(run_ids)))
            session.execute(delete(WorkflowVersion).where(WorkflowVersion.workflow_id.in_(stale_ids)))
            session.execute(delete(Workflow).where(Workflow.id.in_(stale_ids)))
        # A saved map limit from the old engine is what stopped long papers summarising
        # at all. Lift it to the new default rather than leaving the old ceiling in place.
        stored_limit = session.get(AppSetting, "max_map_items")
        default_limit = Settings.model_fields["max_map_items"].default
        if stored_limit is not None and isinstance(stored_limit.value_json, int) and stored_limit.value_json < default_limit:
            stored_limit.value_json = default_limit
            settings.max_map_items = default_limit
        if marker is None:
            session.add(AppSetting(key=_GENERATION_KEY, value_json=SCHEMA_GENERATION))
        else:
            marker.value_json = SCHEMA_GENERATION
        session.commit()


def _seed_templates(services: BackendServices) -> None:
    with services.session_factory() as session:
        for definition in STARTER_WORKFLOWS:
            payload = definition.model_dump(mode="json")
            workflow = session.scalar(select(Workflow).where(Workflow.name == definition.name))
            if workflow is None:
                workflow = Workflow(name=definition.name, description=definition.description, is_template=True)
                session.add(workflow)
                session.flush()
                session.add(WorkflowVersion(workflow_id=workflow.id, version=1, definition_json=payload))
                continue
            versions = list(
                session.scalars(select(WorkflowVersion).where(WorkflowVersion.workflow_id == workflow.id))
            )
            # Refresh starter templates that ship an updated definition, but only while
            # they are untouched: a second version means the user has edited this graph.
            if workflow.is_template and len(versions) == 1 and versions[0].definition_json != payload:
                versions[0].definition_json = payload
                workflow.description = definition.description
        session.commit()


def _load_persisted_settings(session_factory: Any, settings: Settings) -> None:
    with session_factory() as session:
        persisted = session.scalars(select(AppSetting).where(AppSetting.key.in_(PERSISTED_SETTING_KEYS)))
        for setting in persisted:
            setattr(settings, setting.key, setting.value_json)


def _seed_default_ollama_profile(session_factory: Any, settings: Settings) -> None:
    """Migrates the original single-Ollama settings into one explicit profile once."""
    with session_factory() as session:
        profile = session.scalar(select(ProviderProfile).where(ProviderProfile.name == "Default Ollama"))
        if profile is None:
            model_capabilities: dict[str, set[str]] = {}

            def add_model(name: str | None, *capabilities: str) -> None:
                if name:
                    model_capabilities.setdefault(name, set()).update(capabilities)

            add_model(settings.default_generation_model, "chat", "tools")
            add_model(settings.default_embedding_model, "embedding")
            vision_model = settings.ocr_llm_model or (
                settings.default_generation_model if settings.ocr_llm_enhancement_enabled else None
            )
            add_model(vision_model, "vision")
            models = [
                {"name": name, "capabilities": sorted(capabilities)}
                for name, capabilities in model_capabilities.items()
            ]
            profile = ProviderProfile(
                name="Default Ollama",
                kind="ollama",
                base_url=settings.ollama_base_url.rstrip("/"),
                models_json=models,
                state="active",
            )
            session.add(profile)
            session.flush()
        default_profile = profile
        if settings.agent_provider == "openai":
            cloud = session.scalar(select(ProviderProfile).where(ProviderProfile.name == "Default OpenAI"))
            if cloud is None:
                cloud = ProviderProfile(
                    name="Default OpenAI",
                    kind="openai",
                    base_url=(settings.openai_base_url or "https://api.openai.com/v1").rstrip("/"),
                    api_key=settings.openai_api_key,
                    state="active",
                )
                session.add(cloud)
                session.flush()
            default_profile = cloud
        setting = session.get(AppSetting, "default_model_references")
        if setting is None:
            references: dict[str, dict[str, str]] = {}
            if settings.default_generation_model:
                references["chat"] = {"provider_profile_id": default_profile.id, "model": settings.default_generation_model}
                references["tools"] = {"provider_profile_id": default_profile.id, "model": settings.default_generation_model}
            if settings.default_embedding_model:
                references["embedding"] = {"provider_profile_id": default_profile.id, "model": settings.default_embedding_model}
            vision_model = settings.ocr_llm_model or (
                settings.default_generation_model if settings.ocr_llm_enhancement_enabled else None
            )
            if vision_model:
                references["vision"] = {"provider_profile_id": profile.id, "model": vision_model}
            settings.default_model_references = references
            session.add(AppSetting(key="default_model_references", value_json=references))
        session.commit()


def _provider_profile_response(profile: ProviderProfile) -> ProviderProfileResponse:
    return ProviderProfileResponse(
        id=profile.id,
        name=profile.name,
        kind=profile.kind,
        base_url=profile.base_url,
        api_version=profile.api_version,
        api_key_set=bool(profile.api_key),
        state=profile.state,
        models=[ProviderModelEntry.model_validate(item) for item in (profile.models_json or [])],
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


def _new_custom_revision(
    definition_id: str, revision_number: int, payload: Any, config_schema: dict[str, Any]
) -> CustomNodeRevision:
    """Copies the complete authoring spec into an immutable revision row."""
    return CustomNodeRevision(
        definition_id=definition_id,
        revision=revision_number,
        label=payload.label,
        description=payload.description,
        category=payload.category,
        tags_json=list(payload.tags),
        inputs_json=[port.model_dump(mode="json") for port in payload.inputs],
        outputs_json=[port.model_dump(mode="json") for port in payload.outputs],
        config_fields_json=[field.model_dump(mode="json") for field in payload.config_fields],
        config_schema_json=config_schema,
        code=payload.code,
    )


def _custom_node_response(session: Any, definition: CustomNodeDefinition) -> CustomNodeResponse:
    revisions = list(
        session.scalars(
            select(CustomNodeRevision)
            .where(CustomNodeRevision.definition_id == definition.id)
            .order_by(CustomNodeRevision.revision.desc())
        )
    )
    if not revisions:
        raise KeyError("Custom node has no revisions")
    return CustomNodeResponse(
        id=definition.id,
        name=definition.name,
        archived=definition.archived,
        archived_at=definition.archived_at,
        created_at=definition.created_at,
        updated_at=definition.updated_at,
        latest_revision=revision_response(revisions[0]),
        revisions=[revision_response(revision) for revision in revisions],
    )


def _process_is_alive(pid: int | None) -> bool:
    if not pid or pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Owned by another user, so it exists and is certainly not ours to clean up.
        return True
    return True


def _recover_interrupted_runs(session_factory: Any) -> None:
    """Fails runs left behind by a crash, without touching runs another process is still doing.

    A dev server started with ``--reload`` rebuilds the app on every file save, and a
    second instance may be running alongside this one. Agent runs take minutes, so
    blindly failing everything in flight would destroy live work.
    """
    interrupted_message = (
        "Run was interrupted by an application restart. Start it again to continue."
    )
    with session_factory() as session:
        stranded = [
            run
            for run in session.scalars(select(Run).where(Run.status.in_({"pending", "running"})))
            if not _process_is_alive(run.owner_pid)
        ]
        if not stranded:
            return
        run_ids = [run.id for run in stranded]
        session.execute(
            update(Run)
            .where(Run.id.in_(run_ids))
            .values(
                status="failed",
                error=interrupted_message,
                finished_at=utcnow(),
            )
        )
        session.execute(
            update(NodeRun)
            .where(NodeRun.status.in_({"pending", "running"}), NodeRun.run_id.in_(run_ids))
            .values(
                status="failed",
                error=interrupted_message,
                finished_at=utcnow(),
            )
        )
        session.commit()


def _settings_response(services: BackendServices) -> SettingsResponse:
    settings = services.settings
    return SettingsResponse(
        ollama_base_url=settings.ollama_base_url,
        default_generation_model=settings.default_generation_model,
        default_embedding_model=settings.default_embedding_model,
        default_model_references={
            capability: ModelReference.model_validate(reference)
            for capability, reference in settings.default_model_references.items()
        },
        data_dir=str(settings.data_dir),
        workspace_dir=str(settings.workspace_dir),
        artifacts_dir=str(settings.artifacts_dir),
        documents_dir=str(settings.documents_dir),
        database_path=str(settings.database_path),
        request_timeout_seconds=settings.request_timeout_seconds,
        max_upload_bytes=settings.max_upload_bytes,
        max_workspace_file_bytes=settings.max_workspace_file_bytes,
        max_artifact_bytes=settings.max_artifact_bytes,
        max_context_chars=settings.max_context_chars,
        max_chunk_chars=settings.max_chunk_chars,
        max_chunks_per_document=settings.max_chunks_per_document,
        max_map_items=settings.max_map_items,
        max_repeat_iterations=settings.max_repeat_iterations,
        max_concurrent_nodes=settings.max_concurrent_nodes,
        max_subworkflow_depth=settings.max_subworkflow_depth,
        pdf_min_text_chars=settings.pdf_min_text_chars,
        ocr_language=settings.ocr_language,
        ocr_llm_enhancement_enabled=settings.ocr_llm_enhancement_enabled,
        agent_provider=settings.agent_provider,
        openai_api_key_set=bool(settings.openai_api_key),
        openai_base_url=settings.openai_base_url,
        agent_max_turns=settings.agent_max_turns,
        agent_tracing_enabled=settings.agent_tracing_enabled,
        python_node_enabled=settings.python_node_enabled,
        python_node_timeout_seconds=settings.python_node_timeout_seconds,
        python_node_memory_mb=settings.python_node_memory_mb,
        python_node_allowed_imports=list(settings.python_node_allowed_imports),
        ocr_llm_model=settings.ocr_llm_model,
        ocr_llm_triage_model=settings.ocr_llm_triage_model,
    )


def _artifact_response(artifact: Artifact) -> ArtifactResponse:
    return ArtifactResponse(
        id=artifact.id,
        document_id=artifact.document_id,
        run_id=artifact.run_id,
        owner_type=artifact.owner_type,
        kind=artifact.kind,
        relative_path=artifact.relative_path,
        media_type=artifact.media_type,
        size_bytes=artifact.size_bytes,
        sha256=artifact.sha256,
        metadata=artifact.metadata_json or {},
        created_at=artifact.created_at,
    )


def _document_response(services: BackendServices, document_id: str) -> DocumentResponse:
    details = services.documents.get_document_details(document_id)
    if not details:
        raise KeyError("Document not found")
    document, artifacts, chunks = details
    return DocumentResponse(
        id=document.id,
        title=document.title,
        source_filename=document.source_filename,
        content_type=document.content_type,
        status=document.status,
        page_count=document.page_count,
        metadata=document.metadata_json or {},
        artifacts=[_artifact_response(artifact) for artifact in artifacts],
        chunks=[
            DocumentChunkResponse(
                id=chunk.id,
                chunk_index=chunk.chunk_index,
                section_title=chunk.section_title,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                citation=chunk.citation,
                text=chunk.text,
                metadata=chunk.metadata_json or {},
            )
            for chunk in chunks
        ],
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


def _workflow_version_response(version: WorkflowVersion) -> WorkflowVersionResponse:
    return WorkflowVersionResponse(
        id=version.id,
        workflow_id=version.workflow_id,
        version=version.version,
        definition=WorkflowDefinition.model_validate(version.definition_json),
        created_at=version.created_at,
    )


def _workflow_response(services: BackendServices, workflow_id: str) -> WorkflowResponse:
    with services.session_factory() as session:
        workflow = session.get(Workflow, workflow_id)
        if not workflow:
            raise KeyError("Workflow not found")
        version = session.scalar(
            select(WorkflowVersion).where(WorkflowVersion.workflow_id == workflow_id).order_by(WorkflowVersion.version.desc()).limit(1)
        )
    return WorkflowResponse(
        id=workflow.id,
        name=workflow.name,
        description=workflow.description,
        is_template=workflow.is_template,
        created_at=workflow.created_at,
        updated_at=workflow.updated_at,
        latest_version=_workflow_version_response(version) if version else None,
    )


def _run_response(services: BackendServices, run_id: str) -> RunResponse:
    run = services.executor.load_run(run_id)
    if not run:
        raise KeyError("Run not found")
    node_runs = services.executor.load_node_runs(run_id)
    return RunResponse(
        id=run.id,
        workflow_version_id=run.workflow_version_id,
        workflow_name=run.workflow_name,
        status=run.status,
        input=run.input_json or {},
        output=run.output_json,
        error=run.error,
        cancel_requested=run.cancel_requested,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        total_nodes=len((run.workflow_json or {}).get("nodes", [])),
        node_runs=[
            NodeRunResponse(
                id=node_run.id,
                run_id=node_run.run_id,
                node_path=node_run.node_path,
                node_id=node_run.node_id,
                node_type=node_run.node_type,
                status=node_run.status,
                input=node_run.input_json or {},
                output=node_run.output_json,
                error=node_run.error,
                started_at=node_run.started_at,
                finished_at=node_run.finished_at,
            )
            for node_run in node_runs
        ],
    )


def _format_sse(payload: dict[str, Any]) -> str:
    import json

    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def __getattr__(name: str) -> Any:
    """Builds the ASGI app only when something actually asks for it.

    ``uvicorn backend.app:app`` still works, but merely importing this module — which
    the test suite and the helper scripts do — no longer opens the real database and
    marks the user's in-flight runs as interrupted.
    """
    if name == "app":
        instance = create_app()
        globals()["app"] = instance
        return instance
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
