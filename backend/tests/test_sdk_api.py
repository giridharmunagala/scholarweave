from __future__ import annotations

import time

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.conversations.autonomous import RESEARCH_TOOL_IDS


def configure_provider(client: TestClient, stub_provider) -> str:
    response = client.post(
        "/api/providers",
        json={
            "name": "Test provider",
            "kind": "ollama",
            "base_url": stub_provider.base_url,
            "models": [{"name": "stub-model", "capabilities": ["chat", "tools"]}],
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
                }
            }
        },
    )
    assert response.status_code == 200, response.text
    return profile_id


def wait_for_run(client: TestClient, run_id: str) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] in {"completed", "failed", "cancelled"}:
            return run
        time.sleep(0.05)
    raise AssertionError(f"Run {run_id} did not finish.")


def test_public_api_is_research_only(test_settings) -> None:
    app = create_app(test_settings)
    paths = set(app.openapi()["paths"])

    assert "/api/agent/conversations" in paths
    assert "/api/deep-work/conversations" in paths
    assert "/api/documents" in paths
    assert "/api/workspace/files" in paths
    assert not any(path.startswith("/api/agents") for path in paths)
    assert not any(path.startswith("/api/tools") for path in paths)
    assert not any(path.startswith("/api/builder") for path in paths)
    assert not any(path.startswith("/api/research-agents") for path in paths)


def test_conversation_history_does_not_require_its_model_to_be_available(
    test_settings,
) -> None:
    app = create_app(test_settings)
    with TestClient(app) as client:
        conversation = client.post(
            "/api/agent/conversations",
            json={
                "title": "Unavailable model",
                "model_reference": {
                    "provider_profile_id": "removed-provider",
                    "model": "removed-model",
                },
            },
        ).json()

        detail = client.get(f"/api/agent/conversations/{conversation['id']}")

        assert detail.status_code == 200, detail.text
        assert detail.json()["items"] == []


def test_research_agent_uses_only_the_lean_tool_surface(
    test_settings,
    stub_provider,
) -> None:
    app = create_app(test_settings)
    with TestClient(app) as client:
        profile_id = configure_provider(client, stub_provider)
        stub_provider.tool_plans = [
            (
                "Research saved notes",
                "search_research_notes",
                {"query": None, "kinds": [], "tags": [], "limit": 10},
            )
        ]
        conversation = client.post(
            "/api/agent/conversations",
            json={
                "title": "Research papers",
                "model_reference": {
                    "provider_profile_id": profile_id,
                    "model": "stub-model",
                },
            },
        ).json()
        assert conversation["kind"] == "autonomous"

        response = client.post(
            f"/api/agent/conversations/{conversation['id']}/messages",
            json={
                "content": "Research saved notes",
                "reasoning_effort": "medium",
                "web_enabled": True,
                "fast_answer": False,
                "web_search_limit": 1,
            },
        )
        assert response.status_code == 202, response.text
        run = wait_for_run(client, response.json()["run"]["id"])

        assert run["status"] == "completed", run
        assert set(stub_provider.tools_offered) == {
            "set_conversation_title",
            "search_research_sources",
            "acquire_research_source",
            "search_research_library",
            "organize_research_library",
            "read_research_paper",
            "read_research_web_page",
            "search_research_notes",
            "read_research_note",
            "save_research_note",
            "read_paper_summary_batch",
            "paper_summary_checkpoint",
            "save_paper_summary_version",
            "read_tool_result",
        }
        record = app.state.services.runs.get(run["id"])
        assert len(record.blueprint_json["tools"]) == len(RESEARCH_TOOL_IDS) + 1
        snapshot_response = client.get(f"/api/runs/{run['id']}/prompt-snapshot")
        assert snapshot_response.status_code == 200, snapshot_response.text
        snapshot = snapshot_response.json()
        assert snapshot["run_id"] == run["id"]
        assert snapshot["prompt_revision"]
        assert snapshot["agents"][0]["effective_instructions"]
        assert {tool["name"] for tool in snapshot["tools"]} == {
            *stub_provider.tools_offered,
            "paper_summary_checkpoint",
            "read_paper_summary_batch",
            "save_research_note",
            "save_paper_summary_version",
        }
        assert record.blueprint_json["run"] == {
            "max_turns": 16,
            "max_tool_concurrency": 4,
            "tracing_enabled": False,
        }


def test_fast_answer_message_uses_bounded_web_blueprint(
    test_settings,
    stub_provider,
) -> None:
    app = create_app(test_settings)
    with TestClient(app) as client:
        profile_id = configure_provider(client, stub_provider)
        conversation = client.post(
            "/api/agent/conversations",
            json={
                "title": "Fast answer",
                "model_reference": {
                    "provider_profile_id": profile_id,
                    "model": "stub-model",
                },
            },
        ).json()

        response = client.post(
            f"/api/agent/conversations/{conversation['id']}/messages",
            json={
                "content": "Find one current source",
                "web_enabled": True,
                "fast_answer": True,
                "web_search_limit": 3,
            },
        )

        assert response.status_code == 202, response.text
        record = app.state.services.runs.get(response.json()["run"]["id"])
        entry = record.blueprint_json["agents"][0]
        assert "Make at most 3 external" in entry["instructions"]
        assert set(entry["tool_ids"]) == {
            "set-title",
            "search-sources",
            "acquire-source",
            "read-web-page",
        }


def test_deep_work_has_separate_history_and_bounded_worker(
    test_settings,
    stub_provider,
) -> None:
    stub_provider.tool_plans = [
        (
            "Give a direct answer",
            "create_work_plan",
            {"items": [{"id": "answer", "title": "Answer the request"}]},
        ),
        (
            "Autonomous work is not finished",
            "update_work_item",
            {
                "id": "answer",
                "status": "completed",
                "summary": "Prepared the direct answer.",
            },
        ),
    ]
    app = create_app(test_settings)
    with TestClient(app) as client:
        profile_id = configure_provider(client, stub_provider)
        conversation = client.post(
            "/api/deep-work/conversations",
            json={
                "title": "Deep comparison",
                "model_reference": {
                    "provider_profile_id": profile_id,
                    "model": "stub-model",
                },
            },
        ).json()
        assert conversation["kind"] == "deep_work"
        assert client.get("/api/agent/conversations").json() == []
        assert [item["id"] for item in client.get("/api/deep-work/conversations").json()] == [
            conversation["id"]
        ]

        response = client.post(
            f"/api/deep-work/conversations/{conversation['id']}/messages",
            json={"content": "Give a direct answer when delegation is unnecessary."},
        )
        assert response.status_code == 202, response.text
        run = wait_for_run(client, response.json()["run"]["id"])
        assert run["status"] == "completed", run

        stored_run = app.state.services.runs.get(run["id"])
        assert any(
            event.event_type == "run.epoch.completed"
            and event.payload_json.get("terminal_reason") == "work_pending"
            for event in stored_run.events
        )
        blueprint = stored_run.blueprint_json
        assert blueprint["name"] == "ScholarWeave deep work"
        assert [agent["id"] for agent in blueprint["agents"]] == [
            "coordinator",
            "worker",
        ]
        assert [tool["tool_name"] for tool in blueprint["agent_tools"]] == [
            "focused_research_worker",
        ]
        assert blueprint["agent_tools"][0]["max_turns"] == 24
        assert blueprint["run"]["max_turns"] == 48
        assert blueprint["run"]["max_tool_concurrency"] == 4
