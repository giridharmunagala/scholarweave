from __future__ import annotations

from dataclasses import replace
import re
from typing import Any

from backend.agents.blueprint import (
    AgentBlueprint,
    ModelReferenceSpec,
    ReasoningEffort,
    SessionPolicySpec,
)
from backend.agents.compiler import AgentCompiler
from backend.conversations.service import ConversationService
from backend.conversations.schemas import ResearchMode
from backend.core.errors import ConflictError, ValidationError
from backend.runs.service import RunService
from backend.prompting.registry import PromptRegistry, default_prompt_registry
from backend.agents.context import ScholarWeaveContext

PAPER_WORK_COMPLETION_POLICY_ID = "paper-work-v1"

RESEARCH_TOOL_IDS = (
    ("search-sources", "research.sources.search"),
    ("acquire-source", "research.sources.acquire"),
    ("search-library", "research.library.search"),
    ("organize-library", "research.library.organize"),
    ("read-paper", "research.paper.read"),
    ("read-web-page", "research.web.read"),
    ("search-notes", "research.notes.search"),
    ("read-note", "research.notes.read"),
    ("save-note", "research.notes.save"),
    ("summarize-paper", "research.summary.run"),
)
CONVERSATION_TITLE_TOOL = ("set-title", "conversation.title.set")
WORK_PLAN_TOOL_IDS = (
    ("create-work-plan", "work.plan.create"),
    ("update-work-item", "work.plan.update"),
    ("read-work-plan", "work.plan.read"),
)
AUTONOMOUS_TOOL_IDS = RESEARCH_TOOL_IDS
EXTERNAL_TOOL_IDS = {"search-sources", "acquire-source"}
FAST_ANSWER_TOOL_IDS = {"search-sources", "acquire-source", "read-web-page"}


def _resolve_research_mode(
    research_mode: ResearchMode | None,
    *,
    deep_work: bool = False,
    fast_answer: bool = False,
) -> ResearchMode:
    if research_mode not in {None, "learn", "understand", "review"}:
        raise ValidationError("Unknown research mode.")
    if deep_work and fast_answer:
        raise ValidationError("Fast Answer is unavailable in a Deep Work conversation.")
    if deep_work and research_mode not in {None, "review"}:
        raise ValidationError("Deep Work requires review mode.")
    if fast_answer and research_mode not in {None, "learn"}:
        raise ValidationError("Fast Answer requires learn mode.")
    return research_mode or ("learn" if fast_answer else "review")


def _research_mode_metadata(research_mode: ResearchMode) -> dict[str, Any]:
    return {
        "research_mode": research_mode,
        "paper_require_summary": research_mode == "review",
        "paper_require_notes": research_mode == "review",
    }


def _resolve_prompts(prompts: PromptRegistry | None) -> PromptRegistry:
    """Agent instructions live only in the prompt registry defaults, never duplicated in code."""
    return prompts if prompts is not None else default_prompt_registry()


class ConversationTurnService:
    def __init__(
        self,
        compiler: AgentCompiler,
        conversations: ConversationService,
        runs: RunService,
        prompts: PromptRegistry | None = None,
    ) -> None:
        self._compiler = compiler
        self._conversations = conversations
        self._runs = runs
        self._prompts = prompts
        self._deleting_conversations: set[str] = set()

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
            model_reference=model_reference.model_dump(mode="json"),
            session_policy=SessionPolicySpec(),
        )

    def list_conversations(self):
        return self._conversations.list()

    async def delete_conversation(self, conversation_id: str) -> None:
        self._conversations.get(conversation_id)
        if conversation_id in self._deleting_conversations:
            raise ConflictError("Conversation deletion is already in progress.")
        self._deleting_conversations.add(conversation_id)
        try:
            await self._runs.delete_conversation_runs(conversation_id)
            await self._conversations.delete(conversation_id)
        finally:
            self._deleting_conversations.discard(conversation_id)

    def list_deep_work_conversations(self):
        return self._conversations.list(kind="deep_work")

    def get_conversation(self, conversation_id: str):
        record = self._conversations.get(conversation_id)
        if record.kind not in {"autonomous", "deep_work"}:
            raise ValueError("Conversation is not a supported research chat.")
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
        deep_work: bool = False,
        fast_answer: bool = False,
        research_mode: ResearchMode | None = None,
        web_search_limit: int = 1,
        message: str = "",
        context_window_tokens: int | None = None,
        first_turn: bool | None = None,
    ):
        record = self.get_conversation(conversation_id)
        deep_work = deep_work or record.kind == "deep_work"
        research_mode = _resolve_research_mode(
            research_mode, deep_work=deep_work, fast_answer=fast_answer,
        )
        first_turn = (
            not record.last_message_preview.strip()
            if first_turn is None
            else first_turn
        )
        blueprint = (
            deep_work_blueprint(
                record.model_reference_json,
                web_enabled=web_enabled,
                prompts=self._prompts,
                first_turn=first_turn,
            )
            if deep_work
            else research_blueprint(
                record.model_reference_json,
                web_enabled=web_enabled,
                fast_answer=fast_answer,
                research_mode=research_mode,
                web_search_limit=web_search_limit,
                prompts=self._prompts,
                first_turn=first_turn,
            )
        )
        compiled = self._compiler.compile(
            blueprint,
            context_window_tokens=context_window_tokens,
        )
        return replace(
            compiled,
            completion_validator=validate_paper_work_completion,
            completion_policy_id=PAPER_WORK_COMPLETION_POLICY_ID,
        )

    def compile_deep_work_conversation(
        self,
        conversation_id: str,
        *,
        web_enabled: bool = True,
        message: str = "",
        context_window_tokens: int | None = None,
        first_turn: bool | None = None,
    ):
        self.get_deep_work_conversation(conversation_id)
        return self.compile_conversation(
            conversation_id,
            web_enabled=web_enabled,
            deep_work=True,
            message=message,
            context_window_tokens=context_window_tokens,
            first_turn=first_turn,
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
        deep_work: bool = False,
        fast_answer: bool = False,
        research_mode: ResearchMode | None = None,
        web_search_limit: int = 1,
        context_window_tokens: int | None = None,
    ):
        record = self.get_conversation(conversation_id)
        first_turn = not record.last_message_preview.strip()
        deep_work = deep_work or record.kind == "deep_work"
        research_mode = _resolve_research_mode(
            research_mode, deep_work=deep_work, fast_answer=fast_answer,
        )
        compiled = self.compile_conversation(
            conversation_id,
            web_enabled=web_enabled,
            deep_work=deep_work,
            fast_answer=fast_answer,
            research_mode=research_mode,
            web_search_limit=web_search_limit,
            message=message,
            context_window_tokens=context_window_tokens,
            first_turn=first_turn,
        )
        if deep_work and record.kind == "autonomous":
            self._conversations.promote_to_deep_work(conversation_id)
        return self._start_message(
            conversation_id,
            message,
            compiled=compiled,
            reasoning_effort=reasoning_effort,
            runtime_metadata={
                **_research_mode_metadata(research_mode),
                **({"autonomous_work": True} if deep_work else {}),
                **(
                    {
                        "fast_answer": True,
                        "web_search_limit": web_search_limit,
                    }
                    if fast_answer and not deep_work
                    else {}
                ),
                "allow_conversation_title_update": first_turn,
            },
        )

    def start_deep_work_message(
        self,
        conversation_id: str,
        message: str,
        *,
        reasoning_effort: ReasoningEffort | None = None,
        web_enabled: bool = True,
        research_mode: ResearchMode | None = None,
        fast_answer: bool = False,
        context_window_tokens: int | None = None,
    ):
        _resolve_research_mode(research_mode, deep_work=True, fast_answer=fast_answer)
        first_turn = not self.get_deep_work_conversation(
            conversation_id
        ).last_message_preview.strip()
        return self._start_message(
            conversation_id,
            message,
            compiled=self.compile_deep_work_conversation(
                conversation_id,
                web_enabled=web_enabled,
                message=message,
                context_window_tokens=context_window_tokens,
                first_turn=first_turn,
            ),
            reasoning_effort=reasoning_effort,
            runtime_metadata={
                **_research_mode_metadata("review"),
                "autonomous_work": True,
                "allow_conversation_title_update": first_turn,
            },
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
        if conversation_id in self._deleting_conversations:
            raise ConflictError("Conversation deletion is in progress.")
        self._conversations.touch(conversation_id, message)
        return self._runs.create(
            compiled,
            message,
            conversation_id=conversation_id,
            reasoning_effort=reasoning_effort,
            runtime_metadata=runtime_metadata,
        )


def _application_tools(
    *,
    web_enabled: bool = True,
    first_turn: bool = False,
) -> list[dict[str, str]]:
    tools = [
        {
            "id": tool_id,
            "kind": "function",
            "catalog_id": catalog_id,
        }
        for tool_id, catalog_id in RESEARCH_TOOL_IDS
        if web_enabled or tool_id not in EXTERNAL_TOOL_IDS
    ]
    if first_turn:
        tool_id, catalog_id = CONVERSATION_TITLE_TOOL
        tools.append({"id": tool_id, "kind": "function", "catalog_id": catalog_id})
    return tools


def validate_paper_work_completion(context: ScholarWeaveContext) -> None:
    raw_activity = context.metadata.get("paper_activity")
    activity = (
        [
            item
            for item in raw_activity
            if isinstance(item, dict) and item.get("document_id")
        ]
        if isinstance(raw_activity, list)
        else []
    )
    if not activity:
        return
    if (
        context.metadata.get("research_mode") in {"learn", "understand"}
        and not context.metadata.get("autonomous_work")
    ):
        _validate_sourced_paper_answer(context, activity)
        return

    actions_by_document: dict[str, list[str]] = {}
    titles: dict[str, str] = {}
    for item in activity:
        document_id = str(item["document_id"])
        action = str(item.get("action") or "")
        if action == "summary_saved" and (
            item.get("coverage_complete") is False
            or item.get("review_complete") is False
        ):
            action = "summary_partial"
        actions_by_document.setdefault(document_id, []).append(action)
        titles[document_id] = str(item.get("title") or document_id)

    issues: list[str] = []
    required_actions = {
        "read": "extract and read the paper",
        "notes_saved": "populate notes.md through save_research_note",
    }
    for document_id, actions in actions_by_document.items():
        missing = [
            description
            for action, description in required_actions.items()
            if action not in actions
        ]
        if not {"summary_saved", "summary_reused"} & set(actions):
            missing.append(
                "reuse a substantive cited summary or populate summary.md through "
                "summarize_research_paper"
            )
        if "read" in actions:
            read_index = actions.index("read")
            for action, description in (
                ("summary_saved", "save summary.md only after extraction and reading"),
                ("notes_saved", "save notes.md only after extraction and reading"),
            ):
                if action in actions and actions.index(action) < read_index:
                    missing.append(description)
        if missing:
            issues.append(
                f"{titles[document_id]} ({document_id}): " + "; ".join(missing)
            )
    if issues:
        raise ValidationError(
            "Paper work is incomplete. Acquisition must be followed by automatic native-text/OCR "
            "preparation, a reusable or newly generated cited summary, and durable notes before "
            "the run can complete.",
            issues=issues,
        )


def _validate_sourced_paper_answer(
    context: ScholarWeaveContext,
    activity: list[dict[str, Any]],
) -> None:
    documents: dict[str, list[dict[str, Any]]] = {}
    for item in activity:
        documents.setdefault(str(item["document_id"]), []).append(item)
    output = context.metadata.get("completion_output")
    issues: list[str] = []
    for document_id, items in documents.items():
        reads = [item for item in items if item.get("action") == "read"]
        title = str(items[-1].get("title") or document_id)
        if not reads:
            issues.append(f"{title} ({document_id}): read relevant paper passages")
            continue
        citations = {
            citation
            for item in reads
            for citation in (
                item["citations"] if isinstance(item.get("citations"), list) else []
            )
            if isinstance(citation, str) and citation.strip()
        }
        if not citations:
            issues.append(
                f"{title} ({document_id}): obtain page or chunk citations from a source read"
            )
        elif not isinstance(output, str) or not any(
            re.search(r"(?<!\w)" + re.escape(citation) + r"(?!\w)", output)
            for citation in citations
        ):
            issues.append(
                f"{title} ({document_id}): cite a supplied page or chunk in the final answer"
            )
    if issues:
        raise ValidationError(
            "Paper Q&A requires source reading and a cited answer, not a full summary or notes.",
            issues=issues,
        )


def research_blueprint(
    model_reference: dict,
    *,
    web_enabled: bool = True,
    fast_answer: bool = False,
    research_mode: ResearchMode | None = None,
    web_search_limit: int = 1,
    prompts: PromptRegistry | None = None,
    first_turn: bool = False,
) -> AgentBlueprint:
    model = ModelReferenceSpec.model_validate(model_reference or {})
    research_mode = _resolve_research_mode(research_mode, fast_answer=fast_answer)
    if fast_answer and not web_enabled:
        raise ValueError("Fast-answer mode requires web access.")
    prompts = _resolve_prompts(prompts)
    tools = _application_tools(web_enabled=web_enabled, first_turn=first_turn)
    instructions = (
        f"{prompts.render('research')}\n\n"
        f"Selected research mode: {research_mode}."
    )
    if fast_answer:
        allowed_tool_ids = {
            *FAST_ANSWER_TOOL_IDS,
            *(["set-title"] if first_turn else []),
        }
        tools = [tool for tool in tools if tool["id"] in allowed_tool_ids]
        instructions = (
            f"{instructions}\n\n"
            f"{prompts.render('fast-answer', web_search_limit=web_search_limit)}"
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
            "run": {"max_tool_concurrency": 4},
        }
    )


def deep_work_blueprint(
    model_reference: dict,
    *,
    web_enabled: bool = True,
    prompts: PromptRegistry | None = None,
    first_turn: bool = False,
) -> AgentBlueprint:
    model = ModelReferenceSpec.model_validate(model_reference or {})
    prompts = _resolve_prompts(prompts)
    tools = [
        *_application_tools(web_enabled=web_enabled, first_turn=first_turn),
        *(
            {
                "id": tool_id,
                "kind": "function",
                "catalog_id": catalog_id,
            }
            for tool_id, catalog_id in WORK_PLAN_TOOL_IDS
        ),
    ]
    coordinator_tool_ids = [tool["id"] for tool in tools]
    worker_tool_ids = [
        tool_id for tool_id in coordinator_tool_ids if tool_id != "set-title"
    ]
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
                    "instructions": prompts.render("deep-work-coordinator"),
                    "model": model,
                    "model_settings": {"parallel_tool_calls": True},
                    "tool_ids": coordinator_tool_ids,
                },
                {
                    "id": "worker",
                    "name": "Focused Research Worker",
                    "description": "Completes one bounded evidence-gathering track.",
                    "instructions": prompts.render("deep-work-worker"),
                    "model": model,
                    "model_settings": {"parallel_tool_calls": True},
                    "tool_ids": worker_tool_ids,
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
                },
            ],
            "run": {"max_tool_concurrency": 4},
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
