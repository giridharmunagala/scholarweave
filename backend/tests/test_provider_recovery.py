from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import AsyncOpenAI

from backend.app import create_app
from backend.providers.schemas import ProviderModel
from backend.providers.types import ModelReference, ProviderRuntimeError


def test_archived_provider_restores_original_identity_defaults_and_configuration(test_settings):
    with TestClient(create_app(test_settings)) as client:
        created = client.post("/api/providers", json={
            "name": "Recoverable gateway",
            "kind": "openai_compatible",
            "base_url": "http://127.0.0.1:9000/v1",
            "api_key": "local-test-key",
            "api_mode": "responses",
            "tool_reasoning_effort": "none",
            "models": [{
                "name": "manual-chat", "capabilities": ["chat", "tools"],
                "context_window_tokens": 65536,
            }],
        })
        assert created.status_code == 201
        original = created.json()
        path = f"/api/providers/{original['id']}"
        reference = {"provider_profile_id": original["id"], "model": "manual-chat"}
        assert client.put("/api/settings", json={
            "default_model_references": {"chat": reference},
        }).status_code == 200
        assert client.delete(path).json()["state"] == "archived"
        assert original["id"] not in {item["id"] for item in client.get("/api/providers").json()}
        archived = next(
            item for item in client.get("/api/providers?include_archived=true").json()
            if item["id"] == original["id"]
        )
        assert archived["state"] == "archived"
        assert archived["models"] == original["models"]
        assert client.get("/api/settings").json()["default_model_references"]["chat"] == reference
        with pytest.raises(ProviderRuntimeError, match="archived"):
            client.app.state.services.model_runtime.resolve("chat")

    with TestClient(create_app(test_settings)) as client:
        restored = client.post(path + "/restore")
        assert restored.status_code == 200
        body = restored.json()
        assert body["state"] == "active"
        for key in ("id", "name", "created_at", "api_mode", "api_key_set", "models", "tool_reasoning_effort"):
            assert body[key] == original[key]
        runtime = client.app.state.services.model_runtime
        resolved = runtime.resolve("chat")
        assert resolved.profile_id == original["id"]
        assert resolved.model == "manual-chat"
        assert resolved.api_key == "local-test-key"
        assert resolved.context_window_tokens == 65536
        assert client.post(path + "/restore").json()["id"] == original["id"]
        assert client.post("/api/providers/missing/restore").status_code == 404


def test_manual_model_registration_works_without_a_model_listing_endpoint(test_settings, monkeypatch):
    requests = []

    def unsupported_listing(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(404, json={"error": {"message": "Model listing unavailable"}})

    with TestClient(create_app(test_settings)) as client:
        runtime = client.app.state.services.model_runtime
        monkeypatch.setattr(runtime, "client", lambda resolved: AsyncOpenAI(
            base_url=resolved.base_url,
            api_key="unused",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(unsupported_listing)),
        ))
        created = client.post("/api/providers", json={
            "name": "Manual gateway", "kind": "openai_compatible",
            "base_url": "http://provider.invalid/v1", "models": [],
        }).json()
        path = f"/api/providers/{created['id']}"
        assert client.get(path + "/models").json()["discovery_error"]
        model = ProviderModel(
            name="private-deployment", capabilities={"chat", "tools"},
            context_window_tokens=131072,
        )
        saved = client.put(path, json={"models": [model.model_dump(mode="json")]})
        assert saved.status_code == 200
        assert saved.json()["models"][0]["name"] == model.name
        discovered = client.get(path + "/models").json()
        assert discovered["discovery_error"]
        assert discovered["models"][0]["name"] == model.name
        persisted = client.get(path).json()["models"]
        assert persisted[0]["context_window_tokens"] == 131072
        resolved = runtime.resolve("chat", model_reference=ModelReference(created["id"], model.name))
        assert resolved.model == model.name
        assert resolved.context_window_tokens == 131072
        assert len(requests) == 2
        assert all(request.url.path == "/v1/models" for request in requests)
