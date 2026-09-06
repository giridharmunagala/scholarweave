from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from openai import AsyncOpenAI

from backend.agents.compiler import AgentCompiler
from backend.agents.blueprint import AgentBlueprint
from backend.agents.context import ScholarWeaveContext
from backend.agents.harness import (
    AgentDefinition,
    JsonSchemaOutput,
    ModelBehaviorError,
    ModelSettings,
    RunSettings,
    run_agent,
)
from backend.providers.binding import ProfileModelResolver, ProviderClientPool
from backend.providers.runtime import ModelRuntime, ResolvedModel
from backend.providers.types import ModelReference
from backend.providers.inference import InferenceScheduler
from backend.tools.catalog import create_tool_catalog


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Runtime:
    def __init__(
        self,
        kind: str,
        *,
        preserve_thinking: bool = False,
        base_url: str = "https://api.openai.com/v1",
        context_window_tokens: int | None = None,
    ) -> None:
        self.kind = kind
        self.preserve_thinking = preserve_thinking
        self.base_url = base_url
        self.context_window_tokens = context_window_tokens
        self.client_calls = 0
        self.capabilities: list[str] = []

    def resolve(self, capability, *, model_reference):
        self.capabilities.append(capability)
        return ResolvedModel(
            profile_id="profile",
            profile_name="Profile",
            kind=self.kind,
            base_url=self.base_url,
            api_key="test",
            model=model_reference.model or "test-model",
            context_window_tokens=self.context_window_tokens,
            preserve_thinking=self.preserve_thinking,
        )

    def client(self, resolved):
        self.client_calls += 1
        return AsyncOpenAI(api_key="test", base_url=resolved.base_url)


class _TransientClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.closed = False
        self.models = SimpleNamespace(list=self._list_models)
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create_completion)
        )
        self.embeddings = SimpleNamespace(create=self._create_embedding)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        self.closed = True

    async def _list_models(self):
        return SimpleNamespace(data=[SimpleNamespace(id="model-a")])

    async def _create_completion(self, **_kwargs):
        if self.fail:
            raise RuntimeError("generation failed")
        message = SimpleNamespace(content="answer")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            model_dump=lambda **_kwargs: {"id": "response"},
        )

    async def _create_embedding(self, **_kwargs):
        return SimpleNamespace(data=[SimpleNamespace(embedding=[1.0, 2.0])])


@pytest.mark.anyio
async def test_transient_openai_clients_are_closed_on_success_and_error(
    monkeypatch,
) -> None:
    runtime = object.__new__(ModelRuntime)
    clients = [_TransientClient() for _ in range(3)]
    clients.append(_TransientClient(fail=True))
    monkeypatch.setattr(runtime, "client", lambda _resolved: clients.pop(0))
    resolved = ResolvedModel(
        profile_id="profile",
        profile_name="Profile",
        kind="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test",
        model="model-a",
    )
    profile = SimpleNamespace(
        id="profile",
        name="Profile",
        kind="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test",
        models_json=[],
    )
    created = list(clients)

    assert [entry.name for entry in await runtime.discover(profile)] == ["model-a"]
    assert (await runtime.generate(resolved, "answer"))["response"] == "answer"
    assert await runtime.embed(resolved, "text") == [[1.0, 2.0]]
    with pytest.raises(RuntimeError, match="generation failed"):
        await runtime.generate(resolved, "fail")

    assert all(client.closed for client in created)


@pytest.mark.anyio
async def test_every_provider_kind_binds_one_chat_completions_client() -> None:
    for kind, parallel in (
        ("openai", True),
        ("azure_openai", True),
        ("azure_foundry", True),
        ("ollama", False),
        ("openai_compatible", False),
    ):
        runtime = Runtime(kind, context_window_tokens=16_384)
        pool = ProviderClientPool(runtime)  # type: ignore[arg-type]
        resolver = ProfileModelResolver(runtime, pool)  # type: ignore[arg-type]

        first = resolver.resolve_agent_model(ModelReference("profile", "model-a"))
        second = resolver.resolve_agent_model(
            ModelReference("profile", "model-a"),
            require_tools=True,
        )

        assert first.model_name == "model-a"
        assert first.provider_kind == kind
        assert first.supports_parallel_tool_calls is parallel
        assert first.context_window_tokens == 16_384
        assert first.client is second.client
        assert runtime.capabilities == ["chat", "chat"]
        assert runtime.client_calls == 1
        await pool.close()


@pytest.mark.anyio
async def test_client_pool_retires_invalidated_clients_until_safe_shutdown() -> None:
    class Client:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    class ClientRuntime:
        def __init__(self) -> None:
            self.created: list[Client] = []

        def client(self, _resolved):
            client = Client()
            self.created.append(client)
            return client

    runtime = ClientRuntime()
    pool = ProviderClientPool(runtime)  # type: ignore[arg-type]
    resolved = ResolvedModel(
        profile_id="profile",
        profile_name="Profile",
        kind="openai",
        base_url="https://api.openai.com/v1",
        api_key="test",
        model="gpt-test",
    )

    first = pool.get(resolved)
    pool.invalidate_profile("profile")
    second = pool.get(resolved)

    assert second is not first
    assert first.closed is False
    await pool.close()
    assert first.closed is True
    assert second.closed is True


@pytest.mark.anyio
async def test_only_local_provider_clients_use_inference_scheduler(
    monkeypatch,
) -> None:
    scheduler = InferenceScheduler()
    observed: list[InferenceScheduler | None] = []

    def client_factory(
        _settings,
        _provider,
        request_lock=None,
        inference_scheduler=None,
        profile_id=None,
    ):
        observed.append(inference_scheduler)
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: None))

    monkeypatch.setattr(
        "backend.providers.runtime.logged_http_client",
        client_factory,
    )
    runtime = object.__new__(ModelRuntime)
    runtime.settings = SimpleNamespace(request_timeout_seconds=30)
    runtime.inference_scheduler = scheduler
    profiles = [
        ("openai", "https://api.openai.com/v1"),
        ("azure_openai", "https://azure.example/v1"),
        ("azure_foundry", "https://foundry.example/v1"),
        ("ollama", "https://hosted.example/v1"),
        ("openai_compatible", "http://127.0.0.1:8080/v1"),
        ("openai_compatible", "https://openrouter.ai/api/v1"),
    ]
    clients = [
        runtime.client(
            ResolvedModel(
                profile_id=f"{kind}-{base_url}",
                profile_name=kind,
                kind=kind,
                base_url=base_url,
                api_key="test",
                model="model",
            )
        )
        for kind, base_url in profiles
    ]

    assert observed == [None, None, None, scheduler, scheduler, None]
    for client in clients:
        await client.close()


@pytest.mark.anyio
async def test_compatible_profile_preserves_model_thinking_flag() -> None:
    runtime = Runtime("openai_compatible", preserve_thinking=True)
    pool = ProviderClientPool(runtime)  # type: ignore[arg-type]
    resolver = ProfileModelResolver(runtime, pool)  # type: ignore[arg-type]

    resolved = resolver.resolve_agent_model(ModelReference("profile", "local-qwen"))

    assert resolved.preserve_thinking is True
    await pool.close()


@pytest.mark.anyio
async def test_compatible_profile_locality_is_propagated_to_run_model() -> None:
    local_runtime = Runtime(
        "openai_compatible",
        base_url="http://192.168.1.20:8080/v1",
    )
    hosted_runtime = Runtime(
        "openai_compatible",
        base_url="https://openrouter.ai/api/v1",
    )
    local_pool = ProviderClientPool(local_runtime)  # type: ignore[arg-type]
    hosted_pool = ProviderClientPool(hosted_runtime)  # type: ignore[arg-type]

    local = ProfileModelResolver(
        local_runtime, local_pool  # type: ignore[arg-type]
    ).resolve_agent_model(ModelReference("local", "model"))
    hosted = ProfileModelResolver(
        hosted_runtime, hosted_pool  # type: ignore[arg-type]
    ).resolve_agent_model(ModelReference("hosted", "model"))

    assert local.local_inference is True
    assert hosted.local_inference is False
    await local_pool.close()
    await hosted_pool.close()


@pytest.mark.anyio
async def test_compatible_model_does_not_combine_tool_and_json_schema_grammars(
    stub_provider,
) -> None:
    runtime = Runtime("openai_compatible", base_url=f"{stub_provider.base_url}/v1")
    pool = ProviderClientPool(runtime)  # type: ignore[arg-type]
    resolver = ProfileModelResolver(runtime, pool)  # type: ignore[arg-type]
    binding = resolver.resolve_agent_model(ModelReference("profile", "stub-model"))
    output_schema = JsonSchemaOutput(
        "Receipt",
        {
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "required": ["status"],
            "additionalProperties": False,
        },
        strict=True,
    )
    compiled = AgentCompiler(
        SimpleNamespace(resolve_agent_model=lambda *_args, **_kwargs: binding),
        create_tool_catalog(),
    ).compile(
        AgentBlueprint.model_validate(
            {
                "name": "Structured worker",
                "entry_agent_id": "worker",
                "agents": [
                    {
                        "id": "worker",
                        "name": "Structured worker",
                        "instructions": "Return the requested receipt.",
                        "tool_ids": ["library"],
                        "output": {
                            "kind": "json_schema",
                            "name": "Receipt",
                            "schema": output_schema.json_schema(),
                        },
                    }
                ],
                "tools": [
                    {
                        "id": "library",
                        "kind": "function",
                        "catalog_id": "research.library.search",
                    }
                ],
            }
        )
    )
    context = ScholarWeaveContext(run_id="run-1", tool_runtime=SimpleNamespace())

    stub_provider.reply = '{"status":"done"}'
    result = await run_agent(
        compiled.entry_agent,
        "Finish without using the tool.",
        context=context,
        settings=RunSettings(),
        max_turns=1,
    )

    assert result.final_output == {"status": "done"}
    combined_request = stub_provider.requests[-1]
    assert combined_request["tools"][0]["function"]["strict"] is True
    assert combined_request["response_format"] == {"type": "json_object"}

    stub_provider.reply = '{"unexpected":"value"}'
    with pytest.raises(ModelBehaviorError, match="failed JSON Schema validation"):
        await run_agent(
            compiled.entry_agent,
            "Return an invalid receipt.",
            context=context,
            settings=RunSettings(),
            max_turns=1,
        )

    stub_provider.reply = '{"status":"done"}'
    plain_agent = AgentDefinition(
        id="plain",
        name="Structured worker without tools",
        instructions="Return the requested receipt.",
        binding=binding,
        model_settings=ModelSettings(),
        output_schema=output_schema,
    )
    result = await run_agent(
        plain_agent,
        "Finish.",
        context=context,
        settings=RunSettings(),
        max_turns=1,
    )

    assert result.final_output == {"status": "done"}
    assert stub_provider.requests[-1]["response_format"]["type"] == "json_schema"
    await pool.close()
