from __future__ import annotations

import asyncio

import pytest
from agents import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from backend.agents.blueprint import AgentBlueprint
from backend.agents.compiler import AgentCompiler
from backend.core.config import Settings
from backend.runs.broker import EventBroker
from backend.persistence import create_session_factory
from backend.providers.types import ModelReference, ResolvedAgentModel
from backend.runs.repository import RunRepository
from backend.runs.service import (
    STOP_AND_ANSWER_BLUEPRINT_DESCRIPTION,
    STOP_AND_ANSWER_PROMPT,
    RunService,
)
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

    async def invoke(self, catalog_id, arguments, context):
        self.calls.append((catalog_id, arguments))
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
    await client.close()


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
async def test_repeated_information_failures_disable_only_the_failing_tool(
    tmp_path,
    stub_provider,
) -> None:
    stub_provider.tool_plans = [
        ("Find unavailable evidence", "search_web", {"query": f"missing evidence {index}"})
        for index in range(3)
    ]
    client = AsyncOpenAI(
        api_key="test",
        base_url=f"{stub_provider.base_url}/v1",
    )
    model = OpenAIChatCompletionsModel(model="stub-model", openai_client=client)
    compiled = AgentCompiler(Resolver(model), create_tool_catalog()).compile(
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
                        "catalog_id": "web.search",
                    },
                    {
                        "id": "workspace-write",
                        "kind": "function",
                        "catalog_id": "workspace.write",
                    },
                    {
                        "id": "paper-list",
                        "kind": "function",
                        "catalog_id": "documents.list",
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
        SdkSessionFactory(tmp_path / "sdk-sessions.sqlite3"),
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
    assert "search_web tool is now disabled" in outputs[-1]
    offered_tools = {
        tool["function"]["name"]
        for tool in stub_provider.requests[-1].get("tools", [])
    }
    assert "search_web" not in offered_tools
    assert "list_documents" in offered_tools
    assert "write_workspace_file" in offered_tools
    assert "ask_research_helper" in offered_tools
    failures = [
        event.payload_json
        for event in run.events
        if event.event_type == "tool.failed"
    ]
    assert failures[-1]["failure_limit_reached"] is True
    await client.close()


@pytest.mark.anyio
async def test_cancel_immediately_stops_model_stream(
    tmp_path,
    stub_provider,
) -> None:
    stub_provider.stream_delay_seconds = 10
    client = AsyncOpenAI(
        api_key="test",
        base_url=f"{stub_provider.base_url}/v1",
    )
    model = OpenAIChatCompletionsModel(model="stub-model", openai_client=client)
    compiled = AgentCompiler(Resolver(model), create_tool_catalog()).compile(
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
        SdkSessionFactory(tmp_path / "sdk-sessions.sqlite3"),
        ToolRuntime(),
        EventBroker(),
    )

    pending = service.create(
        compiled,
        "Explain the evidence.",
        agent_revision_id=None,
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
    await client.close()


@pytest.mark.anyio
async def test_cancel_immediately_propagates_to_active_tool(
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
    runtime = BlockingToolRuntime()
    service = RunService(
        RunRepository(create_session_factory(settings)),
        SdkSessionFactory(tmp_path / "sdk-sessions.sqlite3"),
        runtime,
        EventBroker(),
    )

    pending = service.create(
        compiled,
        "List documents.",
        agent_revision_id=None,
        conversation_id="conversation-1",
    )
    await asyncio.wait_for(runtime.started.wait(), timeout=2)

    cancelled = await service.cancel(pending.id)

    assert cancelled.status == "cancelled"
    assert runtime.cancelled.is_set()
    await client.close()


@pytest.mark.anyio
async def test_stop_and_answer_starts_tool_free_answer_and_hides_internal_prompt(
    tmp_path,
    stub_provider,
) -> None:
    stub_provider.stream_delay_seconds = 10
    client = AsyncOpenAI(
        api_key="test",
        base_url=f"{stub_provider.base_url}/v1",
    )
    model = OpenAIChatCompletionsModel(model="stub-model", openai_client=client)
    compiled = AgentCompiler(Resolver(model), create_tool_catalog()).compile(
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
    sessions = SdkSessionFactory(tmp_path / "sdk-sessions.sqlite3")
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
            agent_revision_id=None,
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
    recovered_answer = AgentCompiler(Resolver(model), create_tool_catalog()).compile(
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

    paused = await service.run_now(compiled, "List the papers.")

    assert paused.status == "paused"
    assert paused.state_json
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
    assert sum(item.item_type == "tool_call_item" for item in resumed.items) == 1
    assert sum(item.item_type == "tool_call_output_item" for item in resumed.items) == 1
    assert any(event.event_type == "run.paused" for event in resumed.events)
    assert any(event.event_type == "run.completed" for event in resumed.events)

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
