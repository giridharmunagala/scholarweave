from __future__ import annotations

from typing import Literal

from backend.agents.blueprint import (
    AgentBlueprint,
    ModelReferenceSpec,
    ReasoningEffort,
    SessionPolicySpec,
)
from backend.agents.compiler import AgentCompiler
from backend.conversations.service import ConversationService
from backend.runs.service import RunService
from backend.tools.service import FunctionToolService

AUTONOMOUS_TOOL_IDS = (
    ("search-tools", "tools.search"),
    ("list-documents", "documents.list"),
    ("inspect-paper", "documents.inspect"),
    ("ingest-paper", "documents.ingest"),
    ("read-paper-pages", "documents.read_pages"),
    ("read-document-chunks", "documents.read_chunks"),
    ("search-papers", "retrieval.keyword_search"),
    ("search-web", "web.search"),
    ("search-arxiv", "arxiv.search"),
    ("search-wikipedia", "wikipedia.search"),
    ("download-paper", "documents.download"),
    ("download-web-page", "webpage.download"),
    ("list-web-pages", "webpage.list"),
    ("read-web-page", "webpage.read"),
    ("search-web-page", "webpage.search"),
    ("save-web-page-note", "webpage.notes.save"),
    ("list-workspace", "workspace.list"),
    ("search-workspace", "workspace.search"),
    ("read-workspace", "workspace.read"),
    ("write-workspace", "workspace.write"),
    ("replace-workspace-markdown", "workspace.markdown.replace"),
    ("append-workspace-markdown", "workspace.markdown.append"),
    ("set-workspace-tags", "workspace.tags.set"),
    ("search-workspace-tags", "workspace.tags.search"),
    ("create-workspace-note", "workspace.note.create"),
    ("ensure-paper-workspace", "workspace.paper.ensure"),
    ("set-paper-workspace-name", "workspace.paper.name.set"),
    ("write-artifact", "artifacts.write"),
    ("search-conversation-memory", "conversation.memory.search"),
    ("read-conversation-memory", "conversation.memory.read"),
    ("run-python", "python.execute"),
    ("list-sdk-primitives", "sdk.catalog"),
    ("list-agents", "agents.list"),
    ("get-agent", "agents.get"),
    ("validate-agent", "agents.validate"),
    ("save-agent", "agents.save"),
    ("save-function-tool", "function_tools.save"),
)
EXTENDED_WORK_TOOL_IDS = (
    ("create-work-plan", "extended.plan.create"),
    ("update-work-item", "extended.plan.update"),
    ("save-work-note", "extended.notes.save"),
    ("list-work-notes", "extended.notes.list"),
    ("read-work-note", "extended.notes.read"),
)
FOCUSED_WORKER_TOOL_IDS = (
    "list-documents",
    "inspect-paper",
    "ingest-paper",
    "read-paper-pages",
    "read-document-chunks",
    "search-papers",
    "search-web",
    "search-arxiv",
    "search-wikipedia",
    "download-paper",
    "download-web-page",
    "list-web-pages",
    "read-web-page",
    "search-web-page",
    "search-workspace",
    "read-workspace",
    "search-conversation-memory",
    "read-conversation-memory",
    "save-work-note",
)
WorkMode = Literal["direct", "extended"]


class AutonomousAgentService:
    def __init__(
        self,
        compiler: AgentCompiler,
        conversations: ConversationService,
        runs: RunService,
        function_tools: FunctionToolService,
    ) -> None:
        self._compiler = compiler
        self._conversations = conversations
        self._runs = runs
        self._function_tools = function_tools

    def create_conversation(
        self,
        *,
        title: str,
        model_reference: ModelReferenceSpec,
    ):
        return self._conversations.create(
            title=title,
            kind="autonomous",
            agent_revision_id=None,
            model_reference=model_reference.model_dump(mode="json"),
            session_policy=SessionPolicySpec(),
        )

    def list_conversations(self):
        return self._conversations.list(kind="autonomous")

    def get_conversation(self, conversation_id: str):
        return self._conversations.get(conversation_id)

    def compile_conversation(
        self,
        conversation_id: str,
        *,
        work_mode: WorkMode = "direct",
    ):
        record = self.get_conversation(conversation_id)
        if record.kind != "autonomous":
            raise ValueError("Conversation is not an autonomous-agent chat.")
        return self._compile(record.model_reference_json, work_mode=work_mode)

    async def conversation_items(self, conversation_id: str):
        compiled = self.compile_conversation(conversation_id)
        primary = compiled.resolved_models[compiled.blueprint.entry_agent_id]
        return await self._conversations.items(conversation_id, primary)

    def start_message(
        self,
        conversation_id: str,
        message: str,
        *,
        reasoning_effort: ReasoningEffort | None = None,
        work_mode: WorkMode = "direct",
    ):
        compiled = self.compile_conversation(conversation_id, work_mode=work_mode)
        self._conversations.touch(conversation_id, message)
        return self._runs.create(
            compiled,
            message,
            agent_revision_id=None,
            conversation_id=conversation_id,
            reasoning_effort=reasoning_effort,
        )

    def _compile(
        self,
        model_reference: dict,
        *,
        work_mode: WorkMode = "direct",
    ):
        custom_tools = [
            (
                f"custom-tool-{index}",
                f"custom:{document.latest_revision.id}",
                document.latest_revision.requires_approval,
            )
            for index, document in enumerate(self._function_tools.list(), start=1)
        ]
        return self._compiler.compile(
            autonomous_blueprint(
                model_reference,
                custom_tools=custom_tools,
                work_mode=work_mode,
            )
        )


def autonomous_blueprint(
    model_reference: dict,
    *,
    custom_tools: list[tuple[str, str, bool]] | None = None,
    work_mode: WorkMode = "direct",
) -> AgentBlueprint:
    model = ModelReferenceSpec.model_validate(model_reference or {})
    tool_definitions = [
        *AUTONOMOUS_TOOL_IDS,
        *(EXTENDED_WORK_TOOL_IDS if work_mode == "extended" else ()),
    ]
    application_tools = [
        {
            "id": tool_id,
            "kind": "function",
            "catalog_id": catalog_id,
        }
        for tool_id, catalog_id in tool_definitions
    ]
    dynamic_tools = [
        {
            "id": tool_id,
            "kind": "function",
            "catalog_id": catalog_id,
            "needs_approval": needs_approval,
        }
        for tool_id, catalog_id, needs_approval in custom_tools or []
    ]
    all_tool_ids = [tool["id"] for tool in [*application_tools, *dynamic_tools]]
    if work_mode == "extended":
        coordinator_tool_ids = [
            tool_id
            for tool_id, _catalog_id in EXTENDED_WORK_TOOL_IDS
            if tool_id != "save-work-note"
        ]
        coordinator_tool_ids.extend(
            ["search-conversation-memory", "read-conversation-memory"]
        )
        agents = [
            {
                "id": "agent",
                "name": "ScholarWeave Coordinator",
                "description": "Plans sequential work, delegates focused contexts, and synthesizes results.",
                "instructions": (
                    "You are ScholarWeave's extended-work coordinator. Keep your own context small. "
                    "For a genuinely multi-part request, first search prior conversation memory using "
                    "specific keywords. Reuse an older result only when the request and evidence clearly "
                    "match; otherwise treat it as background and update the work. Invoke plan_extended_work "
                    "with the complete user request and a compact statement of relevant prior context. "
                    "Create the returned ordered plan with create_extended_work_plan. Execute exactly one "
                    "work item at a time by invoking execute_focused_work with a self-contained prompt that "
                    "includes the work-item ID, objective, necessary constraints, and expected output. Each "
                    "focused worker has a fresh context and must save detailed findings to a run note. After "
                    "each worker returns, mark the current item completed or blocked with a concise summary. "
                    "Do not issue parallel calls. After all items settle, list the run notes and read only "
                    "the notes needed to reconcile findings. Synthesize one direct answer to the original "
                    "request, explicitly resolving conflicts and uncertainty. For simple requests that do "
                    "not benefit from decomposition, answer directly without manufacturing a plan."
                ),
                "model": model,
                "model_settings": {"parallel_tool_calls": False},
                "tool_ids": coordinator_tool_ids,
            },
            {
                "id": "planner",
                "name": "Extended Work Planner",
                "description": "Turns a broad request into independent, ordered focused work items.",
                "instructions": (
                    "Decompose the supplied request into the smallest useful ordered set of independent "
                    "work items. Each item must be specific enough for a fresh-context worker. Preserve all "
                    "user constraints. Use two to eight items; do not solve the request."
                ),
                "model": model,
                "model_settings": {"parallel_tool_calls": False},
                "output": {
                    "kind": "json_schema",
                    "name": "ExtendedWorkPlan",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "tasks": {
                                "type": "array",
                                "minItems": 2,
                                "maxItems": 8,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "string"},
                                        "title": {"type": "string"},
                                        "instructions": {"type": "string"},
                                        "expected_output": {"type": "string"},
                                    },
                                    "required": [
                                        "id",
                                        "title",
                                        "instructions",
                                        "expected_output",
                                    ],
                                    "additionalProperties": False,
                                },
                            }
                        },
                        "required": ["tasks"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "id": "worker",
                "name": "Focused Work Specialist",
                "description": "Solves one bounded work item in a fresh context and stores detailed findings.",
                "instructions": (
                    "Complete exactly the supplied work item. Treat its prompt as your full scope and do not "
                    "expand into unrelated parts of the parent request. Search prior conversations only when "
                    "it can avoid duplicate work, and reuse a result only when it clearly matches. Use the "
                    "research, document, and workspace tools as needed. Verify important claims. "
                    "Before returning, call save_extended_work_note with the supplied work-item ID, a concise "
                    "summary, detailed evidence-bearing content, and source references. Return only a compact "
                    "handoff containing the summary, key findings, caveats, and saved note ID."
                ),
                "model": model,
                "model_settings": {"parallel_tool_calls": False},
                "tool_ids": list(FOCUSED_WORKER_TOOL_IDS),
            },
        ]
        agent_tools = [
            {
                "id": "plan-work",
                "owner_agent_id": "agent",
                "delegate_agent_id": "planner",
                "tool_name": "plan_extended_work",
                "tool_description": "Create a focused sequential plan for a broad request.",
                "max_turns": 4,
            },
            {
                "id": "execute-work",
                "owner_agent_id": "agent",
                "delegate_agent_id": "worker",
                "tool_name": "execute_focused_work",
                "tool_description": "Execute one bounded work item in a fresh reduced context.",
                "max_turns": 30,
            },
        ]
        session = {"history_max_items": 12, "messages_only": True}
        max_turns = 80
        description = "Completes extended research through sequential isolated-context work."
    else:
        agents = [
            {
                "id": "agent",
                "name": "ScholarWeave Agent",
                "description": "A single autonomous research agent with persistent memory.",
                "instructions": (
                    "You are ScholarWeave's single autonomous research agent. Work like a capable coding "
                    "assistant, but optimize for literature research: finding relevant papers, reading "
                    "primary text, tracing evidence, comparing methods and results, identifying explicit "
                    "limitations and open questions, and writing durable research notes or artifacts. "
                    "Take responsibility for completing the user's requested outcome, not merely "
                    "describing steps. Every tool listed in your tool definitions is directly available "
                    "to you. Choose when to invoke each tool, evaluate its result, and continue the "
                    "tool-use loop until the request is complete or a concrete blocker makes completion "
                    "impossible. For external research, decompose broad questions into focused searches, "
                    "use arXiv for primary papers, Wikipedia for background and terminology, and SearXNG "
                    "web search for wider coverage. Recursively refine queries from useful names, citations, "
                    "and gaps in earlier results, cross-check important claims across independent sources, "
                    "and stop searching once the evidence is sufficient for the requested outcome. Include "
                    "the returned source URLs when citing external evidence. Download an arXiv result with "
                    "download_paper when the user requests it or full-paper evidence is needed. Download an "
                    "HTML page before answering questions about its full content; temporary pages expire, "
                    "but notes saved with save_web_page_note persist with their source URL. Use "
                    "search_available_tools when a keyword search would help you discover a capability; it "
                    "is an index of your tools, not a proxy for calling them. Prefer primary paper evidence "
                    "and cite page or chunk citations returned by tools. Inspect a paper before reading it; "
                    "if its source exists but extracted content is unavailable, ingest it and then inspect it "
                    "again. Never treat metadata alone as paper content. Inspect before writing, and verify "
                    "action results before reporting success. Never claim an action happened without a "
                    "successful tool result. Discover prior work with indexed workspace search instead of "
                    "listing every file. Search prior conversation memory when a specific earlier result may "
                    "avoid repeated work, but reuse it only when the match is clear. Use execute_python for "
                    "exact calculations and data generation rather than mental arithmetic. Create general-"
                    "purpose notes with create_workspace_note. Store durable notes for a paper only under its "
                    "canonical papers/<document-id>/ folder. Prefer exact replacement or append tools over "
                    "rewriting an existing Markdown file, and tag files for later search. Ask the user only "
                    "when essential information or approval is unavailable; otherwise make safe decisions. "
                    "Conversation memory is maintained by the SDK session, so use prior context without "
                    "restating it. You are the only agent in direct mode: do not delegate or create additional "
                    "agents unless the user explicitly asks you to author an agent definition. When a result "
                    "is quantitative and comparing values is the point, render it as a chart: emit a fenced "
                    "```chart block containing JSON with type, title, labels, and series. Use it alongside "
                    "the sentence stating the finding and only with real numbers. Keep the final response "
                    "concise and outcome-first."
                ),
                "model": model,
                "model_settings": {"parallel_tool_calls": False},
                "tool_ids": all_tool_ids,
            }
        ]
        agent_tools = []
        session = {}
        max_turns = 50
        description = "Completes end-to-end research tasks through autonomous tool use."
    return AgentBlueprint(
        name=(
            "ScholarWeave extended work"
            if work_mode == "extended"
            else "ScholarWeave autonomous agent"
        ),
        description=description,
        entry_agent_id="agent",
        agents=agents,
        tools=[*application_tools, *dynamic_tools],
        agent_tools=agent_tools,
        session=session,
        run={"max_turns": max_turns, "max_tool_concurrency": 1},
    )
