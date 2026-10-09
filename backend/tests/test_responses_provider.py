from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.providers.types import ModelReference
from backend.tests.test_api import wait_for_run


def create_profile(client, stub_provider, **overrides):
    response = client.post("/api/providers", json={
        "name": "Responses deployment", "kind": "openai_compatible",
        "base_url": f"{stub_provider.base_url}/v1", "api_mode": "responses",
        "default_reasoning_effort": "high",
        "models": [{"name": "stub-model", "capabilities": ["chat", "tools", "vision"]}],
        **overrides,
    })
    assert response.status_code == 201, response.text
    return response.json()


def test_provider_api_mode_persists_and_rejects_invalid_ollama_mode(test_settings, stub_provider):
    with TestClient(create_app(test_settings)) as client:
        profile = create_profile(client, stub_provider)
        path = f"/api/providers/{profile['id']}"
        assert profile["api_mode"] == "responses"
        assert client.put(path, json={"name": "Renamed"}).json()["api_mode"] == "responses"
        assert client.put(path, json={"api_mode": None}).status_code == 422
        assert client.put(path, json={"api_mode": "invalid"}).status_code == 422
        resolved = client.app.state.services.model_runtime.resolve(
            "chat", model_reference=ModelReference(provider_profile_id=profile["id"], model="stub-model"),
        )
        assert resolved.api_mode == "responses"
        assert client.put(path, json={"kind": "ollama"}).status_code == 400
        assert client.get(path).json()["api_mode"] == "responses"
        assert client.post("/api/providers", json={
            "name": "Invalid Ollama", "kind": "ollama", "base_url": stub_provider.base_url,
            "api_mode": "responses",
        }).status_code == 422
        assert client.put(path, json={"api_mode": "chat_completions"}).json()["api_mode"] == "chat_completions"
    with TestClient(create_app(test_settings)) as client:
        assert client.get(path).json()["api_mode"] == "chat_completions"


def test_responses_verification_preserves_reasoning_and_application_functions(test_settings, stub_provider):
    stub_provider.reject_reasoning_with_tools = True
    stub_provider.call_tool = "record_colour"
    stub_provider.tool_arguments = {"colour": "teal"}
    with TestClient(create_app(test_settings)) as client:
        profile = create_profile(client, stub_provider)
        verified = client.post(f"/api/providers/{profile['id']}/verify", json={"model": "stub-model"})
        assert verified.status_code == 200, verified.text
        assert verified.json()["reachable"] and verified.json()["tool_calling"], verified.text
        assert len(stub_provider.response_api_requests) == 2
        for request in stub_provider.response_api_requests:
            assert request["store"] is False
            assert request["reasoning"]["effort"] == "high"
            assert "messages" not in request and "reasoning_effort" not in request
            assert all(tool["type"] == "function" and "function" not in tool for tool in request["tools"])
        second = stub_provider.response_api_requests[1]
        assert any(item["type"] == "function_call_output" for item in second["input"])
        assert any(item.get("encrypted_content") == "opaque-stub-reasoning" for item in second["input"])


@pytest.mark.parametrize("effort", ["auto", "thorough"])
def test_responses_chat_tool_roundtrip_usage_and_local_history(test_settings, stub_provider, effort):
    with TestClient(create_app(test_settings)) as client:
        profile = create_profile(client, stub_provider)
        note = client.app.state.services.workspace.create_note(name="Existing evidence", content="Keep source qualifiers.")
        goal = "Read this local note and explain its constraint."
        stub_provider.tool_plans = [(goal, "read_research_note", {"path": note.path})]
        conversation = client.post("/api/agent/conversations", json={
            "model_reference": {"provider_profile_id": profile["id"], "model": "stub-model"},
        }).json()
        path = f"/api/agent/conversations/{conversation['id']}/messages"
        response = client.post(path, json={
            "content": goal, "response_effort": effort, "reasoning_effort": "high", "web_enabled": False,
        })
        assert response.status_code == 202, response.text
        run = wait_for_run(client, response.json()["run"]["id"])
        assert run["status"] == "completed", run["error"]
        assert run["final_output"] == stub_provider.reply
        assert run["usage"]["input_tokens"] == 20
        assert run["usage"]["output_tokens"] == 10
        assert len(stub_provider.response_api_requests) == 2
        assert client.app.state.services.workspace.read_file(note.path).content == note.content
        response = client.post(path, json={"content": "Discuss this further.", "reasoning_effort": "high"})
        run = wait_for_run(client, response.json()["run"]["id"])
        assert run["status"] == "completed", run["error"]
        latest = stub_provider.response_api_requests[-1]
        assert latest["store"] is False and "previous_response_id" not in latest
        assert any(item.get("encrypted_content") == "opaque-stub-reasoning" for item in latest["input"])
        assert latest["reasoning"]["effort"] == "high"
        entries = [json.loads(line) for line in test_settings.llm_log_path.read_text().splitlines()]
        responses = [entry for entry in entries if entry["operation"].endswith("/responses")]
        assert responses
        assert all(entry["response"]["object"] == "response" for entry in responses)
        assert all(entry["response"]["usage"]["input_tokens_details"]["cached_tokens"] == 4 for entry in responses)
        assert client.put(f"/api/providers/{profile['id']}", json={"api_mode": "chat_completions"}).status_code == 200
        response = client.post(path, json={"content": "Continue on Chat Completions."})
        run = wait_for_run(client, response.json()["run"]["id"])
        assert run["status"] == "completed", run["error"]
        request = stub_provider.requests[-1]
        assert "messages" in request and "input" not in request
        assert "opaque-stub-reasoning" not in json.dumps(request)


@pytest.mark.parametrize("status", ["completed", "failed", "incomplete"])
def test_responses_audit_log_retains_final_response_shape(status):
    from backend.providers.logging import _combine_sse_chunks

    response = {"id": "resp_test", "object": "response", "status": status, "output": []}
    chunk = {"type": f"response.{status}", "response": response}
    assert _combine_sse_chunks(f"event: response.{status}\ndata: {json.dumps(chunk)}\n\n") == response


@pytest.mark.anyio
async def test_responses_generate_handles_images_and_json_output(test_settings, stub_provider):
    from backend.bootstrap import create_services
    from backend.providers.schemas import ProviderCreate, ProviderModel

    services = create_services(test_settings)
    try:
        profile = services.providers.create(ProviderCreate(
            name="Responses", kind="openai_compatible", base_url=f"{stub_provider.base_url}/v1",
            api_mode="responses", default_reasoning_effort="high",
            models=[ProviderModel(name="stub-model", capabilities={"chat", "vision"})],
        ))
        resolved = services.model_runtime.resolve("vision", model_reference=ModelReference(
            provider_profile_id=profile.id, model="stub-model",
        ))
        stub_provider.reply = '{"finding":"supported"}'
        tokens = []

        async def collect(text):
            tokens.append(text)

        result = await services.model_runtime.generate(
            resolved, prompt="Describe this image.", images=["aW1hZ2U="],
            format_={"type": "object", "properties": {"finding": {"type": "string"}},
                     "required": ["finding"], "additionalProperties": False},
            on_token=collect,
        )
        assert result["response"] == stub_provider.reply
        assert "".join(tokens) == stub_provider.reply
        request = stub_provider.response_api_requests[-1]
        assert request["store"] is False
        assert request["text"]["format"]["type"] == "json_schema"
        assert request["reasoning"]["effort"] == "high"
        assert any(part["type"] == "input_image" for item in request["input"]
                   for part in item.get("content", []) if isinstance(part, dict))
    finally:
        await services.close()


@pytest.mark.anyio
@pytest.mark.parametrize("outcome", ["failed", "incomplete", "refusal"])
async def test_responses_generation_surfaces_invalid_outputs(test_settings, stub_provider, monkeypatch, outcome):
    from backend.bootstrap import create_services
    from backend.providers.schemas import ProviderCreate
    from backend.providers.types import ProviderRuntimeError

    original = stub_provider.response_api

    def invalid_response(payload):
        result = original(payload)
        if outcome == "refusal":
            result["output"] = [{
                "type": "message", "id": "msg_refusal", "role": "assistant", "status": "completed",
                "content": [{"type": "refusal", "refusal": "Cannot provide this output."}],
            }]
        else:
            result["status"] = outcome
            if outcome == "failed":
                result["error"] = {"code": "server_error", "message": "Deployment failed."}
            else:
                result["incomplete_details"] = {"reason": "max_output_tokens"}
        return result

    monkeypatch.setattr(stub_provider, "response_api", invalid_response)
    services = create_services(test_settings)
    try:
        profile = services.providers.create(ProviderCreate(
            name="Responses", kind="openai_compatible",
            base_url=f"{stub_provider.base_url}/v1", api_mode="responses",
        ))
        resolved = services.model_runtime.resolve("chat", model_reference=ModelReference(
            provider_profile_id=profile.id, model="stub-model",
        ))
        with pytest.raises(ProviderRuntimeError):
            await services.model_runtime.generate(resolved, "Describe the evidence.")
    finally:
        await services.close()
