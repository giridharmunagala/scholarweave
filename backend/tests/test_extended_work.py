from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from backend.agents.blueprint import SessionPolicySpec
from backend.agents.instructions import current_system_information
from backend.autonomous.service import FOCUSED_WORKER_TOOL_IDS, autonomous_blueprint
from backend.autonomous.work import (
    create_work_plan,
    list_work_notes,
    save_work_note,
    update_work_item,
)
from backend.bootstrap import create_services
from backend.conversations.memory import ConversationMemoryService
from backend.conversations.repository import ConversationRepository
from backend.core.config import Settings
from backend.persistence import create_session_factory
from backend.runs.repository import RunRepository
from backend.runtime.context import ScholarWeaveContext
from backend.runtime.hooks import ScholarWeaveRunHooks
from backend.runtime.sessions import SdkSessionFactory


class Runtime:
    async def invoke(self, catalog_id, arguments, context):
        raise AssertionError("No application tool should be invoked.")


class EventSink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, payload))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_extended_blueprint_is_sequential_and_context_bounded() -> None:
    blueprint = autonomous_blueprint({}, work_mode="extended")

    assert [agent.id for agent in blueprint.agents] == ["agent", "planner", "worker"]
    assert [tool.tool_name for tool in blueprint.agent_tools] == [
        "plan_extended_work",
        "execute_focused_work",
    ]
    assert blueprint.session == SessionPolicySpec(
        history_max_items=12,
        messages_only=True,
    )
    assert blueprint.run.max_tool_concurrency == 1
    assert all(agent.model_settings.parallel_tool_calls is False for agent in blueprint.agents)
    worker = blueprint.agents[2]
    assert worker.tool_ids == list(FOCUSED_WORKER_TOOL_IDS)
    assert "run-python" not in worker.tool_ids
    assert "save-agent" not in worker.tool_ids


def test_extended_work_plan_and_notes_are_run_scoped() -> None:
    context = ScholarWeaveContext(run_id="run-1", tool_runtime=Runtime())
    created = create_work_plan(
        {
            "tasks": [
                {"id": "evidence", "title": "Collect evidence"},
                {"id": "compare", "title": "Compare results"},
            ]
        },
        context,
    )
    assert [task["status"] for task in created["tasks"]] == ["in_progress", "pending"]

    note = save_work_note(
        {
            "task_id": "evidence",
            "title": "Evidence",
            "summary": "Two sources agree.",
            "content": "Detailed findings.",
            "sources": ["https://example.com/source"],
        },
        context,
    )
    assert note["note_id"] == "evidence-1"
    assert note["content"] == "Detailed findings."
    assert list_work_notes({}, context)["notes"][0]["summary"] == "Two sources agree."

    updated = update_work_item(
        {"id": "evidence", "status": "completed", "summary": "Evidence collected."},
        context,
    )
    assert [task["status"] for task in updated["tasks"]] == ["completed", "in_progress"]


def test_work_note_source_limits_are_enforced_at_runtime() -> None:
    context = ScholarWeaveContext(run_id="run-1", tool_runtime=Runtime())
    create_work_plan(
        {
            "tasks": [
                {"id": "evidence", "title": "Collect evidence"},
                {"id": "compare", "title": "Compare results"},
            ]
        },
        context,
    )

    with pytest.raises(ValueError, match="more than 100 sources"):
        save_work_note(
            {
                "task_id": "evidence",
                "title": "Evidence",
                "summary": "Summary",
                "content": "Findings",
                "sources": [f"https://example.com/{index}" for index in range(101)],
            },
            context,
        )

    with pytest.raises(ValueError, match="between 1 and 2,000 characters"):
        save_work_note(
            {
                "task_id": "evidence",
                "title": "Evidence",
                "summary": "Summary",
                "content": "Findings",
                "sources": [""],
            },
            context,
        )


@pytest.mark.anyio
async def test_extended_work_notes_are_saved_to_the_files_workspace(test_settings) -> None:
    services = create_services(test_settings)
    try:
        runtime = services.runs._tool_runtime
        context = ScholarWeaveContext(run_id="extended-run", tool_runtime=runtime)
        await runtime.invoke(
            "extended.plan.create",
            {
                "tasks": [
                    {"id": "evidence", "title": "Collect evidence"},
                    {"id": "compare", "title": "Compare results"},
                ]
            },
            context,
        )

        result = await runtime.invoke(
            "extended.notes.save",
            {
                "task_id": "evidence",
                "title": "Evidence",
                "summary": "Two sources agree.",
                "content": "Detailed findings.",
                "sources": ["https://example.com/source"],
            },
            context,
        )
        saved = services.workspace.read_file(result["workspace_path"])

        assert result["workspace_path"] == (
            "extended-work-notes/extended-run/evidence-1-Evidence.md"
        )
        assert saved.tags == ("extended-work", "run:extended-run")
        assert saved.note_id is None
        assert saved.content == (
            "# Evidence\n\n"
            "Detailed findings.\n\n"
            "## Sources\n\n"
            "- https://example.com/source\n"
        )
        assert context.receipts[0].href == f"/workspace?path={saved.path}"
    finally:
        await services.close()


@pytest.mark.anyio
async def test_nested_agent_hooks_emit_outputs_to_the_parent_run() -> None:
    sink = EventSink()
    scholar_context = ScholarWeaveContext(
        run_id="run-1",
        tool_runtime=Runtime(),
        event_sink=sink,
    )
    nested_context = SimpleNamespace(context=SimpleNamespace(context=scholar_context))
    agent = SimpleNamespace(name="Focused Work Specialist")
    hooks = ScholarWeaveRunHooks()

    await hooks.on_agent_start(nested_context, agent)  # type: ignore[arg-type]
    await hooks.on_agent_end(nested_context, agent, "Detailed focused findings.")  # type: ignore[arg-type]

    assert sink.events == [
        ("agent.started", {"agent_name": "Focused Work Specialist"}),
        (
            "agent.completed",
            {
                "agent_name": "Focused Work Specialist",
                "output": "Detailed focused findings.",
            },
        ),
    ]


@pytest.mark.anyio
async def test_policy_session_keeps_only_recent_messages(tmp_path) -> None:
    factory = SdkSessionFactory(tmp_path / "sessions.sqlite3")
    policy = SessionPolicySpec(history_max_items=2, messages_only=True)
    session = factory.get("conversation", policy, None)  # type: ignore[arg-type]
    await session.add_items(
        [
            {"role": "user", "content": "first"},
            {"type": "function_call", "name": "tool"},
            {"role": "assistant", "content": "second"},
            {"role": "user", "content": "third"},
        ]
    )

    items = await session.get_items()
    assert [item["content"] for item in items] == ["second", "third"]
    full = factory.get("conversation", SessionPolicySpec(), None)  # type: ignore[arg-type]
    assert len(await full.get_items()) == 3


def test_conversation_memory_finds_clear_prior_match(test_settings: Settings) -> None:
    sessions = create_session_factory(test_settings)
    conversations = ConversationRepository(sessions)
    runs = RunRepository(sessions)
    conversation = conversations.create(
        title="Linear transformer comparison",
        kind="autonomous",
        agent_revision_id=None,
        model_reference={},
        session_policy={},
    )
    run = runs.create(
        agent_revision_id=None,
        conversation_id=conversation.id,
        agent_name="Researcher",
        input_value="Compare Gated DeltaNet and linear attention memory scaling",
        blueprint={},
    )
    runs.complete(
        run.id,
        final_output="Gated DeltaNet improves recurrent-state expressivity.",
        last_agent_name="Researcher",
        usage={},
    )

    memory = ConversationMemoryService(sessions)
    matches = memory.search(
        "Gated DeltaNet linear attention memory scaling",
        exclude_conversation_id=None,
        limit=5,
    )

    assert matches[0]["run_id"] == run.id
    assert matches[0]["match_score"] >= 1
    assert memory.read(run.id)["conversation_title"] == "Linear transformer comparison"


def test_system_information_uses_profile_timezone() -> None:
    at = datetime(2026, 8, 17, 9, 15, tzinfo=ZoneInfo("UTC"))
    information = current_system_information(
        at,
        timezone_name="Asia/Kolkata",
        user_profile="Based in Hyderabad, India.",
    )

    assert "Current time: 14:45:00 IST (UTC+05:30)" in information
    assert "User context: Based in Hyderabad, India." in information
