from __future__ import annotations

from backend.agents.blueprint import (
    AgentBlueprint,
    ModelReferenceSpec,
    ReasoningEffort,
    SessionPolicySpec,
)
from backend.agents.compiler import AgentCompiler
from backend.conversations.service import ConversationService
from backend.runs.service import RunService

RESEARCH_TOOL_IDS = (
    ("search-sources", "research.sources.search"),
    ("acquire-source", "research.sources.acquire"),
    ("search-library", "research.library.search"),
    ("read-paper", "research.paper.read"),
    ("read-web-page", "research.web.read"),
    ("search-notes", "research.notes.search"),
    ("read-note", "research.notes.read"),
    ("save-note", "research.notes.save"),
)
AUTONOMOUS_TOOL_IDS = RESEARCH_TOOL_IDS
EXTERNAL_TOOL_IDS = {"search-sources", "acquire-source"}
FAST_ANSWER_TOOL_IDS = {"search-sources", "acquire-source", "read-web-page"}

RESEARCH_INSTRUCTIONS = """You are ScholarWeave's research agent.
Give the shortest answer that fully addresses the request. Use existing conversation evidence first.
Search papers, the web, arXiv, or Wikipedia only when the answer needs evidence you do not have.
Prefer primary papers. Acquire a paper or web page before making claims that require its full text.
Use paper page or chunk citations and web source URLs for factual claims. Never present metadata or a
search snippet as full-source evidence. Stop searching when the evidence is sufficient.
Read or write durable notes only when the user asks or when saving the result is part of the task.
Do not manufacture a plan, narrate routine tool use, or perform unrelated work. If evidence is absent
or conflicting, say so directly. Keep the final answer concise and outcome-first."""

DEEP_WORK_INSTRUCTIONS = """You are ScholarWeave's deep-work research coordinator.
You have the complete research tool set and may research directly. For a broad request with independent
lines of inquiry, delegate self-contained tracks to focused_research_worker; issue independent
delegations together when the provider supports parallel tool calls. Do not delegate simple questions.
Give each worker a precise objective, evidence requirements, and expected compact handoff. Verify key
claims yourself, reconcile disagreements, and stop once the requested outcome is supported. Prefer
primary papers, cite paper pages or chunks, and include source URLs for web claims. Save durable notes
only when requested. Return one concise synthesis rather than a progress report."""

WORKER_INSTRUCTIONS = """Complete exactly the supplied research track.
Use the research tools to gather the smallest sufficient evidence set. Prefer primary papers, cite
paper pages or chunks, and include web source URLs. Do not expand scope or delegate. Return a compact
handoff with findings, evidence, uncertainty, and sources."""

FAST_ANSWER_INSTRUCTIONS = """Fast-answer mode is active. Make at most {web_search_limit} external
search call(s), using only the web provider. Choose useful HTML results from those searches, acquire
any pages needed to answer, then answer immediately. Do not search papers, arXiv, Wikipedia, notes,
or the local library, and do not perform additional searches after acquiring pages. If the searches
or pages are insufficient, state the limitation in the answer instead of searching further."""


class AutonomousAgentService:
    def __init__(
        self,
        compiler: AgentCompiler,
        conversations: ConversationService,
        runs: RunService,
    ) -> None:
        self._compiler = compiler
        self._conversations = conversations
        self._runs = runs

    def create_conversation(
        self,
        *,
        title: str,
        model_reference: ModelReferenceSpec,
    ):
        return self._create_conversation(
            kind="autonomous",
            title=title,
            model_reference=model_reference,
        )

    def create_deep_work_conversation(
        self,
        *,
        title: str,
        model_reference: ModelReferenceSpec,
    ):
        return self._create_conversation(
            kind="deep_work",
            title=title,
            model_reference=model_reference,
        )

    def _create_conversation(
        self,
        *,
        kind: str,
        title: str,
        model_reference: ModelReferenceSpec,
    ):
        return self._conversations.create(
            title=title,
            kind=kind,
            agent_revision_id=None,
            model_reference=model_reference.model_dump(mode="json"),
            session_policy=SessionPolicySpec(),
        )

    def list_conversations(self):
        return self._conversations.list(kind="autonomous")

    def list_deep_work_conversations(self):
        return self._conversations.list(kind="deep_work")

    def get_conversation(self, conversation_id: str):
        record = self._conversations.get(conversation_id)
        if record.kind != "autonomous":
            raise ValueError("Conversation is not a research chat.")
        return record

    def get_deep_work_conversation(self, conversation_id: str):
        record = self._conversations.get(conversation_id)
        if record.kind != "deep_work":
            raise ValueError("Conversation is not a deep-work chat.")
        return record

    def compile_conversation(
        self,
        conversation_id: str,
        *,
        web_enabled: bool = True,
        fast_answer: bool = False,
        web_search_limit: int = 1,
    ):
        record = self.get_conversation(conversation_id)
        return self._compiler.compile(
            research_blueprint(
                record.model_reference_json,
                web_enabled=web_enabled,
                fast_answer=fast_answer,
                web_search_limit=web_search_limit,
            )
        )

    def compile_deep_work_conversation(
        self,
        conversation_id: str,
        *,
        web_enabled: bool = True,
    ):
        record = self.get_deep_work_conversation(conversation_id)
        return self._compiler.compile(
            deep_work_blueprint(
                record.model_reference_json,
                web_enabled=web_enabled,
            )
        )

    async def conversation_items(self, conversation_id: str):
        return await self._conversations.items(conversation_id)

    def start_message(
        self,
        conversation_id: str,
        message: str,
        *,
        reasoning_effort: ReasoningEffort | None = None,
        web_enabled: bool = True,
        fast_answer: bool = False,
        web_search_limit: int = 1,
    ):
        return self._start_message(
            conversation_id,
            message,
            compiled=self.compile_conversation(
                conversation_id,
                web_enabled=web_enabled,
                fast_answer=fast_answer,
                web_search_limit=web_search_limit,
            ),
            reasoning_effort=reasoning_effort,
            runtime_metadata=(
                {
                    "fast_answer": True,
                    "web_search_limit": web_search_limit,
                }
                if fast_answer
                else None
            ),
        )

    def start_deep_work_message(
        self,
        conversation_id: str,
        message: str,
        *,
        reasoning_effort: ReasoningEffort | None = None,
        web_enabled: bool = True,
    ):
        return self._start_message(
            conversation_id,
            message,
            compiled=self.compile_deep_work_conversation(
                conversation_id,
                web_enabled=web_enabled,
            ),
            reasoning_effort=reasoning_effort,
            runtime_metadata=None,
        )

    def _start_message(
        self,
        conversation_id: str,
        message: str,
        *,
        compiled,
        reasoning_effort: ReasoningEffort | None,
        runtime_metadata: dict | None,
    ):
        self._conversations.touch(conversation_id, message)
        return self._runs.create(
            compiled,
            message,
            agent_revision_id=None,
            conversation_id=conversation_id,
            reasoning_effort=reasoning_effort,
            runtime_metadata=runtime_metadata,
        )


def _application_tools(*, web_enabled: bool = True) -> list[dict[str, str]]:
    return [
        {
            "id": tool_id,
            "kind": "function",
            "catalog_id": catalog_id,
        }
        for tool_id, catalog_id in RESEARCH_TOOL_IDS
        if web_enabled or tool_id not in EXTERNAL_TOOL_IDS
    ]


def research_blueprint(
    model_reference: dict,
    *,
    web_enabled: bool = True,
    fast_answer: bool = False,
    web_search_limit: int = 1,
) -> AgentBlueprint:
    model = ModelReferenceSpec.model_validate(model_reference or {})
    if fast_answer and not web_enabled:
        raise ValueError("Fast-answer mode requires web access.")
    tools = _application_tools(web_enabled=web_enabled)
    instructions = RESEARCH_INSTRUCTIONS
    if fast_answer:
        tools = [tool for tool in tools if tool["id"] in FAST_ANSWER_TOOL_IDS]
        instructions = (
            f"{instructions}\n\n"
            f"{FAST_ANSWER_INSTRUCTIONS.format(web_search_limit=web_search_limit)}"
        )
    return AgentBlueprint.model_validate(
        {
            "name": "ScholarWeave research",
            "description": "Fast, evidence-backed research over papers, the web, and notes.",
            "entry_agent_id": "researcher",
            "agents": [
                {
                    "id": "researcher",
                    "name": "ScholarWeave Researcher",
                    "description": "Answers research questions with the smallest sufficient evidence set.",
                    "instructions": instructions,
                    "model": model,
                    "model_settings": {"parallel_tool_calls": True},
                    "tool_ids": [tool["id"] for tool in tools],
                }
            ],
            "tools": tools,
            "run": {"max_turns": 16, "max_tool_concurrency": 2},
        }
    )


def deep_work_blueprint(
    model_reference: dict,
    *,
    web_enabled: bool = True,
) -> AgentBlueprint:
    model = ModelReferenceSpec.model_validate(model_reference or {})
    tools = _application_tools(web_enabled=web_enabled)
    tool_ids = [tool["id"] for tool in tools]
    return AgentBlueprint.model_validate(
        {
            "name": "ScholarWeave deep work",
            "description": "Long-form research with bounded focused delegation.",
            "entry_agent_id": "coordinator",
            "agents": [
                {
                    "id": "coordinator",
                    "name": "Deep Work Coordinator",
                    "description": "Researches directly and delegates independent tracks when useful.",
                    "instructions": DEEP_WORK_INSTRUCTIONS,
                    "model": model,
                    "model_settings": {"parallel_tool_calls": True},
                    "tool_ids": tool_ids,
                },
                {
                    "id": "worker",
                    "name": "Focused Research Worker",
                    "description": "Completes one bounded evidence-gathering track.",
                    "instructions": WORKER_INSTRUCTIONS,
                    "model": model,
                    "model_settings": {"parallel_tool_calls": True},
                    "tool_ids": tool_ids,
                },
            ],
            "tools": tools,
            "agent_tools": [
                {
                    "id": "focused-worker",
                    "owner_agent_id": "coordinator",
                    "delegate_agent_id": "worker",
                    "tool_name": "focused_research_worker",
                    "tool_description": (
                        "Delegate one self-contained research track and receive a compact evidence handoff."
                    ),
                    "max_turns": 24,
                }
            ],
            "run": {"max_turns": 48, "max_tool_concurrency": 3},
            "session": {"history_max_items": 20, "messages_only": True},
        }
    )


def autonomous_blueprint(
    model_reference: dict,
    *,
    custom_tools=None,
    work_mode: str = "direct",
    work_budget: str = "medium",
) -> AgentBlueprint:
    del custom_tools, work_budget
    if work_mode == "extended":
        return deep_work_blueprint(model_reference)
    return research_blueprint(model_reference)
