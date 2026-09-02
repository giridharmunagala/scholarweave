from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from backend.agents.blueprint import SessionPolicySpec
from backend.agents.compiler import UNLIMITED_AGENT_TOOL_TURNS
from backend.agents.instructions import current_system_information
from backend.autonomous.service import (
    EXTERNAL_SEARCH_SAFETY_LIMIT,
    EXTENDED_WORK_BUDGETS,
    FOCUSED_WORKER_TOOL_IDS,
    autonomous_blueprint,
)
from backend.autonomous.work import (
    budget_status,
    consume_tool_safety_limit,
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
from backend.runtime.priorities import (
    ResearchPriorityRequest,
    list_priority_decisions,
    mark_tool_progress,
    prioritizer_enabled,
)
from backend.runtime.sessions import SdkSessionFactory
from backend.tools.failures import consume_tool_failure, nested_agent_failure_handler


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

    assert [agent.id for agent in blueprint.agents] == [
        "agent",
        "planner",
        "prioritizer",
        "worker",
    ]
    assert [tool.tool_name for tool in blueprint.agent_tools] == [
        "plan_extended_work",
        "prioritize_research_work",
        "execute_focused_work",
        "prioritize_research_work",
    ]
    assert blueprint.session == SessionPolicySpec(
        history_max_items=12,
        messages_only=True,
    )
    assert blueprint.run.max_tool_concurrency == 1
    assert all(agent.model_settings.parallel_tool_calls is False for agent in blueprint.agents)
    assert [tool.max_turns for tool in blueprint.agent_tools] == [10, None, 100, None]
    assert blueprint.run.max_turns == 100
    assert UNLIMITED_AGENT_TOOL_TURNS > 1_000_000
    worker = next(agent for agent in blueprint.agents if agent.id == "worker")
    assert worker.tool_ids == list(FOCUSED_WORKER_TOOL_IDS)
    assert "run-python" not in worker.tool_ids
    assert "save-agent" not in worker.tool_ids


@pytest.mark.parametrize(
    ("name", "recommended_tasks", "exploration_targets"),
    [
        ("low", 3, 3),
        ("medium", 6, 5),
        ("high", 10, 8),
    ],
)
def test_extended_work_budget_communicates_soft_scope_guidance(
    name,
    recommended_tasks,
    exploration_targets,
) -> None:
    blueprint = autonomous_blueprint({}, work_mode="extended", work_budget=name)
    planner = next(agent for agent in blueprint.agents if agent.id == "planner")
    prioritizer = next(agent for agent in blueprint.agents if agent.id == "prioritizer")

    assert EXTENDED_WORK_BUDGETS[name].recommended_tasks == recommended_tasks
    assert EXTENDED_WORK_BUDGETS[name].exploration_targets == exploration_targets
    assert planner.output.schema_["properties"]["tasks"]["maxItems"] == 10
    assert [tool.max_turns for tool in blueprint.agent_tools] == [10, None, 100, None]
    assert blueprint.run.max_turns == 100
    coordinator = next(agent for agent in blueprint.agents if agent.id == "agent")
    worker = next(agent for agent in blueprint.agents if agent.id == "worker")
    for instructions in (coordinator.instructions, worker.instructions):
        assert "not preset-capped" in instructions
        assert f"{EXTERNAL_SEARCH_SAFETY_LIMIT}-call cap" in instructions
        assert "extended_work_budget_status" in instructions
    assert "not hard per-tool quotas" in coordinator.instructions
    assert "not a per-tool quota" in worker.instructions
    assert prioritizer.tool_ids == []
    assert prioritizer.output is not None
    allocation = prioritizer.output.schema_["properties"]
    assert allocation["recommended"]["items"]["properties"]["action"]["enum"] == [
        "pursue",
        "continue",
        "merge",
        "replace",
    ]
    assert allocation["recommended"]["items"]["properties"]["effort"]["enum"] == [
        "low",
        "medium",
        "high",
    ]
    priority_tools = [
        tool
        for tool in blueprint.agent_tools
        if tool.tool_name == "prioritize_research_work"
    ]
    assert {tool.owner_agent_id for tool in priority_tools} == {"agent", "worker"}
    assert all(tool.delegate_agent_id == "prioritizer" for tool in priority_tools)
    assert all(tool.max_turns is None for tool in priority_tools)
    assert "isolated LLM sub-agent" in coordinator.instructions
    assert "isolated prioritize_research_work LLM sub-agent" in worker.instructions
    for instructions in (
        coordinator.instructions,
        planner.instructions,
        prioritizer.instructions,
        worker.instructions,
    ):
        assert str(EXTERNAL_SEARCH_SAFETY_LIMIT) in instructions
        assert str(recommended_tasks) in instructions
        assert str(exploration_targets) in instructions
    assert "Do not invoke it for every extended request" in coordinator.instructions
    assert "You are called only to resolve a stated overflow" in prioritizer.instructions
    assert "not for routine research" in worker.instructions
    assert "budget-status" in coordinator.tool_ids
    assert "budget-status" in worker.tool_ids
    assert "list-priority-decisions" in coordinator.tool_ids
    assert "list-priority-decisions" in worker.tool_ids
    assert "intentional scope decisions" in coordinator.instructions
    assert "coordinator does not respawn it" in worker.instructions


def test_external_search_safety_cap_is_shared_but_other_tools_are_unmetered() -> None:
    context = ScholarWeaveContext(
        run_id="run-1",
        tool_runtime=Runtime(),
        metadata={
            "extended_work_budget": {
                "name": "low",
                "recommended_tasks": 3,
                "exploration_targets": 3,
                "safety_limits": {"external_searches": 2},
            }
        },
    )

    consume_tool_safety_limit("web.search", context)
    consume_tool_safety_limit("arxiv.search", context)
    consume_tool_safety_limit("workspace.search", context)
    consume_tool_safety_limit("workspace.read", context)
    consume_tool_safety_limit("documents.read_pages", context)

    status = budget_status({}, context)
    assert status["safety_limits"]["external_searches"] == {
        "used": 2,
        "limit": 2,
        "remaining": 0,
    }
    assert context.metadata["extended_work_safety_usage"] == {
        "external_searches": 2,
    }
    with pytest.raises(ValueError, match="safety cap for external searches"):
        consume_tool_safety_limit("wikipedia.search", context)


def test_reprioritizer_requires_an_actual_limit_failure() -> None:
    common = {
        "objective": "Choose the smallest sufficient evidence set.",
        "limit_status": "Medium scope allows five source targets.",
        "current_state": "Three sources have already been retained.",
        "evidence_summary": "The retained sources cover the main claim.",
        "candidates": ["Source A", "Source B", "Source C", "Source D", "Source E"],
        "constraints": ["Prefer primary evidence."],
    }

    with pytest.raises(ValidationError, match="observed work to exceed"):
        ResearchPriorityRequest(
            **common,
            trigger="source_limit_exceeded",
            threshold=5,
            observed=5,
        )
    accepted = ResearchPriorityRequest(
        **common,
        trigger="source_limit_exceeded",
        threshold=5,
        observed=6,
    )
    assert accepted.observed == 6
    with pytest.raises(ValidationError, match="300-call cap"):
        ResearchPriorityRequest(
            **common,
            trigger="external_search_cap_reached",
            threshold=299,
            observed=299,
        )


def test_low_budget_can_plan_more_candidates_than_it_recommends() -> None:
    context = ScholarWeaveContext(run_id="run-1", tool_runtime=Runtime())
    created = create_work_plan(
        {
            "tasks": [
                {"id": str(index), "title": f"Task {index}"}
                for index in range(4)
            ]
        },
        context,
    )

    assert len(created["tasks"]) == 4


def test_extended_work_plan_and_notes_are_run_scoped() -> None:
    context = ScholarWeaveContext(run_id="run-1", tool_runtime=Runtime())
    created = create_work_plan(
        {
            "tasks": [
                {
                    "id": "evidence",
                    "title": "Collect evidence",
                    "instructions": "Compare primary sources.",
                    "expected_output": "Evidence table.",
                    "effort": "high",
                    "source_target": 4,
                    "rationale": "This resolves the central uncertainty.",
                },
                {"id": "compare", "title": "Compare results"},
            ]
        },
        context,
    )
    assert [task["status"] for task in created["tasks"]] == ["in_progress", "pending"]
    assert created["tasks"][0]["effort"] == "high"
    assert created["tasks"][0]["source_target"] == 4

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
        metadata={
            "extended_work_budget": {
                "name": "medium",
                "recommended_tasks": 6,
                "exploration_targets": 5,
                "safety_limits": {"external_searches": 300},
            }
        },
    )
    nested_context = SimpleNamespace(context=SimpleNamespace(context=scholar_context))
    agent = SimpleNamespace(name="Focused Work Specialist")
    hooks = ScholarWeaveRunHooks()

    await hooks.on_agent_start(nested_context, agent)  # type: ignore[arg-type]
    await hooks.on_agent_end(nested_context, agent, "Detailed focused findings.")  # type: ignore[arg-type]

    assert scholar_context.metadata["extended_work_progress_revision"] == 1
    assert [event_type for event_type, _ in sink.events] == [
        "agent.started",
        "agent.completed",
    ]
    assert sink.events[0][1]["agent_name"] == "Focused Work Specialist"
    assert sink.events[0][1]["invocation_id"] == sink.events[1][1]["invocation_id"]
    assert sink.events[1][1]["output"] == "Detailed focused findings."


@pytest.mark.anyio
async def test_prioritizer_decisions_are_shared_with_the_coordinator() -> None:
    sink = EventSink()
    scholar_context = ScholarWeaveContext(
        run_id="run-1",
        tool_runtime=Runtime(),
        event_sink=sink,
        metadata={
            "extended_work_budget": {
                "name": "medium",
                "recommended_tasks": 6,
                "exploration_targets": 5,
                "safety_limits": {"external_searches": 300},
            }
        },
    )
    nested_context = SimpleNamespace(context=SimpleNamespace(context=scholar_context))
    agent = SimpleNamespace(name="Research Work Prioritizer")
    output = {
        "summary": "Stop the duplicate benchmark search.",
        "recommended": [
            {
                "id": "primary-paper",
                "title": "Read the primary paper",
                "kind": "paper",
                "action": "continue",
                "effort": "high",
                "source_target": 2,
                "rationale": "It directly answers the question.",
                "instructions": "Extract the reported benchmark and caveats.",
                "expected_output": "A cited comparison.",
            }
        ],
        "deferred": [
            {
                "id": "duplicate-search",
                "title": "Search more benchmark summaries",
                "reason": "Existing primary evidence makes this duplicative.",
            }
        ],
        "stop_conditions": ["Do not reopen duplicate-search without conflicting evidence."],
    }

    await ScholarWeaveRunHooks().on_agent_end(  # type: ignore[arg-type]
        nested_context,
        agent,
        output,
    )

    shared = list_priority_decisions({}, scholar_context)
    assert shared["decisions"][0]["decision_id"] == "priority-1"
    assert shared["decisions"][0]["deferred"][0]["id"] == "duplicate-search"
    assert shared["decisions"][0]["progress_revision"] == 0
    assert shared["decisions"][0]["confirmation_count"] == 1
    assert "not a failure or missing result" in shared["guidance"]
    assert prioritizer_enabled(nested_context, agent) is False
    mark_tool_progress("extended.priorities.list", scholar_context)
    assert prioritizer_enabled(nested_context, agent) is False
    mark_tool_progress("web.search", scholar_context)
    assert prioritizer_enabled(nested_context, agent) is True
    assert [event_type for event_type, _ in sink.events] == [
        "extended.priorities.updated",
        "agent.completed",
    ]
    assert sink.events[0][1] == {
        "decision_id": "priority-1",
        **output,
        "progress_revision": 0,
        "confirmation_count": 1,
    }
    assert sink.events[1][1]["agent_name"] == "Research Work Prioritizer"
    assert sink.events[1][1]["output"] == output
    assert sink.events[1][1]["invocation_id"]

    await ScholarWeaveRunHooks().on_agent_end(  # type: ignore[arg-type]
        nested_context,
        agent,
        output,
    )
    decisions = list_priority_decisions({}, scholar_context)["decisions"]
    assert len(decisions) == 1
    assert decisions[0]["progress_revision"] == 1
    assert decisions[0]["confirmation_count"] == 2
    assert prioritizer_enabled(nested_context, agent) is False


@pytest.mark.anyio
async def test_failed_nested_agent_exposes_partial_state_for_continuation() -> None:
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
    save_work_note(
        {
            "task_id": "evidence",
            "title": "Evidence checkpoint",
            "summary": "Two primary sources were found.",
            "content": "Detailed content should remain in the note.",
            "sources": ["https://example.com/source"],
        },
        context,
    )
    tool_context = SimpleNamespace(
        context=context,
        tool_call=SimpleNamespace(call_id="call-1"),
    )

    message = await nested_agent_failure_handler(
        "execute_focused_work",
        "Focused Work Specialist",
    )(
        tool_context,
        RuntimeError("provider disconnected"),
    )

    assert "Two primary sources were found." in message
    assert "Detailed content should remain in the note." not in message
    assert "delegate only the unfinished scope to a fresh sub-agent" in message
    failure = consume_tool_failure(tool_context, "execute_focused_work")
    assert failure is not None
    assert failure["error_type"] == "RuntimeError"
    assert failure["error"] == "provider disconnected"


@pytest.mark.anyio
async def test_policy_session_keeps_only_recent_messages(tmp_path) -> None:
    factory = SdkSessionFactory(tmp_path / "sessions.sqlite3")
    policy = SessionPolicySpec(history_max_items=2, messages_only=True)
    session = factory.get("conversation", policy)
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
    full = factory.get("conversation", SessionPolicySpec())
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
