from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from backend.agents.blueprint import AgentBlueprint, SessionPolicySpec
from backend.agents.compiler import AgentCompiler
from backend.agents.harness import ModelBinding
from backend.core.config import Settings
from backend.core.errors import ConflictError, NotFoundError
from backend.runs.events import EventBroker
from backend.persistence import create_session_factory
from backend.providers.types import ModelReference, ResolvedAgentModel
from backend.runs.repository import RunRepository
from backend.runs.service import (
    STOP_AND_ANSWER_BLUEPRINT_DESCRIPTION,
    STOP_AND_ANSWER_PROMPT,
    RunService,
    _restore_pending_steering,
)
from backend.conversations.sessions import ConversationSessionFactory
from backend.conversations.steering import SteeringMessage
from backend.conversations.steering import steering_message_id
from backend.tests.harness_support import FakeClient, stub_binding
from backend.tools.catalog import create_tool_catalog
from backend.tools.failures import (
    record_tool_success,
    restore_tool_failure_state_from_attempts,
    serialize_tool_failure_state,
)


class Resolver:
    def __init__(self, binding) -> None:
        self.binding = binding

    def resolve_agent_model(
        self,
        reference: ModelReference,
        *,
        require_tools: bool = False,
    ) -> ResolvedAgentModel:
        return self.binding


class ToolRuntime:
    def __init__(self) -> None:
        self.calls = []
        self.context_metadata = []

    async def invoke(self, catalog_id, arguments, context):
        self.calls.append((catalog_id, arguments))
        self.context_metadata.append(dict(context.metadata))
        return [{"id": "paper-1", "title": "Paper"}]


class FailingToolRuntime:
    def __init__(self) -> None:
        self.calls = 0

    async def invoke(self, catalog_id, arguments, context):
        self.calls += 1
        raise RuntimeError("Remote page returned HTTP 503.")


class BlockingToolRuntime:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def invoke(self, catalog_id, arguments, context):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class BoundingToolRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.bound_results: list[tuple[str, object]] = []

    async def invoke(self, catalog_id, arguments, context):
        self.calls.append((catalog_id, arguments))
        if catalog_id == "tool.results.read":
            return {"content": "Targeted retained evidence.", "has_more": False}
        return {
            "id": "source-1",
            "url": "https://example.com/source-1",
            "text": "do-not-forward-raw-content " * 1_000,
        }

    async def bound_tool_result(
        self,
        catalog_id,
        result,
        context,
        *,
        max_tokens=None,
    ):
        self.bound_results.append((catalog_id, result))
        return {
            "truncated": True,
            "catalog_id": catalog_id,
            "result_ref": "retained-result",
            "preview": {
                "identifiers": [{"id": "source-1"}],
                "references": ["https://example.com/source-1"],
                "excerpts": [],
            },
        }


def failing_binding() -> ModelBinding:
    return ModelBinding(
        client=FakeClient.failing(RuntimeError("Model connection failed.")),
        model_name="stub-model",
        provider_kind="ollama",
    )


def blocking_binding(started: asyncio.Event) -> ModelBinding:
    return ModelBinding(
        client=FakeClient.blocking(started),
        model_name="stub-model",
        provider_kind="ollama",
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_tool_failure_state_restores_by_logical_call_and_success_resets_it() -> None:
    metadata: dict = {}
    attempts = [
        SimpleNamespace(
            catalog_id="research.sources.acquire",
            tool_call_id="call-1",
            status="failed",
        ),
        SimpleNamespace(
            catalog_id="research.sources.acquire",
            tool_call_id="call-1",
            status="failed",
        ),
        SimpleNamespace(
            catalog_id="research.sources.acquire",
            tool_call_id="call-2",
            status="failed",
        ),
        SimpleNamespace(
            catalog_id="research.sources.acquire",
            tool_call_id="call-3",
            status="unknown_outcome",
        ),
    ]

    restore_tool_failure_state_from_attempts(metadata, attempts)

    assert serialize_tool_failure_state(metadata) == {
        "counts": {"research.sources.acquire": 3},
        "disabled": ["research.sources.acquire"],
    }
    record_tool_success(
        SimpleNamespace(metadata=metadata),
        "research.sources.acquire",
    )
    assert serialize_tool_failure_state(metadata) == {
        "counts": {},
        "disabled": [],
    }


@pytest.mark.anyio
async def test_run_service_persists_sdk_items_events_and_usage(
    tmp_path,
    stub_provider,
) -> None:
    stub_provider.call_tool = "search_research_library"
    stub_provider.tool_arguments = {
        "query": None,
        "document_id": None,
        "limit": 10,
    }
    binding = stub_binding(stub_provider)
    catalog = create_tool_catalog()
    compiled = AgentCompiler(Resolver(binding), catalog).compile(
        AgentBlueprint.model_validate(
            {
                "name": "Researcher",
                "entry_agent_id": "researcher",
                "agents": [
                    {
                        "id": "researcher",
                        "name": "Researcher",
                        "instructions": "Use the paper list tool, then answer.",
                        "tool_ids": ["papers"],
                    }
                ],
                "tools": [
                    {
                        "id": "papers",
                        "kind": "function",
                        "catalog_id": "research.library.search",
                    }
                ],
            }
        )
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        database_path=tmp_path / "metadata.sqlite3",
    )
    settings.ensure_directories()
    repository = RunRepository(create_session_factory(settings))
    tool_runtime = ToolRuntime()
    service = RunService(
        repository,
        ConversationSessionFactory(tmp_path / "sdk-sessions.sqlite3"),
        tool_runtime,
        EventBroker(),
        run_log_dir=settings.data_dir / "run_logs",
    )

    run = await service.run_now(compiled, "List the papers.")

    assert run.status == "completed"
    assert run.final_output_json == "Stub answer."
    assert run.last_agent_name == "Researcher"
    assert "total_tokens" in run.usage_json
    assert any(item.item_type == "tool_call_item" for item in run.items)
    assert any(item.item_type == "tool_call_output_item" for item in run.items)
    assert any(event.event_type == "run.completed" for event in run.events)
    run_log = json.loads(
        (settings.data_dir / "run_logs" / f"{run.id}.json").read_text(encoding="utf-8")
    )
    assert run_log["run"]["status"] == "completed"
    assert run_log["event_counts"]["run.completed"] == 1
    assert run_log["event_counts"]["tool.completed"] == 1
    assert "input" not in run_log["run"]
    assert "final_output" not in run_log["run"]
    assert tool_runtime.calls == [
        (
            "research.library.search",
            {"query": None, "document_id": None, "limit": 10},
        )
    ]
    lifecycle = [
        event
        for event in run.events
        if event.event_type in {"agent.started", "agent.completed"}
    ]
    assert [event.event_type for event in lifecycle] == [
        "agent.started",
        "agent.completed",
    ]
    assert lifecycle[0].payload_json["invocation_id"] == lifecycle[1].payload_json[
        "invocation_id"
    ]
    await binding.client.close()


@pytest.mark.anyio
async def test_failed_completion_preserves_user_message_for_next_turn(
    tmp_path,
    stub_provider,
) -> None:
    binding = stub_binding(stub_provider)
    compiled = AgentCompiler(Resolver(binding), create_tool_catalog()).compile(
        AgentBlueprint.model_validate(
            {
                "name": "Researcher",
                "entry_agent_id": "researcher",
                "agents": [
                    {
                        "id": "researcher",
                        "name": "Researcher",
                        "instructions": "Answer the user.",
                        "tool_ids": [],
                    }
                ],
                "tools": [],
            }
        )
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        database_path=tmp_path / "metadata.sqlite3",
    )
    settings.ensure_directories()
    sessions = ConversationSessionFactory(settings.database_path)
    service = RunService(
        RunRepository(create_session_factory(settings)),
        sessions,
        ToolRuntime(),
        EventBroker(),
        settings=settings,
    )

    first = await service.run_now(
        compiled,
        "First turn.",
        conversation_id="conversation-1",
    )
    assert first.status == "completed"

    def reject_completion(_context) -> None:
        raise ValueError("Completion rejected.")

    rejected = await service.run_now(
        replace(
            compiled,
            completion_validator=reject_completion,
            completion_policy_id="test-rejection",
        ),
        "Second turn must survive.",
        conversation_id="conversation-1",
    )
    assert rejected.status == "failed"

    third = await service.run_now(
        compiled,
        "Third turn.",
        conversation_id="conversation-1",
    )
    assert third.status == "completed"

    third_request_messages = stub_provider.requests[2]["messages"]
    assert any(
        message.get("role") == "user"
        and message.get("content") == "Second turn must survive."
        for message in third_request_messages
    )
    await binding.client.close()


@pytest.mark.anyio
async def test_terminal_model_call_continues_with_queued_steering(
    tmp_path,
    stub_provider,
) -> None:
    stub_provider.stream_delay_seconds = 0.5
    binding = stub_binding(stub_provider)
    compiled = AgentCompiler(Resolver(binding), create_tool_catalog()).compile(
        AgentBlueprint.model_validate(
            {
                "name": "Researcher",
                "entry_agent_id": "researcher",
                "agents": [
                    {
                        "id": "researcher",
                        "name": "Researcher",
                        "instructions": "Answer the user.",
                        "tool_ids": [],
                    }
                ],
                "tools": [],
            }
        )
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        database_path=tmp_path / "metadata.sqlite3",
    )
    settings.ensure_directories()
    service = RunService(
        RunRepository(create_session_factory(settings)),
        ConversationSessionFactory(tmp_path / "sdk-sessions.sqlite3"),
        ToolRuntime(),
        EventBroker(),
        settings=settings,
    )

    pending = service.create(
        compiled,
        "Explain the evidence.",
        conversation_id="conversation-1",
    )
    started = await asyncio.to_thread(stub_provider.request_started.wait, 2)
    assert started is True
    steering = await service.steer(
        pending.id,
        "Stop elaborating and give the conclusion now.",
    )
    for _attempt in range(300):
        completed = service.get(pending.id)
        if completed.status in {"completed", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.01)

    assert completed.status == "completed", completed.error
    assert len(stub_provider.requests) == 2
    assert "Stop elaborating" not in str(stub_provider.requests[0]["messages"])
    assert "Stop elaborating" in str(stub_provider.requests[1]["messages"])
    steering_events = [
        event
        for event in completed.events
        if event.event_type in {"steering.queued", "steering.applied"}
    ]
    assert [event.event_type for event in steering_events] == [
        "steering.queued",
        "steering.applied",
    ]
    assert all(
        event.payload_json["message_id"] == steering.id for event in steering_events
    )
    assert completed.epochs[0].terminal_reason == "steering_continuation"
    await binding.client.close()


@pytest.mark.anyio
async def test_steering_is_not_applied_when_run_budget_is_exhausted(
    tmp_path,
    stub_provider,
) -> None:
    stub_provider.stream_delay_seconds = 0.5
    binding = stub_binding(stub_provider)
    compiled = AgentCompiler(Resolver(binding), create_tool_catalog()).compile(
        AgentBlueprint.model_validate(
            {
                "name": "Researcher",
                "entry_agent_id": "researcher",
                "agents": [
                    {
                        "id": "researcher",
                        "name": "Researcher",
                        "instructions": "Answer the user.",
                        "tool_ids": [],
                    }
                ],
                "tools": [],
                "run": {"max_turns": 1},
            }
        )
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        database_path=tmp_path / "metadata.sqlite3",
    )
    settings.ensure_directories()
    sessions = ConversationSessionFactory(tmp_path / "sdk-sessions.sqlite3")
    service = RunService(
        RunRepository(create_session_factory(settings)),
        sessions,
        ToolRuntime(),
        EventBroker(),
        settings=settings,
    )

    pending = service.create(
        compiled,
        "Explain the evidence.",
        conversation_id="conversation-1",
    )
    assert await asyncio.to_thread(stub_provider.request_started.wait, 2) is True
    steering = await service.steer(pending.id, "Give the conclusion now.")
    for _attempt in range(300):
        completed = service.get(pending.id)
        if completed.status in {"completed", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.01)

    assert completed.status == "failed"
    assert len(stub_provider.requests) == 1
    steering_events = [
        event.event_type
        for event in completed.events
        if event.payload_json.get("message_id") == steering.id
    ]
    assert steering_events == ["steering.queued"]
    session = sessions.get("conversation-1", compiled.blueprint.session)
    assert any(
        item.get("role") == "user"
        and steering_message_id(item) == steering.id
        for item in await session.get_items()
        if isinstance(item, dict)
    )
    await binding.client.close()


def test_pending_steering_is_reconstructed_from_durable_events() -> None:
    inbox = _restore_pending_steering(
        [
            SimpleNamespace(
                event_type="steering.queued",
                payload_json={"message_id": "applied", "content": "First"},
            ),
            SimpleNamespace(
                event_type="steering.applied",
                payload_json={"message_id": "applied", "content": "First"},
            ),
            SimpleNamespace(
                event_type="steering.queued",
                payload_json={"message_id": "pending", "content": "Second"},
            ),
        ]
    )

    assert inbox.take_pending_or_close() == [
        SteeringMessage(id="pending", content="Second")
    ]


@pytest.mark.anyio
async def test_real_tool_loop_receives_bounded_output_and_reads_retained_result(
    tmp_path,
    stub_provider,
) -> None:
    stub_provider.tool_plans = [
        (
            "Gather evidence",
            "search_research_library",
            {"query": None, "document_id": None, "limit": 10},
        ),
        (
            "retained-result",
            "read_tool_result",
            {
                "result_ref": "retained-result",
                "start": 0,
                "max_characters": 2_048,
            },
        ),
    ]
    binding = stub_binding(stub_provider)
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        database_path=tmp_path / "metadata.sqlite3",
        tool_result_max_tokens=512,
    )
    compiled = AgentCompiler(
        Resolver(binding),
        create_tool_catalog(),
        settings=settings,
    ).compile(
        AgentBlueprint.model_validate(
            {
                "name": "Bounded researcher",
                "entry_agent_id": "researcher",
                "agents": [
                    {
                        "id": "researcher",
                        "name": "Researcher",
                        "instructions": "Gather evidence and read retained details.",
                        "tool_ids": ["papers"],
                    }
                ],
                "tools": [
                    {
                        "id": "papers",
                        "kind": "function",
                        "catalog_id": "research.library.search",
                    }
                ],
            }
        )
    )
    settings.ensure_directories()
    repository = RunRepository(create_session_factory(settings))
    tool_runtime = BoundingToolRuntime()
    service = RunService(
        repository,
        ConversationSessionFactory(tmp_path / "sdk-sessions.sqlite3"),
        tool_runtime,
        EventBroker(),
    )

    run = await service.run_now(compiled, "Gather evidence.")

    assert run.status == "completed"
    assert tool_runtime.calls == [
        (
            "research.library.search",
            {"query": None, "document_id": None, "limit": 10},
        ),
        (
            "tool.results.read",
            {
                "result_ref": "retained-result",
                "start": 0,
                "max_characters": 2_048,
            },
        ),
    ]
    assert "read_tool_result" in stub_provider.tools_offered
    second_request = stub_provider.requests[1]
    serialized_messages = str(second_request["messages"])
    assert "retained-result" in serialized_messages
    assert "do-not-forward-raw-content" not in serialized_messages
    completed_tool_events = [
        event.payload_json["result"]
        for event in run.events
        if event.event_type == "tool.completed"
    ]
    assert completed_tool_events[0]["result_ref"] == "retained-result"
    await binding.client.close()


@pytest.mark.anyio
async def test_run_failure_settles_active_agent_invocation(tmp_path) -> None:
    binding = failing_binding()
    compiled = AgentCompiler(Resolver(binding), create_tool_catalog()).compile(
        AgentBlueprint.model_validate(
            {
                "name": "Failing agent",
                "entry_agent_id": "failing",
                "agents": [
                    {
                        "id": "failing",
                        "name": "Failing",
                        "instructions": "Answer.",
                    }
                ],
            }
        )
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        database_path=tmp_path / "metadata.sqlite3",
    )
    settings.ensure_directories()
    repository = RunRepository(create_session_factory(settings))
    service = RunService(
        repository,
        ConversationSessionFactory(tmp_path / "sdk-sessions.sqlite3"),
        ToolRuntime(),
        EventBroker(),
    )

    run = await service.run_now(compiled, "Fail.")

    assert run.status == "failed"
    lifecycle = [
        event
        for event in run.events
        if event.event_type in {"agent.started", "agent.failed"}
    ]
    assert [event.event_type for event in lifecycle] == [
        "agent.started",
        "agent.failed",
    ]
    assert lifecycle[0].payload_json["invocation_id"] == lifecycle[1].payload_json[
        "invocation_id"
    ]


@pytest.mark.anyio
async def test_run_cancellation_supersedes_active_agent_invocation(tmp_path) -> None:
    started = asyncio.Event()
    binding = blocking_binding(started)
    compiled = AgentCompiler(Resolver(binding), create_tool_catalog()).compile(
        AgentBlueprint.model_validate(
            {
                "name": "Blocking agent",
                "entry_agent_id": "blocking",
                "agents": [
                    {
                        "id": "blocking",
                        "name": "Blocking",
                        "instructions": "Wait.",
                    }
                ],
            }
        )
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        database_path=tmp_path / "metadata.sqlite3",
    )
    settings.ensure_directories()
    repository = RunRepository(create_session_factory(settings))
    service = RunService(
        repository,
        ConversationSessionFactory(tmp_path / "sdk-sessions.sqlite3"),
        ToolRuntime(),
        EventBroker(),
    )
    created = service.create(
        compiled,
        "Wait.",
        conversation_id=None,
    )
    await asyncio.wait_for(started.wait(), timeout=2)

    await service.cancel(created.id)
    task = service._tasks.get(created.id)
    if task is not None:
        await asyncio.gather(task, return_exceptions=True)
    run = service.get(created.id)

    assert run.status == "cancelled"
    lifecycle = [
        event
        for event in run.events
        if event.event_type in {"agent.started", "agent.superseded"}
    ]
    assert [event.event_type for event in lifecycle] == [
        "agent.started",
        "agent.superseded",
    ]
    assert lifecycle[0].payload_json["invocation_id"] == lifecycle[1].payload_json[
        "invocation_id"
    ]
    assert lifecycle[1].payload_json["reason"] == "run_cancelled"


@pytest.mark.anyio
async def test_run_service_clear_history_preserves_active_runs(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        database_path=tmp_path / "metadata.sqlite3",
    )
    settings.ensure_directories()
    repository = RunRepository(create_session_factory(settings))
    service = RunService(
        repository,
        ConversationSessionFactory(tmp_path / "sdk-sessions.sqlite3"),
        ToolRuntime(),
        EventBroker(),
    )
    old_run = repository.create(
        conversation_id=None,
        agent_name="Old run",
        input_value="old",
        blueprint={},
    )
    active_run = repository.create(
        conversation_id=None,
        agent_name="Active run",
        input_value="active",
        blueprint={},
    )
    blocker = asyncio.Event()
    active_task = asyncio.create_task(blocker.wait())
    service._tasks[active_run.id] = active_task
    try:
        deleted = service.clear_history()

        assert deleted == 1
        assert repository.get(active_run.id).id == active_run.id
        assert [record.id for record in repository.list()] == [active_run.id]
        assert old_run.id != active_run.id
    finally:
        active_task.cancel()
        await asyncio.gather(active_task, return_exceptions=True)


@pytest.mark.anyio
async def test_run_service_deletes_only_finished_runs(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        database_path=tmp_path / "metadata.sqlite3",
    )
    settings.ensure_directories()
    repository = RunRepository(create_session_factory(settings))
    sessions = ConversationSessionFactory(tmp_path / "sdk-sessions.sqlite3")
    service = RunService(
        repository,
        sessions,
        ToolRuntime(),
        EventBroker(),
    )
    finished = repository.create(
        conversation_id=None,
        agent_name="Finished run",
        input_value="done",
        blueprint={},
    )
    repository.complete(
        finished.id,
        final_output="Done",
        last_agent_name="Researcher",
        usage={},
    )
    pending = repository.create(
        conversation_id=None,
        agent_name="Pending run",
        input_value="waiting",
        blueprint={},
    )
    standalone = sessions.get(
        f"run:{finished.id}",
        SessionPolicySpec(),
    )
    await standalone.add_items([{"role": "user", "content": "delete me"}])

    service.delete(finished.id)

    with pytest.raises(NotFoundError):
        repository.get(finished.id)
    with pytest.raises(ConflictError, match="finished run"):
        service.delete(pending.id)
    assert await sessions.get(
        f"run:{finished.id}",
        SessionPolicySpec(),
    ).get_items() == []


@pytest.mark.anyio
async def test_remote_cancellation_records_intent_without_terminalizing_owner_run(
    tmp_path,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        database_path=tmp_path / "metadata.sqlite3",
    )
    settings.ensure_directories()
    repository = RunRepository(create_session_factory(settings))
    service = RunService(
        repository,
        ConversationSessionFactory(tmp_path / "sdk-sessions.sqlite3"),
        ToolRuntime(),
        EventBroker(),
    )
    run = repository.create(
        conversation_id=None,
        agent_name="Remote owner",
        input_value="work",
        blueprint={},
    )
    remote_lease = repository.claim(run.id, "another-process")
    assert remote_lease is not None
    repository.mark_running_owned(remote_lease)

    result = await service.cancel(run.id)

    assert result.status == "running"
    assert result.cancel_requested is True
    assert not any(event.event_type == "run.cancelled" for event in result.events)
    assert repository.release_claim(
        run.id,
        "another-process",
        generation=remote_lease.generation,
        token=remote_lease.token,
    )


@pytest.mark.anyio
async def test_tool_failure_is_returned_to_model_without_failing_run(
    tmp_path,
    stub_provider,
) -> None:
    stub_provider.call_tool = "acquire_research_source"
    stub_provider.tool_arguments = {
        "kind": "web_page",
        "url": "https://example.com/unavailable",
        "title": None,
    }
    binding = stub_binding(stub_provider)
    compiled = AgentCompiler(Resolver(binding), create_tool_catalog()).compile(
        AgentBlueprint.model_validate(
            {
                "name": "Web researcher",
                "entry_agent_id": "researcher",
                "agents": [
                    {
                        "id": "researcher",
                        "name": "Researcher",
                        "instructions": "Download the page, recover from failure, then answer.",
                        "tool_ids": ["web-page"],
                    }
                ],
                "tools": [
                    {
                        "id": "web-page",
                        "kind": "function",
                        "catalog_id": "research.sources.acquire",
                    }
                ],
            }
        )
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        database_path=tmp_path / "metadata.sqlite3",
    )
    settings.ensure_directories()
    repository = RunRepository(create_session_factory(settings))
    service = RunService(
        repository,
        ConversationSessionFactory(tmp_path / "sdk-sessions.sqlite3"),
        FailingToolRuntime(),
        EventBroker(),
    )

    run = await service.run_now(compiled, "Read this web page.")

    assert run.status == "completed"
    tool_outputs = [
        item.item_json["output"]
        for item in run.items
        if item.item_type == "tool_call_output_item"
    ]
    assert len(tool_outputs) == 1
    assert "try a different tool or source" in tool_outputs[0]
    assert any(event.event_type == "tool.failed" for event in run.events)
    assert not any(event.event_type == "run.failed" for event in run.events)
    await binding.client.close()


@pytest.mark.anyio
async def test_repeated_information_failures_disable_only_the_failing_tool(
    tmp_path,
    stub_provider,
) -> None:
    stub_provider.tool_plans = [
        (
            "Find unavailable evidence",
            "search_research_sources",
            {"provider": "web", "query": f"missing evidence {index}"},
        )
        for index in range(3)
    ]
    binding = stub_binding(stub_provider)
    compiled = AgentCompiler(Resolver(binding), create_tool_catalog()).compile(
        AgentBlueprint.model_validate(
            {
                "name": "Loop-aware researcher",
                "entry_agent_id": "researcher",
                "agents": [
                    {
                        "id": "researcher",
                        "name": "Researcher",
                        "instructions": "Find unavailable evidence, then answer.",
                        "tool_ids": [
                            "web-search",
                            "paper-list",
                            "workspace-write",
                        ],
                    },
                    {
                        "id": "helper",
                        "name": "Helper",
                        "instructions": "Help with research.",
                    },
                ],
                "tools": [
                    {
                        "id": "web-search",
                        "kind": "function",
                        "catalog_id": "research.sources.search",
                    },
                    {
                        "id": "workspace-write",
                        "kind": "function",
                        "catalog_id": "research.notes.save",
                    },
                    {
                        "id": "paper-list",
                        "kind": "function",
                        "catalog_id": "research.library.search",
                    },
                ],
                "agent_tools": [
                    {
                        "id": "helper-tool",
                        "owner_agent_id": "researcher",
                        "delegate_agent_id": "helper",
                        "tool_name": "ask_research_helper",
                        "tool_description": "Delegate research to a helper.",
                    }
                ],
                "run": {"max_turns": 8},
            }
        )
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        database_path=tmp_path / "metadata.sqlite3",
    )
    settings.ensure_directories()
    runtime = FailingToolRuntime()
    service = RunService(
        RunRepository(create_session_factory(settings)),
        ConversationSessionFactory(tmp_path / "sdk-sessions.sqlite3"),
        runtime,
        EventBroker(),
    )

    run = await service.run_now(compiled, "Find unavailable evidence.")

    assert run.status == "completed"
    assert runtime.calls == 3
    outputs = [
        item.item_json["output"]
        for item in run.items
        if item.item_type == "tool_call_output_item"
    ]
    assert len(outputs) == 3
    assert "search_research_sources tool is now disabled" in outputs[-1]
    offered_tools = {
        tool["function"]["name"]
        for tool in stub_provider.requests[-1].get("tools", [])
    }
    assert "search_research_sources" not in offered_tools
    assert "search_research_library" in offered_tools
    assert "save_research_note" in offered_tools
    assert "ask_research_helper" in offered_tools
    failures = [
        event.payload_json
        for event in run.events
        if event.event_type == "tool.failed"
    ]
    assert failures[-1]["failure_limit_reached"] is True
    assert failures[-1]["display_message"] == (
        "This tool was paused after three consecutive failures. The agent will use "
        "another available source or explain what could not be verified."
    )
    await binding.client.close()


@pytest.mark.anyio
async def test_repeated_acquisition_failures_disable_acquire_without_retrying(
    tmp_path,
    stub_provider,
) -> None:
    stub_provider.tool_plans = [
        (
            "Find one unavailable paper",
            "acquire_research_source",
            {
                "kind": "paper",
                "url": f"https://example.com/unavailable-{index}.pdf",
                "title": None,
            },
        )
        for index in range(3)
    ]
    binding = stub_binding(stub_provider)
    compiled = AgentCompiler(Resolver(binding), create_tool_catalog()).compile(
        AgentBlueprint.model_validate(
            {
                "name": "Acquisition-aware researcher",
                "entry_agent_id": "researcher",
                "agents": [
                    {
                        "id": "researcher",
                        "name": "Researcher",
                        "instructions": "Try available sources, then answer.",
                        "tool_ids": ["acquire", "paper-list"],
                    }
                ],
                "tools": [
                    {
                        "id": "acquire",
                        "kind": "function",
                        "catalog_id": "research.sources.acquire",
                    },
                    {
                        "id": "paper-list",
                        "kind": "function",
                        "catalog_id": "research.library.search",
                    },
                ],
                "run": {"max_turns": 8},
            }
        )
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        database_path=tmp_path / "metadata.sqlite3",
    )
    settings.ensure_directories()
    runtime = FailingToolRuntime()
    service = RunService(
        RunRepository(create_session_factory(settings)),
        ConversationSessionFactory(tmp_path / "sdk-sessions.sqlite3"),
        runtime,
        EventBroker(),
    )

    try:
        run = await service.run_now(compiled, "Find one unavailable paper.")

        assert run.status == "completed"
        assert runtime.calls == 3
        outputs = [
            item.item_json["output"]
            for item in run.items
            if item.item_type == "tool_call_output_item"
        ]
        assert "acquire_research_source tool is now disabled" in outputs[-1]
        offered_tools = {
            tool["function"]["name"]
            for tool in stub_provider.requests[-1].get("tools", [])
        }
        assert "acquire_research_source" not in offered_tools
        assert "search_research_library" in offered_tools
    finally:
        await service.close()
    await binding.client.close()


@pytest.mark.anyio
async def test_cancel_immediately_stops_model_stream(
    tmp_path,
    stub_provider,
) -> None:
    stub_provider.stream_delay_seconds = 10
    binding = stub_binding(stub_provider)
    compiled = AgentCompiler(Resolver(binding), create_tool_catalog()).compile(
        AgentBlueprint.model_validate(
            {
                "name": "Researcher",
                "entry_agent_id": "researcher",
                "agents": [
                    {
                        "id": "researcher",
                        "name": "Researcher",
                        "instructions": "Answer.",
                    }
                ],
            }
        )
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        database_path=tmp_path / "metadata.sqlite3",
    )
    settings.ensure_directories()
    service = RunService(
        RunRepository(create_session_factory(settings)),
        ConversationSessionFactory(tmp_path / "sdk-sessions.sqlite3"),
        ToolRuntime(),
        EventBroker(),
    )

    pending = service.create(
        compiled,
        "Explain the evidence.",
        conversation_id="conversation-1",
    )
    for _attempt in range(200):
        if stub_provider.requests:
            break
        await asyncio.sleep(0.01)

    cancelled = await service.cancel(pending.id)

    assert cancelled.status == "cancelled"
    assert pending.id not in service._tasks
    assert any(event.event_type == "run.cancelled" for event in cancelled.events)
    await binding.client.close()


@pytest.mark.anyio
async def test_cancel_immediately_propagates_to_active_tool(
    tmp_path,
    stub_provider,
) -> None:
    stub_provider.call_tool = "search_research_library"
    stub_provider.tool_arguments = {
        "query": None,
        "document_id": None,
        "limit": 10,
    }
    binding = stub_binding(stub_provider)
    compiled = AgentCompiler(Resolver(binding), create_tool_catalog()).compile(
        AgentBlueprint.model_validate(
            {
                "name": "Researcher",
                "entry_agent_id": "researcher",
                "agents": [
                    {
                        "id": "researcher",
                        "name": "Researcher",
                        "instructions": "List documents.",
                        "tool_ids": ["papers"],
                    }
                ],
                "tools": [
                    {
                        "id": "papers",
                        "kind": "function",
                        "catalog_id": "research.library.search",
                    }
                ],
            }
        )
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        database_path=tmp_path / "metadata.sqlite3",
    )
    settings.ensure_directories()
    runtime = BlockingToolRuntime()
    service = RunService(
        RunRepository(create_session_factory(settings)),
        ConversationSessionFactory(tmp_path / "sdk-sessions.sqlite3"),
        runtime,
        EventBroker(),
    )

    pending = service.create(
        compiled,
        "List documents.",
        conversation_id="conversation-1",
    )
    await asyncio.wait_for(runtime.started.wait(), timeout=2)

    cancelled = await service.cancel(pending.id)

    assert cancelled.status == "cancelled"
    assert runtime.cancelled.is_set()
    await binding.client.close()


@pytest.mark.anyio
async def test_stop_and_answer_starts_tool_free_answer_and_hides_internal_prompt(
    tmp_path,
    stub_provider,
) -> None:
    stub_provider.stream_delay_seconds = 10
    binding = stub_binding(stub_provider)
    compiled = AgentCompiler(Resolver(binding), create_tool_catalog()).compile(
        AgentBlueprint.model_validate(
            {
                "name": "Researcher",
                "entry_agent_id": "researcher",
                "agents": [
                    {
                        "id": "researcher",
                        "name": "Researcher",
                        "instructions": "Research carefully.",
                        "tool_ids": ["papers"],
                    }
                ],
                "tools": [
                    {
                        "id": "papers",
                        "kind": "function",
                        "catalog_id": "research.library.search",
                    }
                ],
            }
        )
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        database_path=tmp_path / "metadata.sqlite3",
    )
    settings.ensure_directories()
    sessions = ConversationSessionFactory(tmp_path / "sdk-sessions.sqlite3")
    service = RunService(
        RunRepository(create_session_factory(settings)),
        sessions,
        ToolRuntime(),
        EventBroker(),
    )
    session = sessions.get(
        "conversation-1",
        compiled.blueprint.session,
        compiled.resolved_models[compiled.blueprint.entry_agent_id],
    )
    await session.add_items(
        [
            {"role": "user", "content": "Find every relevant source."},
            {
                "role": "assistant",
                "content": "Earlier answer.",
            },
        ]
    )
    async with sessions.run_lock("conversation-1"):
        pending = service.create(
            compiled,
            "Find every relevant source.",
            conversation_id="conversation-1",
        )
        await asyncio.sleep(0)
        stub_provider.stream_delay_seconds = 0
        stopped, answer_pending = await service.stop_and_answer(pending.id)
    for _attempt in range(200):
        answer = service.get(answer_pending.id)
        if answer.status in {"completed", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.01)

    assert stopped.status == "cancelled"
    assert answer.status == "completed", answer.error
    assert answer.final_output_json == "Stub answer."
    assert stub_provider.requests[-1].get("tools") in (None, [])
    assert "Find every relevant source." in str(
        stub_provider.requests[-1].get("messages")
    )
    assert isinstance(answer.input_json, list)
    assert answer.blueprint_json["description"] == STOP_AND_ANSWER_BLUEPRINT_DESCRIPTION
    assert answer.blueprint_json["tools"] == []
    assert answer.blueprint_json["run"]["max_turns"] == 1
    recovered_answer = AgentCompiler(Resolver(binding), create_tool_catalog()).compile(
        AgentBlueprint.model_validate(answer.blueprint_json)
    )
    assert recovered_answer.max_turns == 1
    assert recovered_answer.entry_agent.tools == []
    session_items = await session.get_items()
    assert all(STOP_AND_ANSWER_PROMPT not in str(item) for item in session_items)
    assert sum(
        "Find every relevant source." in str(item)
        and isinstance(item, dict)
        and item.get("role") == "user"
        for item in session_items
    ) == 2
    assert any(
        isinstance(item, dict) and item.get("role") == "assistant"
        for item in session_items
    )
    await binding.client.close()
