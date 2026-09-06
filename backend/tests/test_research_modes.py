from __future__ import annotations

import time
from unittest.mock import Mock

import anyio
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError as SchemaValidationError

from backend.agents.context import ScholarWeaveContext
from backend.agents.context_budget import _request_tokens
from backend.app import create_app
from backend.conversations.schemas import ConversationMessageRequest
from backend.conversations.turns import (
    deep_work_blueprint,
    research_blueprint,
    validate_paper_work_completion,
)
from backend.core.errors import ValidationError
from backend.tests.test_api import configure_provider, wait_for_run


def paper_context(mode: str | None, *, citations: list[str] | None = None):
    return ScholarWeaveContext(
        run_id="mode-test",
        tool_runtime=Mock(),
        metadata={
            "research_mode": mode,
            "completion_output": "The method preserves sparsity (p.2).",
            "paper_activity": [{
                "document_id": "paper-1",
                "title": "Sparse Methods",
                "action": "read",
                "citations": ["p.2"] if citations is None else citations,
            }],
        },
    )


@pytest.mark.parametrize("mode", ["learn", "understand"])
def test_narrow_paper_modes_require_cited_read_but_no_artifact_writes(mode):
    context = paper_context(mode)
    validate_paper_work_completion(context)
    assert context.metadata["paper_activity"][0]["action"] == "read"


@pytest.mark.parametrize("mode", [None, "review"])
def test_review_and_compatible_default_keep_full_paper_gate(mode):
    context = paper_context(mode)
    with pytest.raises(ValidationError) as error:
        validate_paper_work_completion(context)
    assert "notes.md" in str(error.value.issues)
    assert "summary.md" in str(error.value.issues)


@pytest.mark.parametrize(
    "output",
    ["Uncited assertion.", "The answer is on p.20.", "A fabricated citation (p.3).", None],
)
def test_narrow_completion_rejects_missing_or_unread_citations(output):
    context = paper_context("learn")
    context.metadata["completion_output"] = output
    with pytest.raises(ValidationError, match="cited answer"):
        validate_paper_work_completion(context)


def test_narrow_completion_rejects_metadata_only_and_empty_evidence():
    context = paper_context("understand", citations=[])
    with pytest.raises(ValidationError) as error:
        validate_paper_work_completion(context)
    assert "obtain page or chunk citations" in str(error.value.issues)
    context.metadata["paper_activity"][0]["action"] = "acquired"
    with pytest.raises(ValidationError) as error:
        validate_paper_work_completion(context)
    assert "read relevant paper passages" in str(error.value.issues)


def test_mode_switch_and_autonomous_work_cannot_silently_drop_review_gate():
    context = paper_context("learn")
    validate_paper_work_completion(context)
    context.metadata["research_mode"] = "review"
    context.metadata["paper_require_summary"] = False
    context.metadata["paper_require_notes"] = False
    with pytest.raises(ValidationError, match="Paper work is incomplete"):
        validate_paper_work_completion(context)
    context.metadata["research_mode"] = "learn"
    context.metadata["autonomous_work"] = True
    with pytest.raises(ValidationError, match="Paper work is incomplete"):
        validate_paper_work_completion(context)


def test_narrow_gate_keeps_unread_second_paper_visible():
    context = paper_context("learn")
    context.metadata["paper_activity"].append(
        {"document_id": "paper-2", "title": "Contradictory Paper", "action": "acquired"}
    )
    with pytest.raises(ValidationError) as error:
        validate_paper_work_completion(context)
    assert "Contradictory Paper" in str(error.value.issues)


def test_review_gate_rejects_partial_summary_until_full_coverage_saved():
    context = paper_context("review")
    activity = context.metadata["paper_activity"]
    activity.extend([
        {"document_id": "paper-1", "action": "notes_saved"},
        {"document_id": "paper-1", "action": "summary_saved", "coverage_complete": False},
    ])
    with pytest.raises(ValidationError) as error:
        validate_paper_work_completion(context)
    assert "summary.md" in str(error.value.issues)
    activity.append(
        {"document_id": "paper-1", "action": "summary_saved", "coverage_complete": True}
    )
    validate_paper_work_completion(context)


def test_overview_is_not_a_review_even_when_its_short_source_is_fully_covered():
    context = paper_context("review")
    activity = context.metadata["paper_activity"]
    activity.extend([
        {"document_id": "paper-1", "action": "notes_saved"},
        {"document_id": "paper-1", "action": "summary_saved",
         "coverage_complete": True, "review_complete": False},
    ])
    with pytest.raises(ValidationError) as error:
        validate_paper_work_completion(context)
    assert "summary.md" in str(error.value.issues)
    activity.append(
        {"document_id": "paper-1", "action": "summary_saved",
         "coverage_complete": True, "review_complete": True}
    )
    validate_paper_work_completion(context)


@pytest.mark.parametrize("mode", ["learn", "understand", "review"])
def test_explicit_modes_offer_local_paper_tools_without_web(mode):
    blueprint = research_blueprint({}, research_mode=mode, web_enabled=False)
    tools = set(blueprint.agents[0].tool_ids)
    assert {"read-paper", "search-library", "read-note", "save-note"} <= tools
    assert not {"search-sources", "acquire-source"} & tools
    assert f"Selected research mode: {mode}." in blueprint.agents[0].instructions


@pytest.mark.parametrize("mode", ["learn", "understand", "review", "deep_work", "fast_answer"])
def test_product_blueprints_have_no_turn_character_or_delegation_ceiling(mode):
    blueprint = (
        deep_work_blueprint({})
        if mode == "deep_work"
        else research_blueprint(
            {},
            fast_answer=mode == "fast_answer",
            research_mode=None if mode == "fast_answer" else mode,
        )
    )
    assert blueprint.run.max_turns is None
    assert blueprint.run.max_input_characters is None
    assert blueprint.run.max_output_characters is None
    assert all(delegate.max_turns is None for delegate in blueprint.agent_tools)
    assert blueprint.session.history_max_items is None
    assert blueprint.session.messages_only is False


@pytest.mark.parametrize("deep_work", [False, True])
def test_product_runs_continue_across_epochs_without_repeating_writes(
    test_settings, stub_provider, deep_work,
):
    test_settings.agent_epoch_max_turns = 2
    test_settings.agent_max_epochs = 1
    test_settings.agent_run_timeout_seconds = 0.01
    goal = "Record every durable checkpoint."
    expected = [f"Evidence marker {index:02d}." for index in range(20)]
    stub_provider.tool_plans = [
        (
            goal,
            "save_research_note",
            {
                "target": "path", "mode": "append", "path": "notes/checkpoints.md",
                "document_id": None, "name": None, "content": content, "tags": [],
            },
        )
        for content in expected
    ]
    if deep_work:
        stub_provider.tool_plans.insert(
            0,
            (goal, "create_work_plan", {"items": [{"id": "record", "title": goal}]}),
        )
        stub_provider.tool_plans.append(
            (
                goal, "update_work_item",
                {"id": "record", "status": "completed", "summary": "All evidence recorded."},
            )
        )
    with TestClient(create_app(test_settings)) as client:
        profile_id = configure_provider(client, stub_provider)
        conversation = client.post(
            "/api/agent/conversations",
            json={"model_reference": {"provider_profile_id": profile_id, "model": "stub-model"}},
        ).json()
        services = client.app.state.services
        services.workspace.write_file("notes/checkpoints.md", "")
        response = client.post(
            f"/api/agent/conversations/{conversation['id']}/messages",
            json={
                "content": goal, "deep_work": deep_work, "web_enabled": False,
                "context_window_tokens": 131_072,
            },
        )
        assert response.status_code == 202, response.text
        run = wait_for_run(client, response.json()["run"]["id"])
        assert run["status"] == "completed", run["error"]
        content = services.workspace.read_file("notes/checkpoints.md").content
        assert isinstance(content, str)
        assert [line for line in content.splitlines() if line.strip()] == expected
        record = services.runs.get(run["id"])
        assert len(record.epochs) > 10
        assert len(stub_provider.requests) == len(stub_provider.tool_plans) + 1
        if deep_work:
            updates = [
                attempt for attempt in record.tool_attempts
                if attempt.catalog_id == "work.plan.update"
            ]
            assert len(updates) == 1
            assert updates[0].result_json["complete"] is True
            assert updates[0].result_json["items"][0]["status"] == "completed"


@pytest.mark.parametrize(
    "options",
    [
        {"deep_work": True, "research_mode": "learn"},
        {"deep_work": True, "research_mode": "understand"},
        {"fast_answer": True, "research_mode": "review"},
        {"research_mode": "unknown"},
    ],
)
def test_incompatible_mode_requests_are_rejected(options):
    with pytest.raises(SchemaValidationError):
        ConversationMessageRequest(content="Question", **options)


def test_message_input_has_no_character_ceiling_but_requires_content():
    content = "Research evidence " * 10_000
    assert ConversationMessageRequest(content=content).content == content
    schema = ConversationMessageRequest.model_json_schema()["properties"]["content"]
    assert "maxLength" not in schema
    assert schema["minLength"] == 1
    with pytest.raises(SchemaValidationError):
        ConversationMessageRequest(content="")


def test_large_message_reaches_model_when_configured_context_has_room(test_settings, stub_provider):
    content = "x" * 100_001
    with TestClient(create_app(test_settings)) as client:
        profile_id = configure_provider(client, stub_provider)
        conversation = client.post(
            "/api/agent/conversations",
            json={"model_reference": {"provider_profile_id": profile_id, "model": "stub-model"}},
        ).json()
        response = client.post(
            f"/api/agent/conversations/{conversation['id']}/messages",
            json={
                "content": content, "web_enabled": False, "context_window_tokens": 131_072,
            },
        )
        assert response.status_code == 202, response.text
        run = wait_for_run(client, response.json()["run"]["id"])
        assert run["status"] == "completed", run["error"]
        record = client.app.state.services.runs.get(run["id"])
        assert record.input_json == content
        assert any(
            message.get("role") == "user" and message.get("content") == content
            for request in stub_provider.requests
            for message in request["messages"]
        )


def test_modes_switch_per_turn_and_are_durable_in_run_metadata(test_settings, stub_provider):
    with TestClient(create_app(test_settings)) as client:
        profile_id = configure_provider(client, stub_provider)
        conversation = client.post(
            "/api/agent/conversations",
            json={"model_reference": {
                "provider_profile_id": profile_id, "model": "stub-model",
            }},
        ).json()
        for mode in ("learn", "understand", "review"):
            response = client.post(
                f"/api/agent/conversations/{conversation['id']}/messages",
                json={"content": f"Explain this in {mode} mode", "research_mode": mode,
                      "web_enabled": False},
            )
            assert response.status_code == 202, response.text
            run = wait_for_run(client, response.json()["run"]["id"])
            assert run["status"] == "completed", run["error"]
            record = client.app.state.services.runs.get(run["id"])
            assert record.runtime_metadata_json["research_mode"] == mode
            assert record.runtime_metadata_json["paper_require_summary"] is (mode == "review")
            assert record.runtime_metadata_json["paper_require_notes"] is (mode == "review")


@pytest.mark.parametrize("cited", [True, False])
def test_quick_paper_answer_uses_actual_read_evidence_without_full_review(
    test_settings, stub_provider, cited,
):
    with TestClient(create_app(test_settings)) as client:
        profile_id = configure_provider(client, stub_provider)
        services = client.app.state.services
        document = services.documents.create_document_from_bytes(
            b"%PDF-1.4\n%%EOF", filename="sparse.pdf", title="Sparse Methods",
        )
        services.retrieval.replace_document_chunks(document.id, [{
            "section_title": "Method", "page_start": 2, "page_end": 2,
            "citation": "p.2", "text": "The method preserves sparsity by thresholding.",
        }])
        services.document_repository.mark_ready(document.id, page_count=2, metadata={})
        stub_provider.tool_plans = [(
            "Explain sparse method", "read_research_paper",
            {"document_id": document.id, "action": "chunks", "start": 0,
             "limit": 1, "query": None, "offset": None},
        )]
        stub_provider.reply = (
            "The sparse method uses thresholding (p.2)." if cited else "An unsupported assertion."
        )
        conversation = client.post(
            "/api/agent/conversations",
            json={"model_reference": {
                "provider_profile_id": profile_id, "model": "stub-model",
            }},
        ).json()
        response = client.post(
            f"/api/agent/conversations/{conversation['id']}/messages",
            json={"content": "Explain sparse method", "research_mode": "learn",
                  "web_enabled": False},
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run"]["id"]
        if not cited:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if any(
                    event.payload_json.get("terminal_reason") == "completion_rejected"
                    for event in services.runs.get(run_id).events
                ):
                    break
                time.sleep(0.01)
            else:
                pytest.fail("Uncited completion was not rejected.")
            assert client.post(f"/api/runs/{run_id}/cancel").status_code == 200
        run = wait_for_run(client, run_id)
        assert run["status"] == ("completed" if cited else "cancelled"), run["error"]
        record = services.runs.get(run["id"])
        activity = record.runtime_metadata_json["paper_activity"]
        assert any(
            item["action"] == "read" and "p.2" in item.get("citations", [])
            for item in activity
        )
        assert not any(item["action"] in {"notes_saved", "summary_saved"} for item in activity)


@pytest.mark.parametrize("path", ["agent", "deep-work"])
def test_deep_work_conversation_cannot_downgrade_on_either_endpoint(
    test_settings, path,
):
    with TestClient(create_app(test_settings)) as client:
        conversation = client.post("/api/deep-work/conversations", json={}).json()
        response = client.post(
            f"/api/{path}/conversations/{conversation['id']}/messages",
            json={"content": "Question", "research_mode": "learn"},
        )
        assert response.status_code == 400, response.text


def test_invalid_upgrade_does_not_promote_conversation(test_settings):
    with TestClient(create_app(test_settings)) as client:
        conversation = client.post("/api/agent/conversations", json={}).json()
        with pytest.raises(ValidationError):
            client.app.state.services.conversation_turns.start_message(
                conversation["id"], "Question", deep_work=True, research_mode="learn",
            )
        assert client.get(
            f"/api/agent/conversations/{conversation['id']}"
        ).json()["kind"] == "autonomous"


@pytest.mark.parametrize("mode", ["learn", "understand", "review", "deep_work"])
def test_full_workflow_tool_surface_fits_working_context_budget(
    test_settings, stub_provider, mode,
):
    with TestClient(create_app(test_settings)) as client:
        profile_id = configure_provider(client, stub_provider)
        conversation = client.post(
            "/api/agent/conversations",
            json={"model_reference": {
                "provider_profile_id": profile_id, "model": "stub-model",
            }},
        ).json()
        services = client.app.state.services
        compiled = services.conversation_turns.compile_conversation(
            conversation["id"],
            research_mode="review" if mode == "deep_work" else mode,
            deep_work=mode == "deep_work",
            first_turn=True,
        )
        assert compiled.context_policy is not None
        context = ScholarWeaveContext(
            run_id="budget-check", tool_runtime=services.runs._tool_runtime,
        )
        items = [{"role": "user", "content": "Explain the paper's assumptions with sources."}]

        async def check_budget():
            for definition in compiled.agents_by_id.values():
                prepared = await compiled.context_policy.prepare(
                    definition, items, definition.instructions, context, turn_index=1,
                )
                estimated = _request_tokens(
                    definition, prepared.items, prepared.instructions, context,
                )
                assert estimated <= 12_000, (definition.name, estimated)
                print(f"{mode}/{definition.id}: {estimated} estimated input tokens")

        anyio.run(check_budget)
        assert stub_provider.requests == []
