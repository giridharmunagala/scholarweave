from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.agents.context import ScholarWeaveContext
from backend.conversations.schemas import ConversationMessageRequest
from backend.conversations.turns import RESEARCH_TOOL_IDS
from backend.runs.schemas import SteeringMessageRequest


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


@pytest.mark.parametrize("request_type", [ConversationMessageRequest, SteeringMessageRequest])
def test_conversation_inputs_have_no_character_ceiling(request_type) -> None:
    content = "context-sized input " * 6_000
    assert len(content) > 100_000
    assert request_type(content=content).content == content
    with pytest.raises(ValueError):
        request_type(content="")


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


def test_deleting_chat_removes_run_files_and_caches_but_keeps_research(
    test_settings, stub_provider, monkeypatch,
) -> None:
    app = create_app(test_settings)
    with TestClient(app) as client:
        profile_id = configure_provider(client, stub_provider)
        services = app.state.services
        conversations = [
            client.post("/api/agent/conversations", json={
                "title": title,
                "model_reference": {
                    "provider_profile_id": profile_id, "model": "stub-model",
                },
            }).json()["id"]
            for title in ("Delete", "Keep")
        ]
        run_ids = []
        for conversation_id in (conversations[0], conversations[0], conversations[1]):
            response = client.post(
                f"/api/agent/conversations/{conversation_id}/messages",
                json={"content": "Hello.", "web_enabled": False},
            )
            assert response.status_code == 202, response.text
            run_id = response.json()["run"]["id"]
            assert wait_for_run(client, run_id)["status"] == "completed"
            run_ids.append(run_id)
            services.storage.write_json(
                test_settings.artifacts_dir,
                f"runs/{run_id}/tool-results/legacy.json",
                {"content": "An older run-scoped tool result."},
            )
            services.runs._tool_runtime.store_context_checkpoint(
                {"checkpoint_id": "checkpoint"},
                ScholarWeaveContext(run_id, services.runs._tool_runtime),
            )
            assert (test_settings.data_dir / "run_logs" / f"{run_id}.json").exists()
        services.runs.delete(run_ids[0])
        assert (test_settings.data_dir / "run_logs" / f"{run_ids[0]}.json").exists()
        test_settings.max_artifact_bytes = 4096
        for conversation_id in conversations:
            services.runs._tool_runtime.store_context_history(
                [{"role": "assistant", "content": "Exact cached history. " * 400}],
                ScholarWeaveContext(
                    run_ids[0], services.runs._tool_runtime,
                    conversation_id=conversation_id,
                ),
            )
        document = services.documents.create_document_from_bytes(
            b"%PDF-1.4\n%%EOF", filename="saved.pdf", title="Saved paper",
        )
        services.workspace.write_file("notes/saved.md", "Keep this research.")

        def fail_cleanup(*args, **kwargs):
            raise PermissionError("Cache is locked.")

        with monkeypatch.context() as patch:
            patch.setattr(services.storage, "delete_stored_tree", fail_cleanup)
            with pytest.raises(PermissionError, match="Cache is locked"):
                client.delete(f"/api/conversations/{conversations[0]}")
        assert client.get(f"/api/agent/conversations/{conversations[0]}").status_code == 200
        assert client.get(f"/api/runs/{run_ids[1]}").status_code == 200

        response = client.delete(f"/api/conversations/{conversations[0]}")

        assert response.status_code == 204, response.text
        assert client.get(f"/api/agent/conversations/{conversations[0]}").status_code == 404
        for run_id in run_ids[:2]:
            assert client.get(f"/api/runs/{run_id}").status_code == 404
            assert not (test_settings.artifacts_dir / "runs" / run_id).exists()
            assert not (test_settings.data_dir / "run_logs" / f"{run_id}.json").exists()
        assert not (test_settings.artifacts_dir / "conversations" / conversations[0]).exists()
        assert (test_settings.artifacts_dir / "conversations" / conversations[1]).is_dir()
        assert (test_settings.artifacts_dir / "runs" / run_ids[2]).is_dir()
        assert (test_settings.data_dir / "run_logs" / f"{run_ids[2]}.json").exists()
        assert client.get(f"/api/runs/{run_ids[2]}").status_code == 200
        assert services.documents.get_document(document.id) is not None
        assert (test_settings.workspace_dir / "notes" / "saved.md").read_text() == "Keep this research."


def test_deleting_active_chat_waits_for_cancellation_before_removing_files(
    test_settings, stub_provider,
) -> None:
    app = create_app(test_settings)
    with TestClient(app) as client:
        profile_id = configure_provider(client, stub_provider)
        stub_provider.reply = "Keep streaming. " * 100
        stub_provider.stream_delay_seconds = 0.05
        conversation_id = client.post("/api/agent/conversations", json={
            "title": "Delete active chat",
            "model_reference": {
                "provider_profile_id": profile_id, "model": "stub-model",
            },
        }).json()["id"]
        response = client.post(
            f"/api/agent/conversations/{conversation_id}/messages",
            json={"content": "Hello.", "web_enabled": False},
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run"]["id"]
        assert stub_provider.request_started.wait(timeout=5)
        assert client.get(f"/api/runs/{run_id}").json()["status"] == "running"
        services = app.state.services
        services.runs._tool_runtime.store_context_history(
            [{"role": "assistant", "content": "Temporary history."}],
            ScholarWeaveContext(
                run_id, services.runs._tool_runtime, conversation_id=conversation_id,
            ),
        )

        response = client.delete(f"/api/conversations/{conversation_id}")

        assert response.status_code == 204, response.text
        assert client.get(f"/api/runs/{run_id}").status_code == 404
        assert run_id not in services.runs._active_runs
        task = services.runs._tasks.get(run_id)
        assert task is None or task.done()
        assert not (test_settings.artifacts_dir / "conversations" / conversation_id).exists()
        assert not (test_settings.artifacts_dir / "runs" / run_id).exists()
        assert not (test_settings.data_dir / "run_logs" / f"{run_id}.json").exists()


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
            "max_turns": None,
            "max_tool_concurrency": 4,
            "max_input_characters": None,
            "max_output_characters": None,
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


def test_legacy_deep_work_endpoint_is_listed_in_unified_chat_and_has_bounded_worker(
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
        assert [item["id"] for item in client.get("/api/agent/conversations").json()] == [
            conversation["id"]
        ]
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
        assert blueprint["agent_tools"][0]["max_turns"] is None
        assert blueprint["run"]["max_turns"] is None
        assert blueprint["run"]["max_tool_concurrency"] == 4


def test_main_chat_message_can_enable_deep_work(
    test_settings,
    stub_provider,
) -> None:
    stub_provider.tool_plans = [
        (
            "Research this deeply.",
            "create_work_plan",
            {"items": [{"id": "answer", "title": "Answer the request"}]},
        ),
        (
            "Autonomous work is not finished",
            "update_work_item",
            {
                "id": "answer",
                "status": "completed",
                "summary": "Prepared the answer.",
            },
        ),
    ]
    app = create_app(test_settings)
    with TestClient(app) as client:
        profile_id = configure_provider(client, stub_provider)
        conversation = client.post(
            "/api/agent/conversations",
            json={
                "model_reference": {
                    "provider_profile_id": profile_id,
                    "model": "stub-model",
                },
            },
        ).json()

        response = client.post(
            f"/api/agent/conversations/{conversation['id']}/messages",
            json={"content": "Research this deeply.", "deep_work": True},
        )

        assert response.status_code == 202, response.text
        assert response.json()["conversation"]["kind"] == "deep_work"
        run = wait_for_run(client, response.json()["run"]["id"])
        assert run["status"] == "completed", run
        stored_run = app.state.services.runs.get(run["id"])
        assert stored_run.blueprint_json["name"] == "ScholarWeave deep work"
        assert stored_run.runtime_metadata_json["autonomous_work"] is True
        assert client.get(
            f"/api/agent/conversations/{conversation['id']}"
        ).json()["kind"] == "deep_work"
        assert app.state.services.conversation_turns.compile_conversation(
            conversation["id"]
        ).blueprint.name == "ScholarWeave deep work"
        incompatible = client.post(
            f"/api/agent/conversations/{conversation['id']}/messages",
            json={
                "content": "Try fast mode.",
                "fast_answer": True,
            },
        )
        assert incompatible.status_code == 400
        assert "Fast Answer is unavailable" in incompatible.text


def test_main_chat_rejects_fast_answer_with_deep_work(test_settings) -> None:
    app = create_app(test_settings)
    with TestClient(app) as client:
        conversation = client.post("/api/agent/conversations", json={}).json()
        response = client.post(
            f"/api/agent/conversations/{conversation['id']}/messages",
            json={
                "content": "Use both incompatible modes.",
                "deep_work": True,
                "fast_answer": True,
            },
        )

    assert response.status_code == 422
    assert "Fast Answer and Deep Work cannot be enabled together" in response.text
