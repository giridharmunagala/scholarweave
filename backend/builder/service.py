from __future__ import annotations

from dataclasses import replace

from backend.agents.blueprint import AgentBlueprint, ModelReferenceSpec, SessionPolicySpec
from backend.agents.compiler import AgentCompiler
from backend.builder.todos import validate_builder_completion
from backend.conversations.service import ConversationService
from backend.runs.service import RunService

BUILDER_TOOL_IDS = (
    ("create-todo-plan", "builder.todos.create"),
    ("update-todo", "builder.todos.update"),
    ("finish-run", "builder.finish"),
    ("catalog", "sdk.catalog"),
    ("list-agents", "agents.list"),
    ("get-agent", "agents.get"),
    ("validate-agent", "agents.validate"),
    ("save-agent", "agents.save"),
    ("save-function-tool", "function_tools.save"),
    ("list-workspace", "workspace.list"),
    ("read-workspace", "workspace.read"),
    ("write-workspace", "workspace.write"),
)


class BuilderService:
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
        return self._conversations.create(
            title=title,
            kind="builder",
            agent_revision_id=None,
            model_reference=model_reference.model_dump(mode="json"),
            session_policy=SessionPolicySpec(),
        )

    def list_conversations(self):
        return self._conversations.list(kind="builder")

    def get_conversation(self, conversation_id: str):
        return self._conversations.get(conversation_id)

    async def conversation_items(self, conversation_id: str):
        record = self.get_conversation(conversation_id)
        compiled = self._compile(record.model_reference_json)
        primary = compiled.resolved_models[compiled.blueprint.entry_agent_id]
        return await self._conversations.items(conversation_id, primary)

    def start_message(self, conversation_id: str, message: str):
        record = self.get_conversation(conversation_id)
        compiled = self._compile(record.model_reference_json)
        self._conversations.touch(conversation_id, message)
        return self._runs.create(
            compiled,
            message,
            agent_revision_id=None,
            conversation_id=conversation_id,
        )

    async def send_message(self, conversation_id: str, message: str):
        record = self.get_conversation(conversation_id)
        compiled = self._compile(record.model_reference_json)
        self._conversations.touch(conversation_id, message)
        return await self._runs.run_now(
            compiled,
            message,
            conversation_id=conversation_id,
        )

    def _compile(self, model_reference: dict):
        return replace(
            self._compiler.compile(builder_blueprint(model_reference)),
            completion_validator=validate_builder_completion,
        )


def builder_blueprint(model_reference: dict) -> AgentBlueprint:
    model = ModelReferenceSpec.model_validate(model_reference or {})
    return AgentBlueprint(
        name="ScholarWeave builder",
        description="Builds and revises SDK-native agents and function tools.",
        entry_agent_id="builder",
        agents=[
            {
                "id": "builder",
                "name": "ScholarWeave Builder",
                "description": "Creates SDK-native ScholarWeave agents.",
                "instructions": (
                    "You are ScholarWeave's SDK-native agent builder. Every model turn in this run "
                    "uses your configured model; do not delegate planning or intermediate work to "
                    "another model. First decide whether the user is asking to build or revise an "
                    "agent or function tool. For greetings, questions, and other non-actionable "
                    "messages, respond normally and call finish_builder_run with outcome "
                    "informational; do not create a TODO plan or save an asset. For every actionable "
                    "build or revision request, first call "
                    "create_builder_todo_plan with a short ordered plan that includes inspection "
                    "when needed, blueprint construction, validation, and saving. Work through one "
                    "TODO at a time and call update_builder_todo after actually finishing each step. "
                    "Always call list_sdk_primitives before drafting a blueprint and follow its "
                    "agent_blueprint_schema and examples exactly. Keep instructions, model, "
                    "model_settings, and output inside each agents entry; use kind on tools; put "
                    "max_turns under run. Build only "
                    "with Agent, FunctionTool, Agent.as_tool, Handoff, guardrails, structured output, "
                    "ModelSettings, RunConfig, and Session policy fields represented by the blueprint "
                    "schema. Never invent generic nodes, ports, DAG data edges, conditions, "
                    "map/reduce/repeat steps, or workflow compatibility fields. Validate the complete "
                    "blueprint, then call save_agent_blueprint or save_custom_function_tool. A save "
                    "receipt is the only proof that creation succeeded; proposing JSON or describing "
                    "a design is not completion. Mark the save TODO complete only after receiving the "
                    "receipt, then call finish_builder_run with outcome saved. Never classify an "
                    "actionable build or revision request as informational. "
                    "Explain provider-specific hosted-tool limits and use application FunctionTools "
                    "for papers and workspace access. Keep final responses concise and include what "
                    "was saved."
                ),
                "model": model,
                "model_settings": {"parallel_tool_calls": False},
                "tool_ids": [tool_id for tool_id, _ in BUILDER_TOOL_IDS],
            }
        ],
        tools=[
            {
                "id": tool_id,
                "kind": "function",
                "catalog_id": catalog_id,
            }
            for tool_id, catalog_id in BUILDER_TOOL_IDS
        ],
        run={"max_turns": 24, "max_tool_concurrency": 1},
    )
