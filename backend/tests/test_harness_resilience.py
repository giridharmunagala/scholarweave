from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from dataclasses import replace

import pytest

from backend.agents.blueprint import AgentBlueprint
from backend.agents.compiler import AgentCompiler
from backend.core.config import Settings
from backend.utils import utcnow
from backend.persistence import create_session_factory
from backend.providers.types import ModelReference, ResolvedAgentModel
from backend.runs.events import EventBroker
from backend.runs.repository import RunRepository
from backend.runs.service import LeaseDeadline, RunService, _continuation_instruction
from backend.agents.context import ScholarWeaveContext
from backend.conversations.sessions import ConversationSessionFactory
from backend.providers.inference import InferenceScheduler
from backend.tests.harness_support import stub_binding
from backend.tools.catalog import create_tool_catalog


def test_continuation_instruction_includes_durable_goal_state() -> None:
    instruction = _continuation_instruction(
        "run-1",
        2,
        {"status": "working", "completed_steps": ["search"]},
    )

    assert '"status":"working"' in instruction
    assert '"completed_steps":["search"]' in instruction


class _Resolver:
    def __init__(self, model) -> None:
        self.model = model

    def resolve_agent_model(
        self,
        _reference: ModelReference,
        *,
        require_tools: bool = False,
    ) -> ResolvedAgentModel:
        return self.model


class _ToolRuntime:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def invoke(self, catalog_id, _arguments, _context):
        self.calls.append(catalog_id)
        return {"documents": []}


def _compiled(model, settings: Settings, *, max_turns: int = 20):
    return AgentCompiler(
        _Resolver(model),
        create_tool_catalog(),
        settings=settings,
    ).compile(
        AgentBlueprint.model_validate(
            {
                "name": "Durable researcher",
                "entry_agent_id": "researcher",
                "agents": [
                    {
                        "id": "researcher",
                        "name": "Researcher",
                        "instructions": "Use tools as needed, then finish the research.",
                        "tool_ids": ["documents"],
                    }
                ],
                "tools": [
                    {
                        "id": "documents",
                        "kind": "function",
                        "catalog_id": "research.library.search",
                    }
                ],
                "run": {"max_turns": max_turns},
            }
        )
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_supervisor_continues_across_bounded_epochs(tmp_path, stub_provider) -> None:
    stub_provider.tool_plans = [
        (
            "multi epoch",
            "search_research_library",
            {"query": None, "document_id": None, "limit": 10},
        ),
        (
            "multi epoch",
            "search_research_library",
            {"query": None, "document_id": None, "limit": 10},
        ),
    ]
    model = stub_binding(stub_provider, local_inference=True)
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        database_path=tmp_path / "metadata.sqlite3",
        agent_epoch_max_turns=2,
        agent_max_epochs=4,
    )
    settings.ensure_directories()
    repository = RunRepository(create_session_factory(settings))
    sessions = ConversationSessionFactory(tmp_path / "sdk-sessions.sqlite3")
    runtime = _ToolRuntime()
    service = RunService(
        repository,
        sessions,
        runtime,
        EventBroker(),
        settings=settings,
    )

    run = await service.run_now(
        _compiled(model, settings),
        "Research this multi epoch question.",
        conversation_id="conversation-1",
    )

    assert run.status == "completed", run.error
    assert [epoch.terminal_reason for epoch in run.epochs] == [
        "turn_boundary",
        "goal_completed",
    ]
    assert runtime.calls == ["research.library.search", "research.library.search"]
    assert any(event.event_type == "run.epoch.completed" for event in run.events)
    await model.client.close()


@pytest.mark.anyio
async def test_startup_recovery_abandons_interrupted_epoch(tmp_path, stub_provider) -> None:
    model = stub_binding(stub_provider, local_inference=True)
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        database_path=tmp_path / "metadata.sqlite3",
    )
    settings.ensure_directories()
    repository = RunRepository(create_session_factory(settings))
    compiled = _compiled(model, settings)
    record = repository.create(
        conversation_id="conversation-1",
        agent_name=compiled.blueprint.name,
        input_value="Original goal",
        blueprint=compiled.blueprint.model_dump(mode="json", by_alias=True),
    )
    repository.mark_running(record.id)
    repository.begin_epoch(record.id, "Original goal")
    invalid = repository.create(
        conversation_id=None,
        agent_name="Invalid recovery",
        input_value="Broken",
        blueprint={"not": "a blueprint"},
    )
    service = RunService(
        repository,
        ConversationSessionFactory(tmp_path / "sdk-sessions.sqlite3"),
        _ToolRuntime(),
        EventBroker(),
        settings=settings,
    )

    await service.recover_incomplete(
        AgentCompiler(_Resolver(model), create_tool_catalog(), settings=settings)
    )
    for _ in range(200):
        recovered = service.get(record.id)
        if recovered.status in {"completed", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.01)

    assert recovered.status == "completed", recovered.error
    assert recovered.epochs[0].status == "abandoned"
    assert any(event.event_type == "run.recovered" for event in recovered.events)
    assert service.get(invalid.id).status == "failed"
    await service.close()
    await model.client.close()


@pytest.mark.anyio
async def test_recovery_retries_live_claim_without_mutating_or_duplicate_scheduling(
    tmp_path,
    stub_provider,
    monkeypatch,
) -> None:
    model = stub_binding(stub_provider, local_inference=True)
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        database_path=tmp_path / "metadata.sqlite3",
    )
    settings.ensure_directories()
    repository = RunRepository(create_session_factory(settings))
    compiled = _compiled(model, settings)
    record = repository.create(
        conversation_id=None,
        agent_name=compiled.blueprint.name,
        input_value="Original goal",
        blueprint=compiled.blueprint.model_dump(mode="json", by_alias=True),
    )
    repository.mark_running(record.id)
    repository.begin_epoch(record.id, "Original goal")
    assert repository.claim(
        record.id,
        "crashed-owner",
        now=utcnow(),
        lease_seconds=0.15,
    )
    service = RunService(
        repository,
        ConversationSessionFactory(tmp_path / "sdk-sessions.sqlite3"),
        _ToolRuntime(),
        EventBroker(),
        settings=settings,
    )
    service._RECOVERY_RETRY_SECONDS = 0.01
    service._CLAIM_LEASE_SECONDS = 0.2
    service._CLAIM_HEARTBEAT_SECONDS = 0.04
    executions: list[str] = []

    async def finish_recovery(run_id, *_args, **_kwargs) -> None:
        executions.append(run_id)
        repository.complete(
            run_id,
            final_output="Recovered",
            last_agent_name="Researcher",
            usage={},
        )

    monkeypatch.setattr(service, "_execute_claimed_run", finish_recovery)
    compiler = AgentCompiler(
        _Resolver(model),
        create_tool_catalog(),
        settings=settings,
    )

    await service.recover_incomplete(compiler)
    blocked = repository.get(record.id)
    assert blocked.status == "running"
    assert blocked.epochs[0].status == "running"
    assert record.id not in service._tasks

    for _ in range(100):
        recovered = repository.get(record.id)
        if recovered.status == "completed":
            break
        await asyncio.sleep(0.01)

    assert recovered.status == "completed"
    assert recovered.epochs[0].status == "abandoned"
    await asyncio.sleep(0.05)
    assert executions == [record.id]
    await service.close()
    await model.client.close()


@pytest.mark.anyio
async def test_heartbeat_retries_errors_then_cancels_without_releasing_claim(
    tmp_path,
    stub_provider,
    monkeypatch,
    caplog,
) -> None:
    model = stub_binding(stub_provider, local_inference=True)
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
        _ToolRuntime(),
        EventBroker(),
        settings=settings,
    )
    service._CLAIM_LEASE_SECONDS = 0.12
    service._CLAIM_HEARTBEAT_SECONDS = 0.01
    service._CLAIM_RETRY_SECONDS = 0.01
    service._CLAIM_SAFETY_MARGIN_SECONDS = 0.03
    renew_attempts = 0
    releases: list[tuple[str, str]] = []
    execution_cancelled = asyncio.Event()
    original_release = repository.release_claim

    def fail_renewal(*_args, **_kwargs) -> bool:
        nonlocal renew_attempts
        renew_attempts += 1
        raise RuntimeError("database temporarily unavailable")

    def track_release(
        run_id: str,
        owner_id: str,
        *,
        generation: int,
        token: str,
    ) -> bool:
        releases.append((run_id, owner_id))
        return original_release(
            run_id,
            owner_id,
            generation=generation,
            token=token,
        )

    async def wait_for_cancellation(run_id, *_args, **_kwargs) -> None:
        repository.mark_running(run_id)
        try:
            await asyncio.Event().wait()
        finally:
            execution_cancelled.set()

    monkeypatch.setattr(repository, "renew_claim", fail_renewal)
    monkeypatch.setattr(repository, "release_claim", track_release)
    monkeypatch.setattr(service, "_execute_claimed_run", wait_for_cancellation)
    run = service.create(
        _compiled(model, settings),
        "Lease-sensitive work",
        conversation_id=None,
    )
    durable_session = sessions.get(
        f"run:{run.id}",
        _compiled(model, settings).blueprint.session,
    )
    await durable_session.add_items(
        [{"role": "user", "content": "Preserve after lease loss"}]
    )

    await asyncio.wait_for(execution_cancelled.wait(), timeout=1)
    for _ in range(100):
        if run.id not in service._tasks:
            break
        await asyncio.sleep(0.005)

    current = repository.get(run.id)
    assert renew_attempts >= 2
    assert "claim renewal failed; retrying" in caplog.text
    assert current.status == "running"
    assert not any(event.event_type == "run.failed" for event in current.events)
    assert releases == []
    assert await durable_session.get_items() == [
        {"role": "user", "content": "Preserve after lease loss"}
    ]
    for _ in range(100):
        replacement = repository.claim(
            run.id,
            "replacement-owner",
            lease_seconds=1,
        )
        if replacement is not None:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("Expired lost lease was not available to a replacement owner.")
    assert original_release(
        run.id,
        "replacement-owner",
        generation=replacement.generation,
        token=replacement.token,
    )
    await service.close()
    await model.client.close()


@pytest.mark.anyio
async def test_slow_renewal_cancels_execution_before_real_lease_safety_expires(
    tmp_path,
    stub_provider,
    monkeypatch,
) -> None:
    model = stub_binding(stub_provider, local_inference=True)
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
        _ToolRuntime(),
        EventBroker(),
        settings=settings,
    )
    service._CLAIM_LEASE_SECONDS = 0.08
    service._CLAIM_HEARTBEAT_SECONDS = 0.01
    service._CLAIM_SAFETY_MARGIN_SECONDS = 0.01
    original_renew = repository.renew_claim
    execution_cancelled = asyncio.Event()

    def delayed_renew(*args, **kwargs):
        lease = original_renew(*args, **kwargs)
        time.sleep(0.09)
        return lease

    async def wait_for_cancellation(*_args, **_kwargs) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            execution_cancelled.set()

    monkeypatch.setattr(repository, "renew_claim", delayed_renew)
    monkeypatch.setattr(service, "_execute_claimed_run", wait_for_cancellation)
    run = service.create(
        _compiled(model, settings),
        "Lease-sensitive work",
        conversation_id=None,
    )

    await asyncio.wait_for(execution_cancelled.wait(), timeout=1)
    assert repository.get(run.id).status == "pending"
    assert not repository.get(run.id).events
    for _ in range(100):
        replacement = repository.claim(run.id, "replacement", lease_seconds=1)
        if replacement is not None:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("Slow renewal allowed execution beyond the real lease.")
    assert repository.release_claim(
        run.id,
        "replacement",
        generation=replacement.generation,
        token=replacement.token,
    )
    await service.close()
    await model.client.close()


@pytest.mark.anyio
async def test_cancelled_claim_acquisition_releases_commit_that_finishes_late(
    tmp_path,
    stub_provider,
    monkeypatch,
) -> None:
    model = stub_binding(stub_provider, local_inference=True)
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
        _ToolRuntime(),
        EventBroker(),
        settings=settings,
    )
    entered = threading.Event()
    proceed = threading.Event()
    original_claim = repository.claim

    def delayed_claim(*args, **kwargs):
        entered.set()
        assert proceed.wait(timeout=1)
        return original_claim(*args, **kwargs)

    monkeypatch.setattr(repository, "claim", delayed_claim)
    run = service.create(
        _compiled(model, settings),
        "Cancel during claim",
        conversation_id=None,
    )
    assert await asyncio.to_thread(entered.wait, 1)
    run_task = service._tasks[run.id]
    cancel_task = asyncio.create_task(service.cancel(run.id))
    await asyncio.sleep(0)
    run_task.cancel()
    proceed.set()
    cancelled = await asyncio.wait_for(cancel_task, timeout=1)

    assert cancelled.status == "cancelled"
    replacement = original_claim(run.id, "replacement", lease_seconds=1)
    assert replacement is not None
    assert repository.release_claim(
        run.id,
        "replacement",
        generation=replacement.generation,
        token=replacement.token,
    )
    await service.close()
    await model.client.close()


@pytest.mark.anyio
async def test_repeated_cancellation_cannot_strand_delayed_claim(
    tmp_path,
    monkeypatch,
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
        _ToolRuntime(),
        EventBroker(),
        settings=settings,
    )
    run = repository.create(
        conversation_id=None,
        agent_name="Delayed claim",
        input_value="work",
        blueprint={},
    )
    entered = threading.Event()
    proceed = threading.Event()
    original_claim = repository.claim

    def delayed_claim(*args, **kwargs):
        entered.set()
        assert proceed.wait(timeout=1)
        return original_claim(*args, **kwargs)

    monkeypatch.setattr(repository, "claim", delayed_claim)
    acquisition = asyncio.create_task(service._acquire_claim(run.id))
    assert await asyncio.to_thread(entered.wait, 1)
    acquisition.cancel()
    acquisition.cancel()
    with pytest.raises(asyncio.CancelledError):
        await acquisition

    proceed.set()
    await service._wait_for_claim_cleanups(run.id)
    assert run.id not in service._active_leases
    replacement = original_claim(run.id, "replacement", lease_seconds=1)
    assert replacement is not None
    assert repository.release_claim(
        run.id,
        replacement.owner_id,
        generation=replacement.generation,
        token=replacement.token,
    )
    await service.close()


@pytest.mark.anyio
async def test_close_drains_cancelled_claim_acquisition_cleanup(
    tmp_path,
    monkeypatch,
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
        _ToolRuntime(),
        EventBroker(),
        settings=settings,
    )
    run = repository.create(
        conversation_id=None,
        agent_name="Delayed claim",
        input_value="work",
        blueprint={},
    )
    entered = threading.Event()
    proceed = threading.Event()
    original_claim = repository.claim

    def delayed_claim(*args, **kwargs):
        entered.set()
        assert proceed.wait(timeout=1)
        return original_claim(*args, **kwargs)

    monkeypatch.setattr(repository, "claim", delayed_claim)
    acquisition = asyncio.create_task(service._acquire_claim(run.id))
    assert await asyncio.to_thread(entered.wait, 1)
    acquisition.cancel()
    with pytest.raises(asyncio.CancelledError):
        await acquisition

    proceed.set()
    await service.close()

    replacement = original_claim(run.id, "replacement", lease_seconds=1)
    assert replacement is not None
    assert repository.release_claim(
        run.id,
        replacement.owner_id,
        generation=replacement.generation,
        token=replacement.token,
    )


@pytest.mark.anyio
async def test_cancelled_recovery_handoff_releases_preclaim(
    tmp_path,
    stub_provider,
) -> None:
    model = stub_binding(stub_provider, local_inference=True)
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
        _ToolRuntime(),
        EventBroker(),
        settings=settings,
    )
    compiled = _compiled(model, settings)
    run = repository.create(
        conversation_id=None,
        agent_name=compiled.blueprint.name,
        input_value="recover",
        blueprint=compiled.blueprint.model_dump(mode="json", by_alias=True),
    )
    lease = repository.claim(run.id, service._owner_id, lease_seconds=10)
    assert lease is not None
    service._active_leases[run.id] = lease
    previous = asyncio.create_task(asyncio.Event().wait())
    service._tasks[run.id] = previous
    service._schedule(
        run.id,
        compiled,
        "recover",
        conversation_id=None,
        preclaimed=True,
        lease_deadline=LeaseDeadline(
            lease,
            asyncio.get_running_loop().time() + lease.duration_seconds,
        ),
    )
    handoff = service._tasks[run.id]
    handoff.cancel()
    await asyncio.gather(handoff, return_exceptions=True)
    await asyncio.sleep(0)
    await service._wait_for_claim_cleanups(run.id)
    assert run.id not in service._active_leases

    replacement = repository.claim(run.id, "replacement", lease_seconds=1)
    assert replacement is not None
    assert repository.release_claim(
        run.id,
        "replacement",
        generation=replacement.generation,
        token=replacement.token,
    )
    previous.cancel()
    await asyncio.gather(previous, return_exceptions=True)
    await service.close()
    await model.client.close()


@pytest.mark.anyio
async def test_expired_preclaim_is_rejected_before_execution(
    tmp_path,
    stub_provider,
    monkeypatch,
) -> None:
    model = stub_binding(stub_provider, local_inference=True)
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
        _ToolRuntime(),
        EventBroker(),
        settings=settings,
    )
    compiled = _compiled(model, settings)
    run = repository.create(
        conversation_id=None,
        agent_name=compiled.blueprint.name,
        input_value="recover",
        blueprint=compiled.blueprint.model_dump(mode="json", by_alias=True),
    )
    started_at = asyncio.get_running_loop().time()
    lease = repository.claim(run.id, service._owner_id, lease_seconds=0.02)
    assert lease is not None
    service._active_leases[run.id] = lease
    executed = False

    async def unexpected_execution(*_args, **_kwargs) -> None:
        nonlocal executed
        executed = True

    monkeypatch.setattr(service, "_execute_owned_run", unexpected_execution)
    await asyncio.sleep(0.03)
    await service._execute_run(
        run.id,
        compiled,
        "recover",
        conversation_id=None,
        deadline=asyncio.get_running_loop().time() + 1,
        preclaimed=True,
        lease_deadline=LeaseDeadline(
            lease,
            started_at + lease.duration_seconds,
        ),
    )

    assert executed is False
    replacement = repository.claim(run.id, "replacement", lease_seconds=1)
    assert replacement is not None
    assert repository.release_claim(
        run.id,
        "replacement",
        generation=replacement.generation,
        token=replacement.token,
    )
    await service.close()
    await model.client.close()


@pytest.mark.anyio
async def test_standalone_recovery_restores_execution_contract_and_sdk_session(
    tmp_path,
    stub_provider,
) -> None:
    model = stub_binding(stub_provider, local_inference=True)
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        database_path=tmp_path / "metadata.sqlite3",
    )
    settings.ensure_directories()
    repository = RunRepository(create_session_factory(settings))
    compiled = _compiled(model, settings)
    metadata = {
        "paper_activity": [
            {"document_id": "paper-1", "title": "Paper", "action": "read"},
            {
                "document_id": "paper-1",
                "title": "Paper",
                "action": "summary_saved",
            },
            {
                "document_id": "paper-1",
                "title": "Paper",
                "action": "notes_saved",
            },
        ],
    }
    record = repository.create(
        conversation_id=None,
        agent_name=compiled.blueprint.name,
        input_value="Summarize the paper",
        blueprint=compiled.blueprint.model_dump(mode="json", by_alias=True),
        context_window_tokens=777,
        runtime_metadata=metadata,
        completion_policy_id="paper-work-v1",
    )
    repository.mark_running(record.id)
    repository.begin_epoch(record.id, "Summarize the paper")
    sessions = ConversationSessionFactory(tmp_path / "sdk-sessions.sqlite3")
    durable_session = sessions.get(f"run:{record.id}", compiled.blueprint.session)
    await durable_session.add_items(
        [{"role": "user", "content": "Original standalone summary request"}]
    )
    validated: list[dict] = []

    def validate(context: ScholarWeaveContext) -> None:
        validated.append(context.metadata)
        assert context.metadata["paper_activity"] == metadata["paper_activity"]

    service = RunService(
        repository,
        sessions,
        _ToolRuntime(),
        EventBroker(),
        settings=settings,
        completion_validators={"paper-work-v1": validate},
    )
    await service.recover_incomplete(
        AgentCompiler(_Resolver(model), create_tool_catalog(), settings=settings)
    )
    for _ in range(200):
        recovered = service.get(record.id)
        if recovered.status in {"completed", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.01)

    assert recovered.status == "completed", recovered.error
    assert recovered.context_window_tokens == 777
    assert recovered.completion_policy_id == "paper-work-v1"
    assert recovered.runtime_metadata_json == metadata
    assert validated
    assert any(
        message.get("content") == "Original standalone summary request"
        for request in stub_provider.requests
        for message in request.get("messages", [])
    )
    for _ in range(100):
        with sqlite3.connect(tmp_path / "sdk-sessions.sqlite3") as connection:
            session_count = connection.execute(
                "SELECT COUNT(*) FROM sdk_sessions WHERE session_id = ?",
                (f"run:{record.id}",),
            ).fetchone()
            item_count = connection.execute(
                "SELECT COUNT(*) FROM sdk_session_items WHERE session_id = ?",
                (f"run:{record.id}",),
            ).fetchone()
        if session_count == (0,) and item_count == (0,):
            break
        await asyncio.sleep(0.01)
    assert session_count == (0,)
    assert item_count == (0,)
    await service.close()
    await model.client.close()


@pytest.mark.anyio
async def test_run_deadline_includes_waiting_for_exclusive_local_inference(
    tmp_path,
    stub_provider,
) -> None:
    model = stub_binding(stub_provider, local_inference=True)
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        database_path=tmp_path / "metadata.sqlite3",
    )
    settings.agent_run_timeout_seconds = 0.05
    settings.ensure_directories()
    scheduler = InferenceScheduler()
    repository = RunRepository(create_session_factory(settings))
    service = RunService(
        repository,
        ConversationSessionFactory(tmp_path / "sdk-sessions.sqlite3"),
        _ToolRuntime(),
        EventBroker(),
        settings=settings,
        inference_scheduler=scheduler,
    )
    compiled = _compiled(model, settings)
    compiled = replace(
        compiled,
        blueprint=compiled.blueprint.model_copy(
            update={
                "run": compiled.blueprint.run.model_copy(
                    update={"exclusive_inference": True}
                )
            }
        ),
    )

    async with scheduler.request():
        run = service.create(compiled, "Queued work", conversation_id=None)
        await asyncio.sleep(0.1)

    failed = service.get(run.id)
    assert failed.status == "failed"
    assert "deadline" in (failed.error or "")
    await service.close()
    await model.client.close()


@pytest.mark.anyio
async def test_exclusive_hosted_run_bypasses_local_inference_scheduler(
    tmp_path,
    stub_provider,
) -> None:
    model = stub_binding(stub_provider, local_inference=True)
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        database_path=tmp_path / "metadata.sqlite3",
    )
    settings.ensure_directories()
    scheduler = InferenceScheduler()
    service = RunService(
        RunRepository(create_session_factory(settings)),
        ConversationSessionFactory(tmp_path / "sdk-sessions.sqlite3"),
        _ToolRuntime(),
        EventBroker(),
        settings=settings,
        inference_scheduler=scheduler,
    )
    compiled = _compiled(model, settings)
    entry_id = compiled.blueprint.entry_agent_id
    compiled = replace(
        compiled,
        blueprint=compiled.blueprint.model_copy(
            update={
                "run": compiled.blueprint.run.model_copy(
                    update={"exclusive_inference": True}
                )
            }
        ),
        resolved_models={
            entry_id: replace(
                compiled.resolved_models[entry_id],
                provider_kind="openai_compatible",
                local_inference=False,
            )
        },
    )

    async with scheduler.request():
        run = await asyncio.wait_for(
            service.run_now(compiled, "Hosted work", conversation_id=None),
            timeout=3,
        )

    assert run.status == "completed", run.error
    await service.close()
    await model.client.close()


@pytest.mark.anyio
async def test_claim_releases_and_run_fails_when_claimed_setup_raises(
    tmp_path,
    stub_provider,
    monkeypatch,
) -> None:
    model = stub_binding(stub_provider, local_inference=True)
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
        _ToolRuntime(),
        EventBroker(),
        settings=settings,
    )

    async def fail_setup(*_args, **_kwargs) -> None:
        raise RuntimeError("setup exploded")

    monkeypatch.setattr(service, "_execute_claimed_run", fail_setup)
    run = service.create(
        _compiled(model, settings),
        "Run setup",
        conversation_id=None,
    )
    for _ in range(100):
        failed = service.get(run.id)
        if failed.status == "failed":
            break
        await asyncio.sleep(0.01)

    assert failed.status == "failed"
    assert failed.error == "RuntimeError: setup exploded"
    for _ in range(100):
        replacement = repository.claim(run.id, "replacement-owner")
        if replacement is not None:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("Run claim was not released after setup failure.")
    assert repository.release_claim(
        run.id,
        "replacement-owner",
        generation=replacement.generation,
        token=replacement.token,
    )
    await service.close()
    await model.client.close()


@pytest.mark.anyio
async def test_epoch_continuation_respects_total_blueprint_turn_budget(
    tmp_path,
    stub_provider,
) -> None:
    stub_provider.tool_plans = [
        (
            "bounded goal",
            "search_research_library",
            {"query": None, "document_id": None, "limit": 10},
        ),
        (
            "bounded goal",
            "search_research_library",
            {"query": None, "document_id": None, "limit": 10},
        ),
        (
            "bounded goal",
            "search_research_library",
            {"query": None, "document_id": None, "limit": 10},
        ),
    ]
    model = stub_binding(stub_provider, local_inference=True)
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        database_path=tmp_path / "metadata.sqlite3",
        agent_epoch_max_turns=2,
        agent_max_epochs=10,
    )
    settings.ensure_directories()
    service = RunService(
        RunRepository(create_session_factory(settings)),
        ConversationSessionFactory(tmp_path / "sdk-sessions.sqlite3"),
        _ToolRuntime(),
        EventBroker(),
        settings=settings,
    )

    run = await service.run_now(
        _compiled(model, settings, max_turns=2),
        "Work on this bounded goal.",
        conversation_id="conversation-1",
    )

    assert run.status == "failed"
    assert "exhausted its 2-turn budget" in (run.error or "")
    assert len(run.epochs) == 1
    assert sum(int(epoch.usage_json["model_turns"]) for epoch in run.epochs) == 2
    await model.client.close()


@pytest.mark.anyio
async def test_queued_run_can_be_cancelled_before_model_start(
    tmp_path,
    stub_provider,
) -> None:
    model = stub_binding(stub_provider, local_inference=True)
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
        _ToolRuntime(),
        EventBroker(),
        settings=settings,
    )

    async with sessions.run_lock("conversation-1"):
        created = service.create(
            _compiled(model, settings),
            "Wait for the conversation lock.",
            conversation_id="conversation-1",
        )
        await asyncio.sleep(0)
        cancelled = await service.cancel(created.id)

    assert cancelled.status == "cancelled"
    assert not stub_provider.requests
    await model.client.close()


@pytest.mark.anyio
async def test_tool_results_are_bounded_and_attempts_are_journaled(
    test_settings,
    monkeypatch,
) -> None:
    test_settings.tool_result_max_tokens = 256
    from backend.bootstrap import create_services

    services = create_services(test_settings)
    try:
        run = services.runs._repository.create(
            conversation_id=None,
            agent_name="Journal test",
            input_value="test",
            blueprint={},
        )
        context = ScholarWeaveContext(
            run_id=run.id,
            tool_runtime=services.runs._tool_runtime,
        )

        def large_result(_arguments, _context):
            return {"items": ["large result " * 1000]}

        monkeypatch.setattr(
            services.runs._tool_runtime,
            "_search_research_library",
            large_result,
        )
        result = await services.runs._tool_runtime.invoke(
            "research.library.search",
            {"query": None, "document_id": None, "limit": 10},
            context,
            tool_call_id="call-1",
        )

        assert result["truncated"] is True
        assert result["result_ref"]
        stored = services.runs.get(run.id)
        assert len(stored.tool_attempts) == 1
        assert stored.tool_attempts[0].status == "completed"
        assert stored.tool_attempts[0].result_ref == result["result_ref"]
    finally:
        await services.close()


@pytest.mark.anyio
async def test_safe_reads_retry_but_failed_writes_remain_unknown(test_settings) -> None:
    from backend.bootstrap import create_services

    services = create_services(test_settings)
    try:
        run = services.runs._repository.create(
            conversation_id=None,
            agent_name="Failure policy test",
            input_value="test",
            blueprint={},
        )
        context = ScholarWeaveContext(
            run_id=run.id,
            tool_runtime=services.runs._tool_runtime,
        )
        calls = 0

        async def flaky_search(_query: str, _limit: int = 10):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("Remote endpoint returned HTTP 503.")
            return {"results": []}

        services.research_search.search_web = flaky_search
        result = await services.runs._tool_runtime.invoke(
            "research.sources.search",
            {"provider": "web", "query": "fault tolerance"},
            context,
            tool_call_id="read-call",
        )
        assert result["results"] == []
        assert result["cached"] is False

        with pytest.raises(ValueError):
            await services.runs._tool_runtime.invoke(
                "research.notes.save",
                {
                    "target": "path",
                    "mode": "overwrite",
                    "document_id": None,
                    "path": "../escape.md",
                    "name": None,
                    "content": "unsafe",
                    "tags": [],
                },
                context,
                tool_call_id="write-call",
            )

        attempts = services.runs.get(run.id).tool_attempts
        assert [attempt.status for attempt in attempts[:2]] == ["failed", "completed"]
        assert attempts[0].retryable is True
        assert attempts[-1].status == "unknown_outcome"
    finally:
        await services.close()
