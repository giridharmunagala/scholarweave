from __future__ import annotations

import json
from datetime import timedelta

from agents import FunctionTool, Model, ModelResponse, ModelSettings, TResponseInputItem, Usage

from backend.agents.blueprint import AgentBlueprint
from backend.agents.catalog import FunctionToolDefinition, ToolCatalog
from backend.agents.compiler import AgentCompiler
from backend.agents.repository import AgentRepository
from backend.agents.service import AgentService
from backend.core.config import Settings
from backend.core.time import utcnow
from backend.conversations.repository import ConversationRepository
from backend.persistence import create_session_factory
from backend.providers.types import ModelReference, ResolvedAgentModel
from backend.runs.repository import RunRepository
from backend.tools.repository import FunctionToolRepository


class NoopModel(Model):
    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools,
        output_schema,
        handoffs,
        tracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt,
    ) -> ModelResponse:
        return ModelResponse(output=[], usage=Usage(), response_id=None)

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


class Resolver:
    def resolve_agent_model(
        self,
        reference: ModelReference,
        *,
        require_tools: bool = False,
    ) -> ResolvedAgentModel:
        return ResolvedAgentModel(NoopModel(), "test", False, False, False)


def services(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        database_path=tmp_path / "metadata.sqlite3",
    )
    settings.ensure_directories()
    sessions = create_session_factory(settings)
    catalog = ToolCatalog()

    def build(spec) -> FunctionTool:
        async def invoke(_context, arguments: str) -> str:
            return json.dumps(json.loads(arguments))

        return FunctionTool(
            name=spec.name or "echo",
            description=spec.description or "Echo.",
            params_json_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            on_invoke_tool=invoke,
        )

    catalog.register_function_tool(
        FunctionToolDefinition("builtin.echo", "Echo", "Echo.", build)
    )
    return (
        AgentService(AgentRepository(sessions), AgentCompiler(Resolver(), catalog)),
        FunctionToolRepository(sessions),
        ConversationRepository(sessions),
        RunRepository(sessions),
    )


def test_agent_revisions_are_immutable_and_ordered(tmp_path) -> None:
    agents, _, _, _ = services(tmp_path)
    first = AgentBlueprint.model_validate(
        {
            "name": "Researcher",
            "entry_agent_id": "researcher",
            "agents": [
                {
                    "id": "researcher",
                    "name": "Researcher",
                    "instructions": "Research.",
                }
            ],
        }
    )
    created = agents.create(first, presentation={"positions": {"researcher": [0, 0]}})
    updated = agents.update(
        created.record.id,
        first.model_copy(update={"description": "Updated"}),
        presentation={"positions": {"researcher": [100, 100]}},
    )

    revisions = agents.list_revisions(created.record.id)
    assert [revision.revision for revision in revisions] == [2, 1]
    assert revisions[0].blueprint_json["description"] == "Updated"
    assert updated.latest_revision.presentation_json["positions"]["researcher"] == [100, 100]


def test_tool_conversation_and_run_repositories(tmp_path) -> None:
    agents, tools, conversations, runs = services(tmp_path)
    tool = tools.create(
        name="summarize_text",
        description="Summarizes text.",
        parameters_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        output_schema={"type": "string"},
        code="result = arguments['text']",
        requires_approval=False,
    )
    assert tool.revisions[-1].revision == 1

    blueprint = AgentBlueprint.model_validate(
        {
            "name": "Researcher",
            "entry_agent_id": "researcher",
            "agents": [
                {
                    "id": "researcher",
                    "name": "Researcher",
                    "instructions": "Research.",
                }
            ],
        }
    )
    agent = agents.create(blueprint)
    conversation = conversations.create(
        title="Paper chat",
        kind="agent",
        agent_revision_id=agent.latest_revision.id,
        model_reference={},
        session_policy=blueprint.session.model_dump(mode="json"),
    )
    run = runs.create(
        agent_revision_id=agent.latest_revision.id,
        conversation_id=conversation.id,
        agent_name=blueprint.name,
        input_value="Summarize this paper.",
        blueprint=blueprint.model_dump(mode="json", by_alias=True),
    )
    runs.mark_running(run.id)
    runs.add_event(run.id, "run.started", {"agent": blueprint.name})
    runs.add_items(
        run.id,
        [{"type": "message_output_item", "agent_name": "Researcher", "content": "Done"}],
    )
    runs.complete(
        run.id,
        final_output="Done",
        last_agent_name="Researcher",
        usage={"total_tokens": 2},
    )

    saved = runs.get(run.id)
    assert saved.status == "completed"
    assert saved.final_output_json == "Done"
    assert saved.items[0].item_type == "message_output_item"
    assert saved.events[0].event_type == "run.started"
    assert [record.id for record in runs.list(conversation_id=conversation.id)] == [
        run.id
    ]
    assert runs.list(conversation_id="another-conversation") == []


def test_run_repository_bulk_deletes_run_relations(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        database_path=tmp_path / "metadata.sqlite3",
    )
    settings.ensure_directories()
    sessions = create_session_factory(settings)
    runs = RunRepository(sessions)
    old_run = runs.create(
        agent_revision_id=None,
        conversation_id=None,
        agent_name="Old run",
        input_value="old",
        blueprint={},
    )
    current_run = runs.create(
        agent_revision_id=None,
        conversation_id=None,
        agent_name="Current run",
        input_value="current",
        blueprint={},
    )
    runs.add_event(old_run.id, "run.completed", {})
    runs.add_items(
        old_run.id,
        [{"type": "message_output_item", "agent_name": "Old run"}],
    )
    with sessions() as session:
        stored = session.get(type(old_run), old_run.id)
        assert stored is not None
        stored.created_at = utcnow() - timedelta(days=3)
        session.commit()

    expired = runs.ids_created_before(utcnow() - timedelta(days=2))
    runs.delete_many(expired)

    assert expired == [old_run.id]
    assert [record.id for record in runs.list()] == [current_run.id]
