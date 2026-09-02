from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import anyio
from sqlalchemy.orm import Session, sessionmaker

from backend.agents.compiler import AgentCompiler
from backend.agents.guardrails import create_guardrail_catalog
from backend.agents.repository import AgentRepository
from backend.agents.service import AgentService
from backend.autonomous import AutonomousAgentService
from backend.builder.service import BuilderService
from backend.core.config import Settings
from backend.conversations.repository import ConversationRepository
from backend.conversations.memory import ConversationMemoryService
from backend.conversations.service import ConversationService
from backend.core.settings_service import SettingsService
from backend.documents import DocumentService
from backend.documents.figures import FigureExtractor
from backend.documents.formatting import DocumentFormatter
from backend.documents.ingestion import DocumentIngestion
from backend.documents.ocr import DocumentOCR
from backend.documents.repository import DocumentRepository
from backend.documents.vision import VisionEnhancer
from backend.direct_agents import DirectAgentService
from backend.direct_agents.repository import DirectAgentRepository
from backend.runs.broker import EventBroker
from backend.providers.ollama import OllamaClient
from backend.persistence import create_session_factory
from backend.providers.runtime import ModelRuntime
from backend.providers.repository import ProviderRepository
from backend.providers.sdk_models import ProfileModelResolver, SdkClientPool
from backend.providers.service import ProviderService
from backend.providers.builtin_speech import BuiltInSpeechRuntime
from backend.research import ResearchSearchService, SourceDownloadService
from backend.documents.retrieval import RetrievalService
from backend.runs.repository import RunRepository
from backend.runs.service import RunService
from backend.runtime.sdk_compat import SUPPORTED_SDK_VERSION, assert_supported_sdk
from backend.runtime.sessions import SdkSessionFactory
from backend.persistence.files import SafeStorage
from backend.tools.catalog import create_tool_catalog
from backend.tools.repository import FunctionToolRepository
from backend.tools.runtime import ApplicationToolRuntime
from backend.tools.service import FunctionToolService
from backend.workspace.service import WorkspaceService
from backend.workspace.repository import WorkspaceRepository


@dataclass(slots=True)
class ApplicationServices:
    settings: Settings
    session_factory: sessionmaker[Session]
    settings_service: SettingsService
    storage: SafeStorage
    ollama: OllamaClient
    model_runtime: ModelRuntime
    sdk_clients: SdkClientPool
    model_resolver: ProfileModelResolver
    providers: ProviderService
    builtin_speech: BuiltInSpeechRuntime
    retrieval: RetrievalService
    research_search: ResearchSearchService
    source_downloads: SourceDownloadService
    document_repository: DocumentRepository
    document_ocr: DocumentOCR
    document_vision: VisionEnhancer
    document_formatter: DocumentFormatter
    document_figures: FigureExtractor
    document_ingestion: DocumentIngestion
    documents: DocumentService
    workspace: WorkspaceService
    tool_catalog: Any
    guardrail_catalog: Any
    function_tools: FunctionToolService
    compiler: AgentCompiler
    agents: AgentService
    sdk_sessions: SdkSessionFactory
    conversations: ConversationService
    conversation_memory: ConversationMemoryService
    events: EventBroker
    runs: RunService
    builder: BuilderService
    autonomous: AutonomousAgentService
    direct_agents: DirectAgentService
    sdk_version: str = SUPPORTED_SDK_VERSION

    async def start(self) -> None:
        await self.runs.recover_incomplete(self.compiler)

    async def close(self) -> None:
        await self.builtin_speech.close()
        await self.documents.close()
        await self.runs.close()
        await self.research_search.close()
        await self.source_downloads.close()
        await self.sdk_clients.close()
        engine = self.session_factory.kw.get("bind")
        if engine is not None:
            engine.dispose()


def create_services(settings: Settings | None = None) -> ApplicationServices:
    assert_supported_sdk()
    resolved = settings or Settings()
    resolved.ensure_directories()
    session_factory = create_session_factory(resolved)
    settings_service = SettingsService(resolved, session_factory)
    settings_service.load()

    provider_repository = ProviderRepository(session_factory)
    provider_repository.ensure_default_ollama(base_url=resolved.ollama_base_url)
    ollama_gpu_lock = anyio.Lock()
    ollama = OllamaClient(resolved, request_lock=ollama_gpu_lock)
    model_runtime = ModelRuntime(
        session_factory,
        resolved,
        ollama,
        ollama_gpu_lock,
    )
    sdk_clients = SdkClientPool(model_runtime)
    model_resolver = ProfileModelResolver(model_runtime, sdk_clients)

    storage = SafeStorage(resolved)
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
    )
    document_figures = FigureExtractor(
        resolved,
        storage,
        document_repository,
    )
    document_ingestion = DocumentIngestion(
        resolved,
        storage,
        document_repository,
        retrieval,
        document_ocr,
        document_vision,
        document_figures,
        document_formatter,
    )
    documents = DocumentService(
        document_repository,
        document_ingestion,
        document_ocr,
    )
    source_downloads = SourceDownloadService(
        resolved,
        documents,
        workspace,
    )

    tool_catalog = create_tool_catalog()
    function_tools = FunctionToolService(
        FunctionToolRepository(session_factory),
        resolved,
    )
    tool_catalog.register_dynamic_factory(function_tools.dynamic_factory)
    guardrail_catalog = create_guardrail_catalog()
    compiler = AgentCompiler(
        model_resolver,
        tool_catalog,
        guardrail_catalog,
        settings=resolved,
    )
    agents = AgentService(AgentRepository(session_factory), compiler)

    sdk_sessions = SdkSessionFactory(resolved.database_path)
    conversations = ConversationService(
        ConversationRepository(session_factory),
        sdk_sessions,
    )
    conversation_memory = ConversationMemoryService(session_factory)
    direct_agent_repository = DirectAgentRepository(session_factory)
    events = EventBroker()
    run_repository = RunRepository(session_factory)
    tool_runtime = ApplicationToolRuntime(
        settings=resolved,
        documents=documents,
        workspace=workspace,
        retrieval=retrieval,
        research_search=research_search,
        source_downloads=source_downloads,
        storage=storage,
        agent_service=agents,
        function_tool_service=function_tools,
        tool_catalog=tool_catalog,
        direct_agent_repository=direct_agent_repository,
        run_repository=run_repository,
        conversation_memory=conversation_memory,
    )
    runs = RunService(
        run_repository,
        sdk_sessions,
        tool_runtime,
        events,
        settings=resolved,
        retention_days=resolved.run_retention_days,
        delete_run_artifacts=documents.delete_run_artifacts,
    )
    builder = BuilderService(compiler, conversations, runs)
    autonomous = AutonomousAgentService(compiler, conversations, runs, function_tools)
    direct_agents = DirectAgentService(
        direct_agent_repository,
        conversations,
        compiler,
        documents,
        workspace,
    )
    providers = ProviderService(
        provider_repository,
        model_runtime,
        model_resolver,
        sdk_clients,
    )
    builtin_speech = BuiltInSpeechRuntime(resolved)
    return ApplicationServices(
        settings=resolved,
        session_factory=session_factory,
        settings_service=settings_service,
        storage=storage,
        ollama=ollama,
        model_runtime=model_runtime,
        sdk_clients=sdk_clients,
        model_resolver=model_resolver,
        providers=providers,
        builtin_speech=builtin_speech,
        retrieval=retrieval,
        research_search=research_search,
        source_downloads=source_downloads,
        document_repository=document_repository,
        document_ocr=document_ocr,
        document_vision=document_vision,
        document_formatter=document_formatter,
        document_figures=document_figures,
        document_ingestion=document_ingestion,
        documents=documents,
        workspace=workspace,
        tool_catalog=tool_catalog,
        guardrail_catalog=guardrail_catalog,
        function_tools=function_tools,
        compiler=compiler,
        agents=agents,
        sdk_sessions=sdk_sessions,
        conversations=conversations,
        conversation_memory=conversation_memory,
        events=events,
        runs=runs,
        builder=builder,
        autonomous=autonomous,
        direct_agents=direct_agents,
    )
