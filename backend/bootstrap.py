from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from backend.agents.compiler import AgentCompiler
from backend.conversations.turns import (
    ConversationTurnService,
    PAPER_WORK_COMPLETION_POLICY_ID,
    validate_paper_work_completion,
)
from backend.core.config import RUNTIME_VERSION, Settings
from backend.conversations.repository import ConversationRepository
from backend.conversations.service import ConversationService
from backend.core.settings_service import SettingsService
from backend.documents import DocumentService
from backend.documents.formatting import DocumentFormatter
from backend.documents.ingestion import DocumentIngestion
from backend.documents.ocr import DocumentOCR
from backend.documents.repository import DocumentRepository
from backend.documents.vision import VisionEnhancer
from backend.runs.events import EventBroker
from backend.providers.ollama import OllamaClient
from backend.persistence import create_session_factory
from backend.providers.runtime import ModelRuntime
from backend.providers.repository import ProviderRepository
from backend.providers.binding import ProfileModelResolver, ProviderClientPool
from backend.providers.service import ProviderService
from backend.prompting import PromptRegistry
from backend.research import ResearchSearchService, SourceDownloadService
from backend.documents.retrieval import RetrievalService
from backend.runs.repository import RunRepository
from backend.runs.service import RunService
from backend.providers.inference import InferenceScheduler
from backend.conversations.sessions import ConversationSessionFactory
from backend.persistence.files import SafeStorage
from backend.tools.catalog import create_tool_catalog
from backend.tools.runtime import ApplicationToolRuntime
from backend.workspace.service import WorkspaceService
from backend.workspace.repository import WorkspaceRepository
from backend.documents.summaries import (
    PAPER_SUMMARY_COMPLETION_POLICY_ID,
    PaperSummaryService,
    validate_paper_summary_completion,
)


@dataclass(slots=True)
class ApplicationServices:
    settings: Settings
    session_factory: sessionmaker[Session]
    settings_service: SettingsService
    storage: SafeStorage
    ollama: OllamaClient
    model_runtime: ModelRuntime
    provider_clients: ProviderClientPool
    model_resolver: ProfileModelResolver
    providers: ProviderService
    retrieval: RetrievalService
    research_search: ResearchSearchService
    source_downloads: SourceDownloadService
    document_repository: DocumentRepository
    document_ocr: DocumentOCR
    document_vision: VisionEnhancer
    document_formatter: DocumentFormatter
    document_ingestion: DocumentIngestion
    documents: DocumentService
    workspace: WorkspaceService
    prompts: PromptRegistry
    tool_catalog: Any
    compiler: AgentCompiler
    conversation_sessions: ConversationSessionFactory
    conversations: ConversationService
    events: EventBroker
    inference_scheduler: InferenceScheduler
    runs: RunService
    conversation_turns: ConversationTurnService
    summaries: PaperSummaryService
    runtime_version: str = RUNTIME_VERSION

    async def start(self) -> None:
        for document in self.documents.list_documents():
            self.workspace.ensure_paper_folder(document.id, document.title)
        await self.runs.recover_incomplete(self.compiler)

    async def close(self) -> None:
        await self.documents.close()
        await self.runs.close()
        await self.research_search.close()
        await self.source_downloads.close()
        await self.conversation_sessions.close()
        await self.provider_clients.close()
        engine = self.session_factory.kw.get("bind")
        if engine is not None:
            engine.dispose()


def create_services(settings: Settings | None = None) -> ApplicationServices:
    resolved = settings or Settings()
    resolved.ensure_directories()
    session_factory = create_session_factory(resolved)
    settings_service = SettingsService(resolved, session_factory)
    settings_service.load()

    provider_repository = ProviderRepository(session_factory)
    default_ollama = provider_repository.ensure_default_ollama(base_url=resolved.ollama_base_url)
    inference_scheduler = InferenceScheduler()
    ollama = OllamaClient(
        resolved, inference_scheduler=inference_scheduler, profile_id=default_ollama.id,
    )
    model_runtime = ModelRuntime(
        session_factory,
        resolved,
        ollama,
        inference_scheduler,
    )
    provider_clients = ProviderClientPool(model_runtime)
    model_resolver = ProfileModelResolver(model_runtime, provider_clients)

    storage = SafeStorage(resolved)
    prompts = PromptRegistry(resolved.prompt_config_dir)
    workspace = WorkspaceService(storage, WorkspaceRepository(session_factory))
    retrieval = RetrievalService(session_factory, resolved)
    research_search = ResearchSearchService(resolved)
    document_repository = DocumentRepository(session_factory, resolved, storage)
    document_repository.recover_stale_ingestions()
    document_ocr = DocumentOCR(resolved)
    document_formatter = DocumentFormatter(resolved)
    document_vision = VisionEnhancer(
        resolved,
        ollama,
        model_runtime,
        document_formatter,
        prompts,
    )
    document_ingestion = DocumentIngestion(
        resolved,
        storage,
        document_repository,
        retrieval,
        document_ocr,
        document_vision,
        document_formatter,
    )
    documents = DocumentService(
        document_repository,
        document_ingestion,
        document_ocr,
        workspace,
    )
    source_downloads = SourceDownloadService(
        resolved,
        documents,
        workspace,
    )

    tool_catalog = create_tool_catalog(prompts)
    compiler = AgentCompiler(
        model_resolver,
        tool_catalog,
        settings=resolved,
        prompts=prompts,
        inference_scheduler=inference_scheduler,
    )
    conversation_sessions = ConversationSessionFactory(resolved.database_path)
    conversations = ConversationService(
        ConversationRepository(session_factory),
        conversation_sessions,
        storage,
    )
    events = EventBroker()
    run_repository = RunRepository(session_factory)
    tool_runtime = ApplicationToolRuntime(
        settings=resolved,
        documents=documents,
        workspace=workspace,
        retrieval=retrieval,
        research_search=research_search,
        source_downloads=source_downloads,
        conversations=conversations,
        prompts=prompts,
        storage=storage,
        run_repository=run_repository,
    )
    runs = RunService(
        run_repository,
        conversation_sessions,
        tool_runtime,
        events,
        settings=resolved,
        prompts=prompts,
        retention_days=resolved.run_retention_days,
        delete_run_artifacts=documents.delete_run_artifacts,
        run_log_dir=resolved.data_dir / "run_logs",
        inference_scheduler=inference_scheduler,
        completion_validators={
            PAPER_WORK_COMPLETION_POLICY_ID: validate_paper_work_completion,
            PAPER_SUMMARY_COMPLETION_POLICY_ID: partial(
                validate_paper_summary_completion, workspace=workspace
            ),
        },
    )
    conversation_turns = ConversationTurnService(
        compiler,
        conversations,
        runs,
        prompts,
    )
    summaries = PaperSummaryService(
        compiler,
        runs,
        documents,
        workspace,
        prompts,
    )
    tool_runtime.set_summary_runner(summaries.run_for_agent)
    providers = ProviderService(
        provider_repository,
        model_runtime,
        model_resolver,
        provider_clients,
        prompts,
    )
    return ApplicationServices(
        settings=resolved,
        session_factory=session_factory,
        settings_service=settings_service,
        storage=storage,
        ollama=ollama,
        model_runtime=model_runtime,
        provider_clients=provider_clients,
        model_resolver=model_resolver,
        providers=providers,
        retrieval=retrieval,
        research_search=research_search,
        source_downloads=source_downloads,
        document_repository=document_repository,
        document_ocr=document_ocr,
        document_vision=document_vision,
        document_formatter=document_formatter,
        document_ingestion=document_ingestion,
        documents=documents,
        workspace=workspace,
        prompts=prompts,
        tool_catalog=tool_catalog,
        compiler=compiler,
        conversation_sessions=conversation_sessions,
        conversations=conversations,
        events=events,
        inference_scheduler=inference_scheduler,
        runs=runs,
        conversation_turns=conversation_turns,
        summaries=summaries,
    )
