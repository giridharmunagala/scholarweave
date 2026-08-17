from __future__ import annotations

import asyncio

import pytest
from agents import (
    Model,
    ModelResponse,
    ModelSettings,
    OpenAIChatCompletionsModel,
    TResponseInputItem,
    Usage,
)
from openai import AsyncOpenAI

from backend.agents.blueprint import AgentBlueprint
from backend.agents.compiler import AgentCompiler
from backend.core.config import Settings
from backend.runs.broker import EventBroker
from backend.persistence import create_session_factory
from backend.providers.types import ModelReference, ResolvedAgentModel
from backend.runs.repository import RunRepository
from backend.runs.service import RunService
from backend.runtime.sessions import SdkSessionFactory
from backend.tools.catalog import create_tool_catalog


class Resolver:
    def __init__(self, model) -> None:
        self.model = model

    def resolve_agent_model(
        self,
        reference: ModelReference,
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


class ToolRuntime:
    def __init__(self) -> None:
        self.calls = []
        self.context_metadata = []

    async def invoke(self, catalog_id, arguments, context):
        self.calls.append((catalog_id, arguments))
        self.context_metadata.append(dict(context.metadata))
        return [{"id": "paper-1", "title": "Paper"}]


class FailingToolRuntime:
    async def invoke(self, catalog_id, arguments, context):
        raise RuntimeError("Remote page returned HTTP 503.")


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


class FailingModel(Model):
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
        raise RuntimeError("Model connection failed.")

    def stream_response(self, *args, **kwargs):
        async def events():
            raise RuntimeError("Model connection failed.")
            yield

        return events()


class BlockingModel(Model):
    def __init__(self) -> None:
        self.started = asyncio.Event()

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
        async def events():
            self.started.set()
            await asyncio.Event().wait()
            yield

        return events()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_run_service_persists_sdk_items_events_and_usage(
    tmp_path,
    stub_provider,
) -> None:
    stub_provider.call_tool = "list_documents"
    client = AsyncOpenAI(
        api_key="test",
        base_url=f"{stub_provider.base_url}/v1",
    )
    model = OpenAIChatCompletionsModel(model="stub-model", openai_client=client)
    catalog = create_tool_catalog()
    compiled = AgentCompiler(Resolver(model), catalog).compile(
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
                        "catalog_id": "documents.list",
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
        SdkSessionFactory(tmp_path / "sdk-sessions.sqlite3"),
        tool_runtime,
        EventBroker(),
    )

    run = await service.run_now(compiled, "List the papers.")

    assert run.status == "completed"
    assert run.final_output_json == "Stub answer."
    assert run.last_agent_name == "Researcher"
    assert "total_tokens" in run.usage_json
    assert any(item.item_type == "tool_call_item" for item in run.items)
    assert any(item.item_type == "tool_call_output_item" for item in run.items)
    assert any(event.event_type == "run.completed" for event in run.events)
    assert tool_runtime.calls == [("documents.list", {})]
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
    await client.close()


@pytest.mark.anyio
async def test_real_tool_loop_receives_bounded_output_and_reads_retained_result(
    tmp_path,
    stub_provider,
) -> None:
    stub_provider.tool_plans = [
        ("Gather evidence", "list_documents", {}),
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
    client = AsyncOpenAI(
        api_key="test",
        base_url=f"{stub_provider.base_url}/v1",
    )
    model = OpenAIChatCompletionsModel(model="stub-model", openai_client=client)
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        database_path=tmp_path / "metadata.sqlite3",
        tool_result_max_tokens=512,
    )
    compiled = AgentCompiler(
        Resolver(model),
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
                        "catalog_id": "documents.list",
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
        SdkSessionFactory(tmp_path / "sdk-sessions.sqlite3"),
        tool_runtime,
        EventBroker(),
    )

    run = await service.run_now(compiled, "Gather evidence.")

    assert run.status == "completed"
    assert tool_runtime.calls == [
        ("documents.list", {}),
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
    await client.close()


@pytest.mark.anyio
async def test_run_failure_settles_active_agent_invocation(tmp_path) -> None:
    model = FailingModel()
    compiled = AgentCompiler(Resolver(model), create_tool_catalog()).compile(
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
        SdkSessionFactory(tmp_path / "sdk-sessions.sqlite3"),
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
    model = BlockingModel()
    compiled = AgentCompiler(Resolver(model), create_tool_catalog()).compile(
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
        SdkSessionFactory(tmp_path / "sdk-sessions.sqlite3"),
        ToolRuntime(),
        EventBroker(),
    )
    created = service.create(
        compiled,
        "Wait.",
        agent_revision_id=None,
        conversation_id=None,
    )
    await asyncio.wait_for(model.started.wait(), timeout=2)

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
        SdkSessionFactory(tmp_path / "sdk-sessions.sqlite3"),
        ToolRuntime(),
        EventBroker(),
    )
    old_run = repository.create(
        agent_revision_id=None,
        conversation_id=None,
        agent_name="Old run",
        input_value="old",
        blueprint={},
    )
    active_run = repository.create(
        agent_revision_id=None,
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
async def test_tool_failure_is_returned_to_model_without_failing_run(
    tmp_path,
    stub_provider,
) -> None:
    stub_provider.call_tool = "download_web_page"
    stub_provider.tool_arguments = {"url": "https://example.com/unavailable"}
    client = AsyncOpenAI(
        api_key="test",
        base_url=f"{stub_provider.base_url}/v1",
    )
    model = OpenAIChatCompletionsModel(model="stub-model", openai_client=client)
    compiled = AgentCompiler(Resolver(model), create_tool_catalog()).compile(
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
                        "catalog_id": "webpage.download",
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
        SdkSessionFactory(tmp_path / "sdk-sessions.sqlite3"),
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
    await client.close()


@pytest.mark.anyio
async def test_run_service_serializes_approval_and_resumes_sdk_state(
    tmp_path,
    stub_provider,
) -> None:
    stub_provider.call_tool = "list_documents"
    client = AsyncOpenAI(
        api_key="test",
        base_url=f"{stub_provider.base_url}/v1",
    )
    model = OpenAIChatCompletionsModel(model="stub-model", openai_client=client)
    compiled = AgentCompiler(Resolver(model), create_tool_catalog()).compile(
        AgentBlueprint.model_validate(
            {
                "name": "Approval researcher",
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
                        "catalog_id": "documents.list",
                        "needs_approval": True,
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
        SdkSessionFactory(tmp_path / "sdk-sessions.sqlite3"),
        tool_runtime,
        EventBroker(),
    )

    run = service.create(
        compiled,
        "List the papers.",
        agent_revision_id=None,
        conversation_id=None,
        runtime_metadata={"extended_work_notes": [{"summary": "Keep this finding."}]},
    )
    for _attempt in range(200):
        paused = service.get(run.id)
        if paused.status in {"paused", "completed", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.01)

    assert paused.status == "paused"
    assert paused.state_json
    assert paused.state_json["context"]["context"]["metadata"] == {
        "extended_work_notes": [{"summary": "Keep this finding."}]
    }
    assert len(paused.interruptions) == 1
    assert paused.interruptions[0].status == "pending"
    assert tool_runtime.calls == []

    await service.resolve_interruption(
        compiled,
        run_id=paused.id,
        interruption_id=paused.interruptions[0].id,
        approved=True,
    )
    for _attempt in range(200):
        resumed = service.get(paused.id)
        if resumed.status in {"completed", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.01)

    assert resumed.status == "completed", resumed.error
    assert resumed.interruptions[0].status == "approved"
    assert tool_runtime.calls == [("documents.list", {})]
    assert tool_runtime.context_metadata == [
        {"extended_work_notes": [{"summary": "Keep this finding."}]}
    ]
    assert sum(item.item_type == "tool_call_item" for item in resumed.items) == 1
    assert sum(item.item_type == "tool_call_output_item" for item in resumed.items) == 1
    assert any(event.event_type == "run.paused" for event in resumed.events)
    assert any(event.event_type == "run.completed" for event in resumed.events)
    lifecycle = [
        event
        for event in resumed.events
        if event.event_type
        in {"agent.started", "agent.completed", "agent.superseded"}
    ]
    assert [event.event_type for event in lifecycle[:4]] == [
        "agent.started",
        "agent.superseded",
        "agent.started",
        "agent.completed",
    ]
    assert lifecycle[0].payload_json["invocation_id"] == lifecycle[1].payload_json[
        "invocation_id"
    ]
    assert lifecycle[2].payload_json["invocation_id"] == lifecycle[3].payload_json[
        "invocation_id"
    ]
    assert lifecycle[0].payload_json["invocation_id"] != lifecycle[2].payload_json[
        "invocation_id"
    ]

    rejected = await service.run_now(compiled, "List the papers again.")
    await service.resolve_interruption(
        compiled,
        run_id=rejected.id,
        interruption_id=rejected.interruptions[0].id,
        approved=False,
        rejection_message="The paper list is not needed.",
    )
    for _attempt in range(200):
        rejected = service.get(rejected.id)
        if rejected.status in {"completed", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.01)

    assert rejected.status == "completed", rejected.error
    assert rejected.interruptions[0].status == "rejected"
    assert rejected.interruptions[0].response_json == {
        "approved": False,
        "message": "The paper list is not needed.",
    }
    assert tool_runtime.calls == [("documents.list", {})]
    await client.close()
