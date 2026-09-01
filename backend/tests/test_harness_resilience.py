from __future__ import annotations

import asyncio

import pytest
from agents import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from backend.agents.blueprint import AgentBlueprint
from backend.agents.compiler import AgentCompiler
from backend.core.config import Settings
from backend.persistence import create_session_factory
from backend.providers.types import ModelReference, ResolvedAgentModel
from backend.runs.broker import EventBroker
from backend.runs.repository import RunRepository
from backend.runs.service import RunService
from backend.runtime.context import ScholarWeaveContext
from backend.runtime.sessions import SdkSessionFactory
from backend.tools.catalog import create_tool_catalog


class _Resolver:
    def __init__(self, model) -> None:
        self.model = model

    def resolve_agent_model(
        self,
        _reference: ModelReference,
        *,
        require_tools: bool = False,
    ) -> ResolvedAgentModel:
        return ResolvedAgentModel(
            self.model,
            "ollama",
            False,
            False,
            False,
            model_name="stub-model",
        )


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
                        "catalog_id": "documents.list",
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
        ("multi epoch", "list_documents", {}),
        ("multi epoch", "list_documents", {}),
    ]
    client = AsyncOpenAI(api_key="test", base_url=f"{stub_provider.base_url}/v1")
    model = OpenAIChatCompletionsModel(model="stub-model", openai_client=client)
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        database_path=tmp_path / "metadata.sqlite3",
        agent_epoch_max_turns=2,
        agent_max_epochs=4,
    )
    settings.ensure_directories()
    repository = RunRepository(create_session_factory(settings))
    sessions = SdkSessionFactory(tmp_path / "sdk-sessions.sqlite3")
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
    assert runtime.calls == ["documents.list", "documents.list"]
    assert any(event.event_type == "run.epoch.completed" for event in run.events)
    await client.close()


@pytest.mark.anyio
async def test_startup_recovery_abandons_interrupted_epoch(tmp_path, stub_provider) -> None:
    client = AsyncOpenAI(api_key="test", base_url=f"{stub_provider.base_url}/v1")
    model = OpenAIChatCompletionsModel(model="stub-model", openai_client=client)
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        database_path=tmp_path / "metadata.sqlite3",
    )
    settings.ensure_directories()
    repository = RunRepository(create_session_factory(settings))
    compiled = _compiled(model, settings)
    record = repository.create(
        agent_revision_id=None,
        conversation_id="conversation-1",
        agent_name=compiled.blueprint.name,
        input_value="Original goal",
        blueprint=compiled.blueprint.model_dump(mode="json", by_alias=True),
    )
    repository.mark_running(record.id)
    repository.begin_epoch(record.id, "Original goal")
    invalid = repository.create(
        agent_revision_id=None,
        conversation_id=None,
        agent_name="Invalid recovery",
        input_value="Broken",
        blueprint={"not": "a blueprint"},
    )
    service = RunService(
        repository,
        SdkSessionFactory(tmp_path / "sdk-sessions.sqlite3"),
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
    await client.close()


@pytest.mark.anyio
async def test_epoch_continuation_respects_total_blueprint_turn_budget(
    tmp_path,
    stub_provider,
) -> None:
    stub_provider.tool_plans = [
        ("bounded goal", "list_documents", {}),
        ("bounded goal", "list_documents", {}),
        ("bounded goal", "list_documents", {}),
    ]
    client = AsyncOpenAI(api_key="test", base_url=f"{stub_provider.base_url}/v1")
    model = OpenAIChatCompletionsModel(model="stub-model", openai_client=client)
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
        SdkSessionFactory(tmp_path / "sdk-sessions.sqlite3"),
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
    await client.close()


@pytest.mark.anyio
async def test_queued_run_can_be_cancelled_before_model_start(
    tmp_path,
    stub_provider,
) -> None:
    client = AsyncOpenAI(api_key="test", base_url=f"{stub_provider.base_url}/v1")
    model = OpenAIChatCompletionsModel(model="stub-model", openai_client=client)
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        database_path=tmp_path / "metadata.sqlite3",
    )
    settings.ensure_directories()
    sessions = SdkSessionFactory(tmp_path / "sdk-sessions.sqlite3")
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
            agent_revision_id=None,
            conversation_id="conversation-1",
        )
        await asyncio.sleep(0)
        cancelled = await service.cancel(created.id)

    assert cancelled.status == "cancelled"
    assert not stub_provider.requests
    await client.close()


@pytest.mark.anyio
async def test_tool_results_are_bounded_and_attempts_are_journaled(
    test_settings,
) -> None:
    test_settings.tool_result_max_tokens = 256
    from backend.bootstrap import create_services

    services = create_services(test_settings)
    try:
        run = services.runs._repository.create(
            agent_revision_id=None,
            conversation_id=None,
            agent_name="Journal test",
            input_value="test",
            blueprint={},
        )
        context = ScholarWeaveContext(
            run_id=run.id,
            tool_runtime=services.runs._tool_runtime,
        )

        result = await services.runs._tool_runtime.invoke(
            "sdk.catalog",
            {},
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
            agent_revision_id=None,
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

        async def flaky_search(_query: str):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("Remote endpoint returned HTTP 503.")
            return {"results": []}

        services.research_search.search_web = flaky_search
        result = await services.runs._tool_runtime.invoke(
            "web.search",
            {"query": "fault tolerance"},
            context,
            tool_call_id="read-call",
        )
        assert result == {"results": []}

        with pytest.raises(ValueError):
            await services.runs._tool_runtime.invoke(
                "workspace.write",
                {"path": "../escape.md", "content": "unsafe"},
                context,
                tool_call_id="write-call",
            )

        attempts = services.runs.get(run.id).tool_attempts
        assert [attempt.status for attempt in attempts[:2]] == ["failed", "completed"]
        assert attempts[0].retryable is True
        assert attempts[-1].status == "unknown_outcome"
    finally:
        await services.close()
