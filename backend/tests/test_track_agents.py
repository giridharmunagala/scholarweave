from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.agents.blueprint import FunctionToolSpec
from backend.agents.context import ScholarWeaveContext
from backend.app import create_app
from backend.bootstrap import create_services
from backend.conversations.turns import deep_work_blueprint, research_blueprint
from backend.tests.test_api import configure_provider, wait_for_run
from backend.tools.catalog import create_tool_catalog
from backend.tools.policy import operation_policy


def test_track_tools_available_offline_at_every_effort():
    for blueprint in (
        research_blueprint({}, web_enabled=False, response_effort="auto"),
        research_blueprint({}, web_enabled=False, response_effort="quick"),
        deep_work_blueprint({}, web_enabled=False),
    ):
        assert {
            "research.tracks.read", "research.tracks.write", "research.tracks.organize",
        } <= {tool.catalog_id for tool in blueprint.tools}
        for agent in blueprint.agents:
            assert {"read-track", "write-track", "organize-track"} <= set(agent.tool_ids)
            for skill in ("problem-framing", "literature-review", "experiment-planning", "results-interpretation"):
                assert f'<skill name="{skill}">' in agent.instructions
    assert operation_policy("research.tracks.read", {}).safe_retry
    for catalog_id in ("research.tracks.write", "research.tracks.organize"):
        policy = operation_policy(catalog_id, {})
        assert policy.mutating and not policy.safe_retry


@pytest.mark.parametrize("effort", ["auto", "quick", "thorough"])
def test_llm_creates_and_reads_exploration_with_real_provider(test_settings, stub_provider, effort):
    with TestClient(create_app(test_settings)) as client:
        profile = configure_provider(client, stub_provider)
        track = client.post("/api/tracks", json={"title": "Explore retrieval"}).json()
        goal = "Save these tentative search leads to a new exploration file in this track."
        stub_provider.tool_plans = [
            (goal, "read_research_track", {
                "action": "inspect", "track_id": None, "section": None, "limit": 10, "offset": 0,
            }),
            (goal, "write_research_track", {
                "track_id": None, "section": "exploration/retrieval-leads",
                "content": "# Leads\n\nUnverified hypothesis; check local paper evidence first.",
                "expected_sha256": None,
            }),
            (goal, "read_research_track", {
                "action": "section", "track_id": None, "section": "exploration/retrieval-leads", "limit": 10, "offset": 0,
            }),
        ]
        conversation = client.post("/api/agent/conversations", json={
            "track_id": track["id"], "model_reference": {"provider_profile_id": profile, "model": "stub-model"},
        }).json()
        response = client.post(f"/api/agent/conversations/{conversation['id']}/messages", json={
            "content": goal, "response_effort": effort, "web_enabled": False,
        })
        assert response.status_code == 202, response.text
        run = wait_for_run(client, response.json()["run"]["id"])
        assert run["status"] == "completed", run["error"]
        detail = client.get(f"/api/tracks/{track['id']}").json()
        assert detail["exploration"][0]["section"] == "exploration/retrieval-leads"
        saved = client.get(f"/api/tracks/{track['id']}/exploration/retrieval-leads").json()
        assert "Unverified hypothesis" in saved["content"]


@pytest.mark.anyio
async def test_real_track_tools_enforce_revision_and_preserve_history(test_settings):
    services = create_services(test_settings)
    try:
        runtime = services.runs._tool_runtime
        context = ScholarWeaveContext(run_id="track-tools", tool_runtime=runtime)
        catalog = create_tool_catalog()

        async def invoke(catalog_id, arguments):
            tool = catalog.build_function_tool(FunctionToolSpec(id="track", catalog_id=catalog_id))
            return await tool.on_invoke_tool(
                SimpleNamespace(context=context, tool_call_id=catalog_id), json.dumps(arguments),
            )

        created = await invoke("research.tracks.organize", {
            "action": "create", "track_id": None, "title": "Retrieval", "document_id": None,
        })
        context.metadata["research_track_id"] = created["id"]
        section = await invoke("research.tracks.read", {
            "action": "section", "track_id": None, "section": "problem", "limit": 10, "offset": 0,
        })
        content = section["content"] + "\n\nKeep this researcher-defined constraint.\n"
        saved = await invoke("research.tracks.write", {
            "track_id": None, "section": "problem", "content": content,
            "expected_sha256": section["sha256"],
        })
        assert saved["content"] == content
        with pytest.raises(RuntimeError, match="section changed"):
            await invoke("research.tracks.write", {
                "track_id": None, "section": "problem", "content": "Discard everything",
                "expected_sha256": section["sha256"],
            })
        assert services.tracks.read_section(created["id"], "problem").content == content
        assert any(receipt.href == f"/tracks/{created['id']}" for receipt in context.receipts)
        with pytest.raises(RuntimeError, match="invalid_input"):
            await invoke("research.tracks.write", {
                "track_id": None, "section": "problem", "content": "Missing revision",
            })
        with pytest.raises(RuntimeError, match="invalid_input"):
            await invoke("research.tracks.organize", {
                "action": "create", "track_id": None, "title": None, "document_id": None,
            })
    finally:
        await services.close()


@pytest.mark.parametrize("effort", ["auto", "quick", "thorough"])
def test_track_chat_reads_and_saves_requested_section_with_real_provider(
    test_settings, stub_provider, effort,
):
    with TestClient(create_app(test_settings)) as client:
        profile = configure_provider(client, stub_provider)
        track = client.post("/api/tracks", json={"title": "Efficient retrieval"}).json()
        section_url = f"/api/tracks/{track['id']}/sections/problem"
        section = client.get(section_url).json()
        goal = "Update only this track's problem section with the supplied latency constraint."
        updated = section["content"] + "\n\nConstraint: report latency as well as retrieval quality.\n"
        stub_provider.tool_plans = [
            (goal, "read_research_track", {
                "action": "section", "track_id": None, "section": "problem", "limit": 10, "offset": 0,
            }),
            (goal, "write_research_track", {
                "track_id": None, "section": "problem", "content": updated,
                "expected_sha256": section["sha256"],
            }),
        ]
        stub_provider.reply = "Updated the requested problem section, preserving earlier content."
        conversation = client.post("/api/agent/conversations", json={
            "track_id": track["id"],
            "model_reference": {"provider_profile_id": profile, "model": "stub-model"},
        }).json()
        assert conversation["track_id"] == track["id"]
        response = client.post(f"/api/agent/conversations/{conversation['id']}/messages", json={
            "content": goal, "web_enabled": False, "response_effort": effort,
        })
        assert response.status_code == 202, response.text
        run = wait_for_run(client, response.json()["run"]["id"])
        assert run["status"] == "completed", run["error"]
        assert client.get(section_url).json()["content"] == updated
        record = client.app.state.services.runs.get(run["id"])
        assert record.runtime_metadata_json["research_track_id"] == track["id"]
        assert {attempt.catalog_id for attempt in record.tool_attempts} >= {
            "research.tracks.read", "research.tracks.write",
        }


def test_track_discussion_does_not_write_or_wait_for_external_experiments(test_settings, stub_provider):
    with TestClient(create_app(test_settings)) as client:
        profile = configure_provider(client, stub_provider)
        track = client.post("/api/tracks", json={"title": "Outside experiments"}).json()
        url = f"/api/tracks/{track['id']}/sections/experiments"
        original = client.get(url).json()
        running = (
            "# Experiments\n\n"
            "| ID | Experiment | Status | Success criterion | Evidence |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| EXP-001 | External baseline | Running | Report latency | Awaiting researcher |\n"
        )
        assert client.put(url, json={
            "content": running, "expected_sha256": original["sha256"],
        }).status_code == 200
        goal = "Discuss the running experiment. Do not save or wait for it."
        stub_provider.tool_plans = [(goal, "read_research_track", {
            "action": "section", "track_id": None, "section": "experiments", "limit": 10, "offset": 0,
        })]
        stub_provider.reply = "The external baseline is running; measurements have not been supplied."
        conversation = client.post("/api/agent/conversations", json={
            "track_id": track["id"],
            "model_reference": {"provider_profile_id": profile, "model": "stub-model"},
        }).json()
        response = client.post(f"/api/agent/conversations/{conversation['id']}/messages", json={
            "content": goal, "web_enabled": False, "response_effort": "quick",
        })
        run = wait_for_run(client, response.json()["run"]["id"])
        assert run["status"] == "completed", run["error"]
        assert client.get(url).json()["content"] == running
        record = client.app.state.services.runs.get(run["id"])
        assert all(attempt.catalog_id != "research.tracks.write" for attempt in record.tool_attempts)
        compiled = client.app.state.services.conversation_turns.compile_conversation(conversation["id"])
        assert track["id"] in compiled.blueprint.agents[0].instructions
        assert "never keep a run alive" in compiled.blueprint.agents[0].instructions
        assert client.delete(f"/api/conversations/{conversation['id']}").status_code == 204
        assert client.app.state.services.tracks.for_conversation(conversation["id"]) is None


def test_invalid_track_does_not_create_a_conversation(test_settings):
    with TestClient(create_app(test_settings)) as client:
        before = client.get("/api/agent/conversations").json()
        response = client.post("/api/agent/conversations", json={"track_id": "missing-track"})
        assert response.status_code == 404, response.text
        assert client.get("/api/agent/conversations").json() == before
