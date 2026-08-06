from __future__ import annotations

import time

from fastapi.testclient import TestClient

from backend.app import create_app


def configure_provider(client: TestClient, stub_provider) -> str:
    response = client.post(
        "/api/providers",
        json={
            "name": "Test provider",
            "kind": "ollama",
            "base_url": stub_provider.base_url,
            "models": [
                {
                    "name": "stub-model",
                    "capabilities": ["chat", "tools"],
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    profile_id = response.json()["id"]
    response = client.put(
        "/api/settings",
        json={
            "default_model_references": {
                "chat": {
                    "provider_profile_id": profile_id,
                    "model": "stub-model",
                },
                "tools": {
                    "provider_profile_id": profile_id,
                    "model": "stub-model",
                },
            }
        },
    )
    assert response.status_code == 200, response.text
    return profile_id


def test_clean_sdk_api_has_no_node_or_workflow_routes(test_settings) -> None:
    app = create_app(test_settings)
    paths = set(app.openapi()["paths"])

    assert "/api/sdk/catalog" in paths
    assert "/api/agents" in paths
    assert "/api/tools" in paths
    assert "/api/conversations" in paths
    assert "/api/runs" in paths
    assert not any("workflow" in path for path in paths)
    assert not any(path == "/api/nodes" or "custom-nodes" in path for path in paths)


def test_agent_revision_and_runner_api(test_settings, stub_provider) -> None:
    app = create_app(test_settings)
    with TestClient(app) as client:
        configure_provider(client, stub_provider)
        blueprint = {
            "name": "SDK researcher",
            "entry_agent_id": "researcher",
            "agents": [
                {
                    "id": "researcher",
                    "name": "Researcher",
                    "instructions": "Answer the user's question.",
                }
            ],
        }
        response = client.post(
            "/api/agents",
            json={"blueprint": blueprint, "presentation": {"positions": {}}},
        )
        assert response.status_code == 201, response.text
        agent = response.json()
        assert agent["latest_revision"]["blueprint"]["agents"][0]["id"] == "researcher"

        run_response = client.post(
            "/api/runs",
            json={
                "agent_revision_id": agent["latest_revision"]["id"],
                "input": "What is ScholarWeave?",
            },
        )
        assert run_response.status_code == 202, run_response.text
        run_id = run_response.json()["id"]

        deadline = time.monotonic() + 10
        run = None
        while time.monotonic() < deadline:
            run = client.get(f"/api/runs/{run_id}").json()
            if run["status"] in {"completed", "failed"}:
                break
            time.sleep(0.05)

        assert run is not None
        assert run["status"] == "completed", run
        assert run["final_output"] == "Stub answer."
        assert any(item["type"] == "message_output_item" for item in run["items"])
        assert any(event["event_type"] == "run.completed" for event in run["events"])


def test_function_tool_authoring_api(test_settings) -> None:
    app = create_app(test_settings)
    with TestClient(app) as client:
        definition = {
            "name": "uppercase",
            "description": "Uppercase text.",
            "parameters_schema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
            "output_schema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
            "code": "def invoke(arguments):\n    return {'text': arguments['text'].upper()}\n",
            "requires_approval": False,
        }
        response = client.post("/api/tools", json=definition)
        assert response.status_code == 201, response.text
        assert response.json()["latest_revision"]["catalog_id"].startswith("custom:")

        tested = client.post(
            "/api/tools/test",
            json={"definition": definition, "arguments": {"text": "paper"}},
        )
        assert tested.status_code == 200, tested.text
        assert tested.json()["output"] == {"text": "PAPER"}


def test_input_guardrail_tripwire_is_projected_to_run_events(
    test_settings,
    stub_provider,
) -> None:
    app = create_app(test_settings)
    with TestClient(app) as client:
        configure_provider(client, stub_provider)
        response = client.post(
            "/api/agents",
            json={
                "blueprint": {
                    "name": "Guarded agent",
                    "entry_agent_id": "agent",
                    "agents": [
                        {
                            "id": "agent",
                            "name": "Agent",
                            "instructions": "Answer briefly.",
                            "input_guardrail_ids": ["short-input"],
                        }
                    ],
                    "guardrails": [
                        {
                            "id": "short-input",
                            "kind": "input",
                            "catalog_id": "content.max_characters",
                            "config": {"max_characters": 5},
                        }
                    ],
                }
            },
        )
        assert response.status_code == 201, response.text
        revision_id = response.json()["latest_revision"]["id"]

        started = client.post(
            "/api/runs",
            json={
                "agent_revision_id": revision_id,
                "input": "This input is too long.",
            },
        )
        assert started.status_code == 202, started.text
        run_id = started.json()["id"]
        deadline = time.monotonic() + 10
        run = None
        while time.monotonic() < deadline:
            run = client.get(f"/api/runs/{run_id}").json()
            if run["status"] in {"completed", "failed"}:
                break
            time.sleep(0.05)

        assert run is not None
        assert run["status"] == "failed"
        tripwire = next(
            event
            for event in run["events"]
            if event["event_type"] == "guardrail.tripwire"
        )
        assert tripwire["payload"]["guardrail_name"] == "short-input"
        assert tripwire["payload"]["tripwire_triggered"] is True


def test_builder_is_sdk_agent_with_persisted_session(
    test_settings,
    stub_provider,
) -> None:
    app = create_app(test_settings)
    with TestClient(app) as client:
        profile_id = configure_provider(client, stub_provider)
        generated_blueprint = {
            "name": "Generated analyst",
            "entry_agent_id": "analyst",
            "agents": [
                {
                    "id": "analyst",
                    "name": "Analyst",
                    "instructions": "Analyze the supplied research question.",
                }
            ],
        }
        stub_provider.tool_plans = [
            (
                "Build an analyst agent",
                "create_builder_todo_plan",
                {
                    "tasks": [
                        {"id": "draft", "title": "Draft the analyst blueprint"},
                        {"id": "save", "title": "Validate and save the analyst"},
                    ]
                },
            ),
            (
                "Build an analyst agent",
                "update_builder_todo",
                {"id": "draft", "status": "completed", "note": "Blueprint drafted."},
            ),
            (
                "Build an analyst agent",
                "list_sdk_primitives",
                {},
            ),
            (
                "Build an analyst agent",
                "validate_agent_blueprint",
                {"blueprint": generated_blueprint},
            ),
            (
                "Build an analyst agent",
                "save_agent_blueprint",
                {
                    "agent_id": None,
                    "blueprint": generated_blueprint,
                    "presentation": {"positions": {"analyst": [120, 120]}},
                },
            ),
            (
                "Build an analyst agent",
                "update_builder_todo",
                {"id": "save", "status": "completed", "note": "Save receipt received."},
            ),
            (
                "Build an analyst agent",
                "finish_builder_run",
                {"outcome": "saved", "summary": "Saved the analyst agent."},
            ),
        ]
        conversation_response = client.post(
            "/api/builder/conversations",
            json={
                "title": "Build analyst",
                "model_reference": {
                    "provider_profile_id": profile_id,
                    "model": "stub-model",
                },
            },
        )
        assert conversation_response.status_code == 201, conversation_response.text
        conversation_id = conversation_response.json()["id"]

        message_response = client.post(
            f"/api/builder/conversations/{conversation_id}/messages",
            json={"content": "Build an analyst agent"},
        )
        assert message_response.status_code == 202, message_response.text
        run_id = message_response.json()["run"]["id"]

        deadline = time.monotonic() + 10
        run = None
        while time.monotonic() < deadline:
            run = client.get(f"/api/runs/{run_id}").json()
            if run["status"] in {"completed", "failed"}:
                break
            time.sleep(0.05)

        assert run is not None
        assert run["status"] == "completed", run
        assert "save_agent_blueprint" in stub_provider.tools_offered
        assert {
            request["model"]
            for request in stub_provider.requests
            if request.get("messages")
        } == {"stub-model"}
        saved = client.get("/api/agents").json()
        assert [agent["name"] for agent in saved] == ["Generated analyst"]

        conversation = client.get(
            f"/api/builder/conversations/{conversation_id}"
        )
        assert conversation.status_code == 200, conversation.text
        items = conversation.json()["items"]
        assert any(item["role"] == "user" for item in items)
        assert any(item["role"] == "assistant" for item in items)
        assert any(
            event["event_type"] == "builder.todos.updated"
            for event in run["events"]
        )


def test_builder_completes_informational_message_without_saving(
    test_settings,
    stub_provider,
) -> None:
    app = create_app(test_settings)
    with TestClient(app) as client:
        profile_id = configure_provider(client, stub_provider)
        stub_provider.tool_plans = [
            (
                "Hi",
                "finish_builder_run",
                {
                    "outcome": "informational",
                    "summary": "Hello! What would you like to build?",
                },
            ),
        ]
        conversation = client.post(
            "/api/builder/conversations",
            json={
                "title": "Builder greeting",
                "model_reference": {
                    "provider_profile_id": profile_id,
                    "model": "stub-model",
                },
            },
        ).json()

        response = client.post(
            f"/api/builder/conversations/{conversation['id']}/messages",
            json={"content": "Hi"},
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run"]["id"]

        deadline = time.monotonic() + 10
        run = None
        while time.monotonic() < deadline:
            run = client.get(f"/api/runs/{run_id}").json()
            if run["status"] in {"completed", "failed"}:
                break
            time.sleep(0.05)

        assert run is not None
        assert run["status"] == "completed", run
        assert not any(
            event["event_type"] == "builder.todos.updated"
            for event in run["events"]
        )
        assert client.get("/api/agents").json() == []


def test_builder_does_not_complete_without_todos_and_save(
    test_settings,
    stub_provider,
) -> None:
    app = create_app(test_settings)
    with TestClient(app) as client:
        profile_id = configure_provider(client, stub_provider)
        conversation = client.post(
            "/api/builder/conversations",
            json={
                "title": "Incomplete build",
                "model_reference": {
                    "provider_profile_id": profile_id,
                    "model": "stub-model",
                },
            },
        ).json()

        response = client.post(
            f"/api/builder/conversations/{conversation['id']}/messages",
            json={"content": "Build an agent without using tools"},
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run"]["id"]

        deadline = time.monotonic() + 10
        run = None
        while time.monotonic() < deadline:
            run = client.get(f"/api/runs/{run_id}").json()
            if run["status"] in {"completed", "failed"}:
                break
            time.sleep(0.05)

        assert run is not None
        assert run["status"] == "failed"
        assert "No successful builder completion was recorded" in run["error"]
