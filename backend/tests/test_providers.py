from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import AsyncOpenAI

from backend.app import create_app
from backend.bootstrap import create_services
from backend.core.config import Settings
from backend.core.models import AppSetting
from backend.providers.types import ProviderRuntimeError
from backend.providers.ollama import OllamaClient
from backend.providers.logging import ScheduledTransport
from backend.providers.repository import ProviderRepository
from backend.providers.runtime import ResolvedModel, _compatible_context_window
from backend.providers.schemas import ProviderCreate, ProviderModel, ProviderUpdate
from backend.providers.types import AgentModelDefaults, ModelReference


@pytest.mark.anyio
async def test_model_switch_setting_persists_without_restart_confirmation(test_settings):
    services = create_services(test_settings)
    provider = services.providers.create(ProviderCreate(
        name="Hot swap GPU", kind="openai_compatible", base_url="http://127.0.0.1:8080/v1",
        models=[ProviderModel(name="main")],
    ))
    assert provider.serialize_model_switches
    repository = ProviderRepository(services.session_factory)
    repository.update(provider.id, config_json={
        "residency": {"enabled": True, "resident_model": "main"},
        "unrelated": "keep",
    })
    await services.close()
    restarted = create_services(test_settings)
    try:
        scheduler = restarted.providers._runtime.inference_scheduler
        async with asyncio.timeout(1):
            async with scheduler.request(profile_id=provider.id, model="another"):
                pass
        updated = restarted.providers.update(
            provider.id, ProviderUpdate(serialize_model_switches=False),
        )
        assert not updated.serialize_model_switches
        updated = restarted.providers.update(provider.id, ProviderUpdate(name="Renamed"))
        assert not updated.serialize_model_switches
        stored = ProviderRepository(restarted.session_factory).get(provider.id)
        assert stored.config_json == {"serialize_model_switches": False, "unrelated": "keep"}
    finally:
        await restarted.close()
    restarted = create_services(test_settings)
    try:
        assert not restarted.providers.get(provider.id).serialize_model_switches
    finally:
        await restarted.close()


@pytest.mark.anyio
async def test_multi_model_catalog_does_not_gate_inference(test_settings, monkeypatch):
    services = create_services(test_settings)
    profile = services.providers.create(ProviderCreate(
        name="Catalog server", kind="openai_compatible", base_url="http://127.0.0.1:8080/v1",
    ))
    scheduler = services.providers._runtime.inference_scheduler
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={
                "object": "list",
                "data": [{"id": model, "object": "model", "created": 0, "owned_by": "local"}
                         for model in ("main", "small", "vision")],
            })
        return httpx.Response(200, json={
            "id": "reply", "object": "chat.completion", "created": 0,
            "model": json.loads(request.content)["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
        })

    def mock_client(resolved):
        return AsyncOpenAI(
            base_url=resolved.base_url, api_key="unused",
            http_client=httpx.AsyncClient(transport=ScheduledTransport(
                httpx.MockTransport(respond), scheduler, resolved.profile_id,
            )),
        )

    monkeypatch.setattr(services.providers._runtime, "client", mock_client)
    try:
        models = await services.providers.discover(profile.id)
        assert {model.name for model in models.models} == {"main", "small", "vision"}
        async with asyncio.timeout(1):
            for model in ("main", "small"):
                resolved = services.providers._runtime.resolve(
                    "chat", model_reference=ModelReference(profile.id, model),
                )
                assert (await services.providers._runtime.generate(resolved, "hello"))["response"] == "ok"
        assert calls == ["/v1/models", "/v1/chat/completions", "/v1/chat/completions"]
    finally:
        await services.close()


def test_provider_model_switch_setting_http_defaults_and_updates(test_settings):
    with TestClient(create_app(test_settings)) as client:
        for name, url, expected in (
            ("Local", "http://127.0.0.1:8080/v1", True),
            ("Remote", "https://example.com/v1", False),
        ):
            response = client.post("/api/providers", json={
                "name": name, "kind": "openai_compatible", "base_url": url,
            })
            assert response.status_code == 201
            profile = response.json()
            assert profile["serialize_model_switches"] is expected
            path = f"/api/providers/{profile['id']}"
            response = client.put(path, json={"serialize_model_switches": not expected})
            assert response.status_code == 200
            assert response.json()["serialize_model_switches"] is not expected
            assert client.put(path, json={"serialize_model_switches": None}).status_code == 422
            assert client.put(path, json={"name": name + " renamed"}).json()["serialize_model_switches"] is not expected
            assert client.post(path + "/residency/confirm", json={"model": "main"}).status_code == 404


def test_compatible_model_context_is_read_from_llama_cpp_metadata() -> None:
    class Model:
        def model_dump(self, *, mode):
            assert mode == "python"
            return {
                "id": "muse-glimmer",
                "status": {
                    "args": [
                        "llama-server",
                        "--ctx-size",
                        "131072",
                    ]
                },
            }

    assert _compatible_context_window(Model()) == 131_072


def test_provider_profile_crud_masks_key_persists_and_archives(test_settings) -> None:
    client = TestClient(create_app(test_settings))
    created = client.post(
        "/api/providers",
        json={
            "name": "Shared gateway",
            "kind": "openai_compatible",
            "base_url": "https://models.example.test/v1/",
            "api_key": "do-not-return-me",
            "models": [{"name": "chat-1", "capabilities": ["chat", "tools"]}],
        },
    )
    assert created.status_code == 201
    body = created.json()
    assert body["api_key_set"] is True
    assert "api_key" not in body
    assert "do-not-return-me" not in str(body)

    profile_id = body["id"]
    updated = client.put(
        f"/api/providers/{profile_id}",
        json={
            "name": "Renamed gateway",
            "models": [
                {
                    "name": "embed-1",
                    "capabilities": ["embedding"],
                    "enabled": False,
                }
            ],
        },
    )
    assert updated.status_code == 200
    assert updated.json()["models"] == [
        {
            "name": "embed-1",
            "capabilities": ["embedding"],
            "reasoning_efforts": None,
            "preserve_thinking": False,
            "context_window_tokens": None,
            "enabled": False,
        }
    ]

    restarted = TestClient(
        create_app(
            Settings(
                data_dir=test_settings.data_dir,
                workspace_dir=test_settings.workspace_dir,
                frontend_dist_dir=test_settings.frontend_dist_dir,
            )
        )
    )
    restarted_profile = restarted.get(f"/api/providers/{profile_id}").json()
    assert restarted_profile["api_key_set"] is True
    assert restarted_profile["models"] == [
        {
            "name": "embed-1",
            "capabilities": ["embedding"],
            "reasoning_efforts": None,
            "preserve_thinking": False,
            "context_window_tokens": None,
            "enabled": False,
        }
    ]
    archived = restarted.delete(f"/api/providers/{profile_id}")
    assert archived.status_code == 200
    assert archived.json()["state"] == "archived"
    assert profile_id not in {profile["id"] for profile in restarted.get("/api/providers").json()}


def test_last_chat_model_reference_persists_across_restarts(test_settings) -> None:
    client = TestClient(create_app(test_settings))
    reference = {
        "provider_profile_id": "provider-1",
        "model": "research-model",
    }

    updated = client.put(
        "/api/settings",
        json={"last_chat_model_reference": reference},
    )

    assert updated.status_code == 200
    assert updated.json()["last_chat_model_reference"] == reference

    restarted = TestClient(
        create_app(
            Settings(
                data_dir=test_settings.data_dir,
                workspace_dir=test_settings.workspace_dir,
                frontend_dist_dir=test_settings.frontend_dist_dir,
            )
        )
    )
    assert restarted.get("/api/settings").json()["last_chat_model_reference"] == reference


def test_provider_models_expose_model_specific_reasoning_efforts(test_settings) -> None:
    client = TestClient(create_app(test_settings))
    luna = client.post(
        "/api/providers",
        json={
            "name": "Azure reasoning",
            "kind": "azure_openai",
            "base_url": "https://azure.example.test/openai/v1",
            "api_key": "test-key",
            "models": [{"name": "gpt-5.6-luna"}],
        },
    ).json()["models"][0]
    compatible = client.post(
        "/api/providers",
        json={
            "name": "Local reasoning",
            "kind": "openai_compatible",
            "base_url": "http://127.0.0.1:8080/v1",
            "models": [
                {"name": "qwen3.8-27b-q3-vision-long"},
                {"name": "gemma-4-12b-q6-bf16-long"},
                {"name": "gemma-4-12b-q8-long"},
                {"name": "custom-model"},
                {"name": "/models/Qwen3.8-27B-GGUF/Qwen3.8-27B-IQ3_S.gguf"},
                {"name": "qwen3.6-27b"},
            ],
        },
    ).json()["models"]

    assert luna["reasoning_efforts"] == [
        "none",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]
    assert compatible[0]["reasoning_efforts"] == ["none", "low", "medium", "xhigh"]
    assert compatible[1]["reasoning_efforts"] == ["none", "high"]
    assert compatible[2]["reasoning_efforts"] == ["none", "high"]
    assert compatible[3]["reasoning_efforts"] is None
    assert compatible[4]["reasoning_efforts"] == ["none", "low", "medium", "xhigh"]
    assert compatible[5]["reasoning_efforts"] == ["low", "medium", "xhigh"]


def test_user_profile_and_timezone_settings_persist(test_settings) -> None:
    app = create_app(test_settings)
    with TestClient(app) as client:
        updated = client.put(
            "/api/settings",
            json={
                "user_timezone": "Asia/Kolkata",
                "user_profile": "Based in Hyderabad, India.",
            },
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["user_timezone"] == "Asia/Kolkata"
        assert updated.json()["user_profile"] == "Based in Hyderabad, India."

        invalid = client.put(
            "/api/settings",
            json={"user_timezone": "Not/A_Timezone"},
        )
        assert invalid.status_code == 422


def test_legacy_empty_ocr_model_references_migrate_to_null(test_settings) -> None:
    services = create_services(test_settings)
    with services.session_factory() as session:
        session.add_all(
            [
                AppSetting(key="ocr_llm_model", value_json={}),
                AppSetting(key="ocr_llm_triage_model", value_json={}),
            ]
        )
        session.commit()
    asyncio.run(services.close())

    restarted = create_services(
        Settings(
            data_dir=test_settings.data_dir,
            workspace_dir=test_settings.workspace_dir,
            frontend_dist_dir=test_settings.frontend_dist_dir,
        )
    )
    try:
        response = restarted.settings_service.response()
        assert response.ocr_llm_model is None
        assert response.ocr_llm_triage_model is None
        with restarted.session_factory() as session:
            assert session.get(AppSetting, "ocr_llm_model") is None
            assert session.get(AppSetting, "ocr_llm_triage_model") is None
    finally:
        asyncio.run(restarted.close())


def test_provider_validation_does_not_reflect_api_keys(test_settings) -> None:
    client = TestClient(create_app(test_settings))
    secret = "never-reflect-this-key"
    response = client.post(
        "/api/providers",
        json={"name": "Bad key", "kind": "openai", "base_url": "https://api.example.test/v1", "api_key": secret * 1000},
    )
    assert response.status_code == 422
    assert secret not in response.text


def test_default_model_references_resolve_through_default_ollama_profile(test_settings) -> None:
    settings = Settings(
        data_dir=test_settings.data_dir,
        workspace_dir=test_settings.workspace_dir,
        frontend_dist_dir=test_settings.frontend_dist_dir,
        ollama_base_url="http://ollama.test:11434",
    )
    services = create_services(settings)

    profile = next(profile for profile in services.model_runtime.profiles() if profile.name == "Default Ollama")
    assert profile.base_url == "http://ollama.test:11434"
    services.settings.default_model_references = {
        "chat": {"provider_profile_id": profile.id, "model": "local-chat"},
        "embedding": {"provider_profile_id": profile.id, "model": "local-embed"},
    }
    assert services.model_runtime.resolve("chat").model == "local-chat"
    assert services.model_runtime.resolve("embedding").model == "local-embed"


def test_disabled_provider_model_cannot_be_resolved(test_settings) -> None:
    services = create_services(test_settings)
    profile = services.providers.create(
        ProviderCreate(
            name="Selectable models",
            kind="openai_compatible",
            base_url="https://models.example.test/v1",
            models=[ProviderModel(name="disabled-chat", enabled=False)],
        )
    )

    with pytest.raises(ProviderRuntimeError, match="disabled"):
        services.model_runtime.resolve(
            "chat",
            model_reference=ModelReference(
                provider_profile_id=profile.id,
                model="disabled-chat",
            ),
        )


@pytest.mark.anyio
async def test_azure_discovery_requires_models_to_be_enabled(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = create_services(test_settings)
    profile = services.providers.create(
        ProviderCreate(
            name="Azure catalog",
            kind="azure_openai",
            base_url="https://azure.example.test/openai",
            api_key="test-key",
            models=[
                ProviderModel(name="already-selected", enabled=True),
            ],
        )
    )

    async def discover(_profile) -> list[ProviderModel]:
        return [
            ProviderModel(name="already-selected"),
            ProviderModel(name="newly-discovered"),
        ]

    monkeypatch.setattr(services.model_runtime, "discover", discover)

    result = await services.providers.discover(profile.id)

    assert {model.name: model.enabled for model in result.models} == {
        "already-selected": True,
        "newly-discovered": False,
    }


def test_compatible_discovery_and_model_resolution(test_settings, stub_provider) -> None:
    app = create_app(test_settings)
    client = TestClient(app)
    created = client.post(
        "/api/providers",
        json={
            "name": "Stub compatible",
            "kind": "openai_compatible",
            "base_url": f"{stub_provider.base_url}/v1",
            "models": [{"name": "manual-embed", "capabilities": ["embedding"]}],
        },
    )
    assert created.status_code == 201
    profile = created.json()
    assert profile["api_key_set"] is False
    discovered = client.get(f"/api/providers/{profile['id']}/models")
    assert discovered.status_code == 200
    assert {entry["name"] for entry in discovered.json()["models"]} == {"stub-model", "manual-embed"}
    persisted = client.get(f"/api/providers/{profile['id']}").json()
    assert {entry["name"] for entry in persisted["models"]} == {"stub-model", "manual-embed"}
    verified = client.post(f"/api/providers/{profile['id']}/verify", json={"model": "stub-model"})
    assert verified.status_code == 200
    assert verified.json()["reachable"] is True

    runtime = app.state.services.model_runtime
    agent_model_defaults = AgentModelDefaults(chat=ModelReference(provider_profile_id=profile["id"], model="agent-chat"))
    assert runtime.resolve("chat", agent_model_defaults=agent_model_defaults).model == "agent-chat"
    assert runtime.resolve(
        "chat",
        model_reference=ModelReference(provider_profile_id=profile["id"], model="selected-chat"),
        agent_model_defaults=agent_model_defaults,
    ).model == "selected-chat"


def test_hosted_openai_still_requires_api_key(test_settings) -> None:
    with pytest.raises(ValueError, match="require an API key"):
        ProviderCreate.model_validate(
            {
            "name": "Hosted OpenAI",
            "kind": "openai",
            "base_url": "https://api.openai.com/v1",
            }
        )


@pytest.mark.anyio
async def test_ollama_discovery_lists_every_installed_model_with_capabilities(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = create_services(test_settings)
    profile = next(
        item for item in services.model_runtime.profiles() if item.name == "Default Ollama"
    )
    repository = ProviderRepository(services.session_factory)
    repository.update(
        profile.id,
        models_json=[
            {"name": "stale-model:2b", "capabilities": ["chat"]},
            {"name": "chat-model:12b", "capabilities": ["chat"]},
        ],
    )

    async def list_models(_client: OllamaClient) -> list[dict[str, str]]:
        return [
            {"name": "embed-model:latest"},
            {"name": "chat-model:12b"},
        ]

    async def show_model(_client: OllamaClient, model: str) -> dict:
        return {
            "capabilities": (
                ["embedding"]
                if model == "embed-model:latest"
                else ["completion", "vision", "tools"]
            ),
            "model_info": {
                "llama.context_length": (
                    8_192 if model == "embed-model:latest" else 131_072
                )
            },
        }

    monkeypatch.setattr(OllamaClient, "list_models", list_models)
    monkeypatch.setattr(OllamaClient, "show_model", show_model)

    response = await services.providers.discover(profile.id)
    entries = response.models

    assert {entry.name for entry in entries} == {
        "embed-model:latest",
        "chat-model:12b",
    }
    by_name = {entry.name: entry.capabilities for entry in entries}
    assert by_name["embed-model:latest"] == {"embedding"}
    assert {
        entry.name: entry.context_window_tokens
        for entry in entries
    } == {
        "embed-model:latest": 8_192,
        "chat-model:12b": 131_072,
    }
    resolved = services.model_resolver.resolve_agent_model(
        ModelReference(
            provider_profile_id=profile.id,
            model="chat-model:12b",
        )
    )
    assert resolved.context_window_tokens == 131_072
    assert by_name["chat-model:12b"] == {"chat", "vision", "tools"}
    assert {
        item["name"] for item in repository.get(profile.id).models_json
    } == {
        "embed-model:latest",
        "chat-model:12b",
    }


def test_partial_model_references_never_mix_profiles_and_models(test_settings) -> None:
    services = create_services(test_settings)
    default_profile_id = next(
        profile.id for profile in services.model_runtime.profiles() if profile.name == "Default Ollama"
    )
    agent_model_defaults = AgentModelDefaults(
        chat=ModelReference(provider_profile_id=default_profile_id, model="agent-model")
    )

    resolved = services.model_runtime.resolve(
        "chat",
        model_reference=ModelReference(model="selected-model"),
        agent_model_defaults=agent_model_defaults,
    )
    assert resolved.profile_id == default_profile_id
    assert resolved.model == "selected-model"

    with pytest.raises(ProviderRuntimeError, match="no model name"):
        services.model_runtime.resolve(
            "chat",
            model_reference=ModelReference(provider_profile_id=default_profile_id),
            agent_model_defaults=agent_model_defaults,
        )


def test_profile_verification_uses_the_models_declared_capability(test_settings, stub_provider) -> None:
    client = TestClient(create_app(test_settings))
    profile = client.post(
        "/api/providers",
        json={
            "name": "Embedding gateway",
            "kind": "openai_compatible",
            "base_url": f"{stub_provider.base_url}/v1",
            "api_key": "test-key",
            "models": [{"name": "embed-model", "capabilities": ["embedding"]}],
        },
    ).json()

    result = client.post(f"/api/providers/{profile['id']}/verify", json={"model": "embed-model"})

    assert result.status_code == 200
    assert result.json()["reachable"] is True
    assert result.json()["tool_calling"] is False
    assert "embedding vector" in result.json()["detail"]
    assert stub_provider.requests[-1]["model"] == "embed-model"


def test_openai_compatible_vision_request_uses_image_content(test_settings, stub_provider) -> None:
    app = create_app(test_settings)
    client = TestClient(app)
    profile = client.post(
        "/api/providers",
        json={
            "name": "Vision gateway",
            "kind": "openai_compatible",
            "base_url": f"{stub_provider.base_url}/v1",
            "api_key": "test-key",
            "models": [{"name": "vision-model", "capabilities": ["vision"]}],
        },
    ).json()
    resolved = app.state.services.model_runtime.resolve(
        "vision",
        model_reference=ModelReference(provider_profile_id=profile["id"], model="vision-model"),
    )

    asyncio.run(
        app.state.services.model_runtime.generate(
            resolved,
            "Read this page.",
            images=["cGFnZQ=="],
            format_={"type": "object", "properties": {"text": {"type": "string"}}},
        )
    )

    request = stub_provider.requests[-1]
    content = request["messages"][-1]["content"]
    assert content[0] == {"type": "text", "text": "Read this page."}
    assert content[1]["image_url"]["url"] == "data:image/png;base64,cGFnZQ=="
    assert request["response_format"]["type"] == "json_schema"


def test_azure_profiles_use_the_expected_openai_clients(test_settings) -> None:
    services = create_services(test_settings)
    azure = ResolvedModel(
        profile_id="azure",
        profile_name="Azure OpenAI",
        kind="azure_openai",
        base_url="https://example.openai.azure.com/openai/v1",
        api_key="secret",
        model="deployment",
    )
    foundry = ResolvedModel(
        profile_id="foundry",
        profile_name="Azure Foundry",
        kind="azure_foundry",
        base_url="https://example.services.ai.azure.com/models",
        api_key="secret",
        model="deployment",
    )

    azure_client = services.model_runtime.client(azure)
    foundry_client = services.model_runtime.client(foundry)
    assert isinstance(azure_client, AsyncOpenAI)
    assert isinstance(foundry_client, AsyncOpenAI)
    assert str(azure_client.base_url) == "https://example.openai.azure.com/openai/v1/"
    assert str(foundry_client.base_url) == "https://example.services.ai.azure.com/models/"
    asyncio.run(azure_client.close())
    asyncio.run(foundry_client.close())


def test_azure_openai_profile_does_not_accept_api_version() -> None:
    profile = ProviderCreate.model_validate(
        {
            "name": "Azure v1",
            "kind": "azure_openai",
            "base_url": "https://example.openai.azure.com/openai/v1",
            "api_key": "secret",
        }
    )

    assert profile.kind == "azure_openai"
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        ProviderCreate.model_validate(
            {
                **profile.model_dump(mode="json"),
                "api_version": "2024-10-21",
            }
        )
