from __future__ import annotations

from backend.agents.blueprint import AgentBlueprint, ModelReferenceSpec, SessionPolicySpec
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
    ("list-sdk-primitives", "sdk.catalog"),
    ("list-agents", "agents.list"),
    ("get-agent", "agents.get"),
    ("validate-agent", "agents.validate"),
    ("save-agent", "agents.save"),
    ("save-function-tool", "function_tools.save"),
)


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

    def compile_conversation(self, conversation_id: str):
        record = self.get_conversation(conversation_id)
        if record.kind != "autonomous":
            raise ValueError("Conversation is not an autonomous-agent chat.")
        return self._compile(record.model_reference_json)

    async def conversation_items(self, conversation_id: str):
        compiled = self.compile_conversation(conversation_id)
        primary = compiled.resolved_models[compiled.blueprint.entry_agent_id]
        return await self._conversations.items(conversation_id, primary)

    def start_message(self, conversation_id: str, message: str):
        compiled = self.compile_conversation(conversation_id)
        self._conversations.touch(conversation_id, message)
        return self._runs.create(
            compiled,
            message,
            agent_revision_id=None,
            conversation_id=conversation_id,
        )

    def _compile(self, model_reference: dict):
        custom_tools = [
            (
                f"custom-tool-{index}",
                f"custom:{document.latest_revision.id}",
                document.latest_revision.requires_approval,
            )
            for index, document in enumerate(self._function_tools.list(), start=1)
        ]
        return self._compiler.compile(
            autonomous_blueprint(model_reference, custom_tools=custom_tools)
        )


def autonomous_blueprint(
    model_reference: dict,
    *,
    custom_tools: list[tuple[str, str, bool]] | None = None,
) -> AgentBlueprint:
    model = ModelReferenceSpec.model_validate(model_reference or {})
    application_tools = [
        {
            "id": tool_id,
            "kind": "function",
            "catalog_id": catalog_id,
        }
        for tool_id, catalog_id in AUTONOMOUS_TOOL_IDS
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
    tool_ids = [tool["id"] for tool in [*application_tools, *dynamic_tools]]
    return AgentBlueprint(
        name="ScholarWeave autonomous agent",
        description="Completes end-to-end research tasks through autonomous tool use.",
        entry_agent_id="agent",
        agents=[
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
                    "search_available_tools when "
                    "a keyword search would help you discover "
                    "a capability; it is an index of your tools, not a proxy for calling them. Prefer "
                    "primary paper evidence and cite page or chunk citations returned by tools. Inspect "
                    "a paper before reading it; if its source exists but extracted content is unavailable, "
                    "ingest it and then inspect it again. Never treat metadata alone as paper content. "
                    "Inspect before writing, and verify action results before reporting success. Never claim an "
                    "action happened without a successful tool result. Discover prior work with indexed "
                    "workspace search instead of listing every file. Create general-purpose notes with "
                    "create_workspace_note, supplying a concise descriptive name; the application generates "
                    "the UUID and stores the note under notes/<uuid>/note.md. Store durable notes for a paper only "
                    "under the canonical papers/<document-id>/ folder returned by ensure_paper_workspace: "
                    "use summary.md for synthesized results and notes.md for supporting notes. Prefer exact "
                    "replacement or append tools over rewriting an existing Markdown file, and tag files "
                    "with useful subject and method terms for later search. Ask the user only when essential "
                    "information or approval is unavailable; otherwise make safe, reasonable decisions "
                    "autonomously. Conversation memory is maintained by the SDK session, so use prior "
                    "context without restating it. You are the only agent: do "
                    "not delegate or create additional agents unless the user explicitly asks you to "
                    "author an agent definition. Keep the final response concise and outcome-first."
                ),
                "model": model,
                "model_settings": {"parallel_tool_calls": False},
                "tool_ids": tool_ids,
            }
        ],
        tools=[*application_tools, *dynamic_tools],
        run={"max_turns": 50, "max_tool_concurrency": 1},
    )
