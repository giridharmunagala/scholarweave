from __future__ import annotations

from dataclasses import dataclass, replace

from backend.agents.blueprint import (
    AgentBlueprint,
    AgentSpec,
    FunctionToolSpec,
    ModelReferenceSpec,
    RunSettingsSpec,
    SessionPolicySpec,
)
from backend.agents.compiler import AgentCompiler, CompiledAgent
from backend.conversations.models import ConversationRecord
from backend.conversations.service import ConversationService
from backend.direct_agents.repository import DirectAgentRepository
from backend.direct_agents.schemas import DirectAgentKey
from backend.documents import DocumentService
from backend.runtime.context import ScholarWeaveContext
from backend.workspace.service import WorkspaceService


@dataclass(frozen=True, slots=True)
class DirectAgentDefinition:
    key: DirectAgentKey
    name: str
    description: str
    requires_document: bool


DIRECT_AGENTS = (
    DirectAgentDefinition(
        "summary",
        "Summary agent",
        "Reads an entire retained paper and answers the four fixed summary questions.",
        True,
    ),
    DirectAgentDefinition(
        "open_areas",
        "Open areas identification agent",
        "Finds explicitly stated open research areas across saved paper summaries.",
        False,
    ),
    DirectAgentDefinition(
        "qa",
        "Q & A bot",
        "Answers questions using one retained research paper as its only source.",
        True,
    ),
    DirectAgentDefinition(
        "paper_cleaner",
        "Research paper cleaner",
        "Reviews every page and records a keep or no-keep decision with a reason.",
        True,
    ),
)
DEFINITIONS = {definition.key: definition for definition in DIRECT_AGENTS}


class DirectAgentService:
    def __init__(
        self,
        repository: DirectAgentRepository,
        conversations: ConversationService,
        compiler: AgentCompiler,
        documents: DocumentService,
        workspace: WorkspaceService,
    ) -> None:
        self.repository = repository
        self._conversations = conversations
        self._compiler = compiler
        self._documents = documents
        self._workspace = workspace

    def create_conversation(
        self,
        *,
        agent_key: DirectAgentKey,
        document_ids: list[str],
        title: str,
        model_reference: ModelReferenceSpec,
    ) -> ConversationRecord:
        definition = DEFINITIONS[agent_key]
        if definition.requires_document and len(document_ids) != 1:
            raise ValueError(f"{definition.name} requires exactly one paper.")
        if not definition.requires_document and document_ids:
            raise ValueError(f"{definition.name} works across saved summaries and takes no paper.")
        for document_id in document_ids:
            document = self._documents.get_document(document_id)
            if document is None:
                raise ValueError("Selected paper was not found.")
            if document.status != "ready":
                raise ValueError("Selected paper must be ingested before an agent can use it.")
        record = self._conversations.create(
            title=title,
            kind="direct_agent",
            agent_revision_id=None,
            model_reference=model_reference.model_dump(mode="json"),
            session_policy=SessionPolicySpec(),
        )
        self.repository.create_conversation_scope(
            record.id,
            agent_key=agent_key,
            document_ids=document_ids,
        )
        return record

    def compile_conversation(self, conversation_id: str) -> CompiledAgent:
        record = self._conversations.get(conversation_id)
        if record.kind != "direct_agent":
            raise ValueError("Conversation is not a direct-agent chat.")
        scope = self.repository.get_conversation_scope(conversation_id)
        compiled = self._compiler.compile(
            direct_agent_blueprint(
                scope.agent_key,
                list(scope.document_ids_json or []),
                ModelReferenceSpec.model_validate(record.model_reference_json or {}),
            )
        )
        validator = self._completion_validator(
            scope.agent_key,
            list(scope.document_ids_json or []),
        )
        return replace(compiled, completion_validator=validator)

    def _completion_validator(
        self,
        agent_key: DirectAgentKey,
        document_ids: list[str],
    ):
        document_id = document_ids[0] if document_ids else None

        def validate(context: ScholarWeaveContext) -> None:
            if agent_key == "open_areas":
                if not context.metadata.get("paper_summaries_listed"):
                    raise ValueError("Open-areas agent must inspect saved summaries before finishing.")
                return
            if document_id is None:
                raise ValueError("Direct paper agent has no selected paper.")
            document = self._documents.get_document(document_id)
            if document is None or document.page_count is None:
                raise ValueError("Selected paper is no longer available.")
            read_pages = set(context.metadata.get("paper_pages_read", []))
            if agent_key == "qa":
                if not context.metadata.get("paper_pages_tool_called"):
                    raise ValueError("Q & A bot must read retained paper pages before answering.")
                return
            if agent_key == "summary":
                decisions = self.repository.page_decisions(document_id)
                expected = {
                    page
                    for page in range(1, document.page_count + 1)
                    if decisions.get(page) != "no_keep"
                }
                if read_pages != expected:
                    raise ValueError("Summary agent must read every retained page before finishing.")
                if not context.metadata.get("paper_pages_tool_called"):
                    raise ValueError("Summary agent must inspect retained pages before finishing.")
                summary = context.metadata.get("paper_summary")
                if not context.metadata.get("paper_summary_saved") or not isinstance(summary, dict):
                    raise ValueError("Summary agent must save the four-part summary before finishing.")
                self.repository.save_summary(
                    document_id,
                    contribution=str(summary["contribution"]),
                    contributions_detail=str(summary["contributions_detail"]),
                    experimentation_results=str(summary["experimentation_results"]),
                    open_areas=list(summary["open_areas"]),
                )
                paper_workspace = self._workspace.ensure_paper_folder(
                    document_id,
                    document.title,
                )
                self._workspace.write_file(
                    paper_workspace["summary_path"],
                    _summary_markdown(document.title, summary),
                )
                return
            expected = set(range(1, document.page_count + 1))
            if read_pages != expected:
                raise ValueError("Paper cleaner must read every page before finishing.")
            if set(context.metadata.get("paper_pages_decided", [])) != expected:
                raise ValueError("Paper cleaner must save a decision for every page before finishing.")
            decisions = context.metadata.get("paper_page_decisions")
            if not isinstance(decisions, dict):
                raise ValueError("Paper cleaner has no staged page decisions.")
            self.repository.save_page_decisions(
                document_id,
                list(decisions.values()),
            )

        return validate


def _summary_markdown(title: str, summary: dict) -> str:
    open_areas = list(summary["open_areas"])
    if open_areas:
        open_areas_markdown = "\n".join(
            f"- {area['statement']} ({area['citation']})"
            for area in open_areas
        )
    else:
        open_areas_markdown = "No explicit open research areas were identified."
    return (
        f"# {title}\n\n"
        f"## Contribution\n\n{summary['contribution']}\n\n"
        f"## Detailed contributions\n\n{summary['contributions_detail']}\n\n"
        f"## Experiments and results\n\n{summary['experimentation_results']}\n\n"
        f"## Open research areas\n\n{open_areas_markdown}\n"
    )


def direct_agent_blueprint(
    agent_key: DirectAgentKey,
    document_ids: list[str],
    model_reference: ModelReferenceSpec,
) -> AgentBlueprint:
    document_id = document_ids[0] if document_ids else None
    instructions, tool_catalog_ids, max_turns = _agent_configuration(agent_key, document_id)
    tools = [
        FunctionToolSpec(
            id=f"research-tool-{index}",
            catalog_id=catalog_id,
        )
        for index, catalog_id in enumerate(tool_catalog_ids, start=1)
    ]
    definition = DEFINITIONS[agent_key]
    return AgentBlueprint(
        name=definition.name,
        description=definition.description,
        entry_agent_id="direct-agent",
        agents=[
            AgentSpec(
                id="direct-agent",
                name=definition.name,
                description=definition.description,
                instructions=instructions,
                model=model_reference,
                tool_ids=[tool.id for tool in tools],
            )
        ],
        tools=tools,
        run=RunSettingsSpec(max_turns=max_turns),
    )


def _agent_configuration(
    agent_key: DirectAgentKey,
    document_id: str | None,
) -> tuple[str, tuple[str, ...], int]:
    if agent_key == "summary":
        return (
            f"""You are the fixed ScholarWeave summary agent for paper ID {document_id}.
Read every available retained page with read_retained_paper_pages, paginating until has_more is false.
Do not answer from partial reading. Pages marked no_keep by the cleaner are unavailable by design.
Answer exactly these sections:
1. What is the contribution of this paper?
2. Explain the contributions in detail.
3. Experimentation and results.
4. Open areas for further research.
For section 4, include only directions the paper explicitly identifies as future work, limitations to
address, or open research. Never infer an open area. If none is explicit, say so. Cite page numbers.
Before the final answer, call save_paper_summary once with all four answers. Each open-area entry must
contain the paper's explicit statement and its page citation.""",
            ("research.pages.read_retained", "research.summaries.save"),
            40,
        )
    if agent_key == "open_areas":
        return (
            """You are the fixed ScholarWeave open-areas identification agent.
Call list_paper_summaries to inspect every saved summary in the paper repository. Identify themes,
gaps, and research opportunities using only the summaries' explicitly stated open_areas entries.
Do not infer open areas from contributions, experiments, limitations without future-work language,
or your external knowledge. Name the source papers and preserve their page citations. If no saved
summaries contain explicit open areas, say so plainly. Continue the conversation using this evidence.""",
            ("research.summaries.list",),
            20,
        )
    if agent_key == "qa":
        return (
            f"""You are the fixed ScholarWeave paper Q & A bot for paper ID {document_id}.
Answer only from this paper's retained pages. Use read_retained_paper_pages and paginate as needed
to gather sufficient context. Pages marked no_keep by the cleaner are unavailable by design.
Cite page numbers for factual claims. If the retained paper does not support an answer, say that
the answer is not present in the paper; do not use outside knowledge or invent details.""",
            ("research.pages.read_retained",),
            40,
        )
    return (
        f"""You are the fixed ScholarWeave research paper cleaner for paper ID {document_id}.
Review every page using read_all_paper_pages, paginating until has_more is false. For every page,
decide keep or no_keep. Keep pages containing research substance needed for understanding,
summarization, evidence, methods, experiments, results, limitations, conclusions, or explicit future
work. Mark no_keep only for content that is unnecessary for downstream research analysis, such as
blank pages, publisher boilerplate, standalone navigation, or purely administrative material.
Call save_paper_page_decisions for every page exactly once (batches are allowed), with a concise
reason. Do not finish until saved_count covers the paper's total_pages. Then return a page-by-page
keep/no-keep table and totals.""",
        ("research.pages.read_all", "research.page_decisions.save"),
        100,
    )
