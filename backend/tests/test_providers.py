from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from openai import AsyncAzureOpenAI, AsyncOpenAI

from backend.app import create_app, create_backend_services
from backend.config import Settings
from backend.provider_runtime import ProviderRuntimeError, ResolvedModel
from backend.schemas import ModelReference, WorkflowDefinition, WorkflowModelDefaults


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
    assert created.status_code == 200
    body = created.json()
    assert body["api_key_set"] is True
    assert "api_key" not in body
    assert "do-not-return-me" not in str(body)

    profile_id = body["id"]
    updated = client.put(
        f"/api/providers/{profile_id}",
        json={"name": "Renamed gateway", "models": [{"name": "embed-1", "capabilities": ["embedding"]}]},
    )
    assert updated.status_code == 200
    assert updated.json()["models"] == [{"name": "embed-1", "capabilities": ["embedding"]}]

    restarted = TestClient(
        create_app(
            Settings(
                data_dir=test_settings.data_dir,
                workspace_dir=test_settings.workspace_dir,
                frontend_dist_dir=test_settings.frontend_dist_dir,
            )
        )
    )
    assert restarted.get(f"/api/providers/{profile_id}").json()["api_key_set"] is True
    archived = restarted.delete(f"/api/providers/{profile_id}")
    assert archived.status_code == 200
    assert archived.json()["state"] == "archived"
    assert profile_id not in {profile["id"] for profile in restarted.get("/api/providers").json()}


def test_provider_validation_does_not_reflect_api_keys(test_settings) -> None:
    client = TestClient(create_app(test_settings))
    secret = "never-reflect-this-key"
    response = client.post(
        "/api/providers",
        json={"name": "Bad key", "kind": "openai", "base_url": "https://api.example.test/v1", "api_key": secret * 1000},
    )
    assert response.status_code == 422
    assert secret not in response.text


def test_migrates_legacy_defaults_into_default_ollama_profile(test_settings) -> None:
    settings = Settings(
        data_dir=test_settings.data_dir,
        workspace_dir=test_settings.workspace_dir,
        frontend_dist_dir=test_settings.frontend_dist_dir,
        ollama_base_url="http://ollama.test:11434",
        default_generation_model="legacy-chat",
        default_embedding_model="legacy-embed",
    )
    services = create_backend_services(settings)

    profile = next(profile for profile in services.model_runtime.profiles() if profile.name == "Default Ollama")
    assert profile.base_url == "http://ollama.test:11434"
    assert {entry["name"] for entry in profile.models_json} == {"legacy-chat", "legacy-embed"}
    assert services.settings.default_model_references["chat"]["provider_profile_id"] == profile.id
    assert services.settings.default_model_references["embedding"]["model"] == "legacy-embed"


def test_compatible_discovery_and_model_resolution(test_settings, stub_provider) -> None:
    app = create_app(test_settings)
    client = TestClient(app)
    created = client.post(
        "/api/providers",
        json={
            "name": "Stub compatible",
            "kind": "openai_compatible",
            "base_url": f"{stub_provider.base_url}/v1",
            "api_key": "test-key",
            "models": [{"name": "manual-embed", "capabilities": ["embedding"]}],
        },
    )
    assert created.status_code == 200
    profile = created.json()
    discovered = client.get(f"/api/providers/{profile['id']}/models")
    assert discovered.status_code == 200
    assert {entry["name"] for entry in discovered.json()["models"]} == {"stub-model", "manual-embed"}
    verified = client.post(f"/api/providers/{profile['id']}/verify", json={"model": "stub-model"})
    assert verified.status_code == 200
    assert verified.json()["reachable"] is True

    runtime = app.state.services.model_runtime
    workflow_defaults = WorkflowModelDefaults(chat=ModelReference(provider_profile_id=profile["id"], model="workflow-chat"))
    assert runtime.resolve("chat", workflow_defaults=workflow_defaults).model == "workflow-chat"
    assert runtime.resolve(
        "chat",
        node_reference=ModelReference(provider_profile_id=profile["id"], model="node-chat"),
        workflow_defaults=workflow_defaults,
    ).model == "node-chat"


def test_partial_model_references_never_mix_profiles_and_models(test_settings) -> None:
    services = create_backend_services(test_settings)
    default_profile_id = next(
        profile.id for profile in services.model_runtime.profiles() if profile.name == "Default Ollama"
    )
    workflow_defaults = WorkflowModelDefaults(
        chat=ModelReference(provider_profile_id=default_profile_id, model="workflow-model")
    )

    resolved = services.model_runtime.resolve(
        "chat",
        node_reference=ModelReference(model="legacy-node-model"),
        workflow_defaults=workflow_defaults,
    )
    assert resolved.profile_id == default_profile_id
    assert resolved.model == "legacy-node-model"

    with pytest.raises(ProviderRuntimeError, match="no model name"):
        services.model_runtime.resolve(
            "chat",
            node_reference=ModelReference(provider_profile_id=default_profile_id),
            workflow_defaults=workflow_defaults,
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


def test_agent_node_uses_its_own_provider_profile(test_settings, stub_provider) -> None:
    services = create_backend_services(test_settings)
    with services.session_factory() as session:
        from backend.models import ProviderProfile

        profile = ProviderProfile(
            name="Agent stub",
            kind="openai_compatible",
            base_url=f"{stub_provider.base_url}/v1",
            api_key="test-key",
            state="active",
        )
        session.add(profile)
        session.commit()
        session.refresh(profile)
        profile_id = profile.id
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "Per-agent provider",
            "nodes": [
                {"id": "input", "type": "text_input", "config": {"value": "hello"}},
                {
                    "id": "agent",
                    "type": "agent",
                    "config": {
                        "name": "Remote",
                        "instructions": "Reply briefly.",
                        "provider_profile_id": profile_id,
                        "model": "stub-model",
                    },
                },
                {"id": "output", "type": "final_output"},
            ],
            "edges": [
                {"source_node_id": "input", "source_port": "text", "target_node_id": "agent", "target_port": "input"},
                {"source_node_id": "agent", "source_port": "text", "target_node_id": "output", "target_port": "content"},
            ],
        }
    )
    run = asyncio.run(_wait_for_run(services, workflow))
    assert run.status == "completed", run.error
    assert run.output_json == "Stub answer."
    assert stub_provider.requests[0]["model"] == "stub-model"


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
        node_reference=ModelReference(provider_profile_id=profile["id"], model="vision-model"),
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
    services = create_backend_services(test_settings)
    azure = ResolvedModel(
        profile_id="azure",
        profile_name="Azure OpenAI",
        kind="azure_openai",
        base_url="https://example.openai.azure.com",
        api_version="2024-10-21",
        api_key="secret",
        model="deployment",
    )
    foundry = ResolvedModel(
        profile_id="foundry",
        profile_name="Azure Foundry",
        kind="azure_foundry",
        base_url="https://example.services.ai.azure.com/models",
        api_version=None,
        api_key="secret",
        model="deployment",
    )

    azure_client = services.model_runtime.client(azure)
    foundry_client = services.model_runtime.client(foundry)
    assert isinstance(azure_client, AsyncAzureOpenAI)
    assert isinstance(foundry_client, AsyncOpenAI)
    assert str(foundry_client.base_url) == "https://example.services.ai.azure.com/models/"
    asyncio.run(azure_client.close())
    asyncio.run(foundry_client.close())


async def _wait_for_run(services, workflow: WorkflowDefinition):
    run = await services.executor.start_run(workflow, {})
    for _ in range(100):
        await asyncio.sleep(0.05)
        current = services.executor.load_run(run.id)
        if current and current.status in {"completed", "failed", "cancelled"}:
            return current
    raise AssertionError("Run did not finish")
