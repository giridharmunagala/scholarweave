from __future__ import annotations

from fastapi.testclient import TestClient

from backend.app import create_app


def test_batch_route_preserves_explicit_model_mode_and_paper_order(test_settings, monkeypatch):
    app = create_app(test_settings)
    services = app.state.services
    calls = []

    def start_batch(document_ids, *, model_reference, reasoning_effort, mode):
        calls.append((document_ids, model_reference.model, mode))
        return [
            (
                services.runs._repository.create(
                    conversation_id=None,
                    agent_name="Paper summary",
                    input_value=document_id,
                    blueprint={},
                ),
                "test-revision",
            )
            for document_id in document_ids
        ]

    monkeypatch.setattr(services.summaries, "start_batch", start_batch)
    with TestClient(app) as client:
        response = client.post("/api/documents/summary-batches", json={
            "document_ids": ["paper-2", "paper-1"],
            "model_reference": {"provider_profile_id": "local", "model": "small"},
            "mode": "overview",
        })
    assert response.status_code == 202, response.text
    assert calls == [(["paper-2", "paper-1"], "small", "overview")]
    assert [item["document_id"] for item in response.json()["runs"]] == ["paper-2", "paper-1"]
    assert all(item["run"]["status"] == "pending" for item in response.json()["runs"])


def test_batch_route_rejects_empty_or_oversized_batches(test_settings):
    with TestClient(create_app(test_settings)) as client:
        for ids in ([], ["paper", "paper"], [" "], [f"paper-{index}" for index in range(51)]):
            response = client.post("/api/documents/summary-batches", json={
                "document_ids": ids,
                "model_reference": {"provider_profile_id": "local", "model": "small"},
            })
            assert response.status_code == 422
        assert client.post("/api/documents/summary-batches", json={
            "document_ids": ["paper"],
        }).status_code == 422


def test_single_summary_route_passes_depth_and_document_identity(test_settings, monkeypatch):
    app = create_app(test_settings)
    services = app.state.services
    calls = []

    def start(document_id, *, model_reference, reasoning_effort, mode):
        calls.append(mode)
        return services.runs._repository.create(
            conversation_id=None, agent_name="Summary", input_value=document_id, blueprint={},
        ), "revision"

    monkeypatch.setattr(services.summaries, "start", start)
    with TestClient(app) as client:
        response = client.post("/api/documents/paper/summaries", json={"mode": "overview"})
    assert response.status_code == 202, response.text
    assert response.json()["document_id"] == "paper"
    assert calls == ["overview"]


def test_summary_routes_preserve_coverage_and_provenance(test_settings, monkeypatch):
    app = create_app(test_settings)
    summaries = app.state.services.summaries
    version = {
        "id": "version",
        "document_id": "paper",
        "run_id": "run",
        "path": "papers/paper/summaries/version.md",
        "created_at": "2026-01-01T00:00:00Z",
        "prompt_revision": "prompt",
        "review_summary": "Quick overview only.",
        "citation_count": 1,
        "status": "overview",
        "mode": "overview",
        "review_complete": False,
        "coverage_complete": False,
        "coverage": {
            "kind": "pages",
            "checkpointed_batches": 1,
            "exact_spans_path": "papers/paper/evidence/source/index.json",
        },
        "next_start": 2,
        "next_offset": 0,
        "evidence_path": "papers/paper/evidence/source/index.json",
        "model": {"model": "local-model", "provider_kind": "openai_compatible", "provider_profile_id": "local"},
        "content_hash": "content",
        "source_hash": "source",
        "extraction_hash": "extraction",
        "source_version": "revision",
        "canonical_path": "papers/paper/summary.md",
        "canonical_updated": False,
    }
    monkeypatch.setattr(summaries, "versions", lambda document_id: [version])
    monkeypatch.setattr(summaries, "version", lambda document_id, version_id: (version, "Overview"))
    monkeypatch.setattr(summaries, "promote", lambda document_id, version_id: (
        version, "papers/paper/summary.md", "Overview",
    ))
    with TestClient(app) as client:
        listed = client.get("/api/documents/paper/summaries")
        opened = client.get("/api/documents/paper/summaries/version")
        promoted = client.post("/api/documents/paper/summaries/version/promote")
    for response in (listed, opened, promoted):
        assert response.status_code == 200, response.text
    for payload in (listed.json()[0], opened.json()["version"], promoted.json()["version"]):
        assert payload["source_version"] == "revision"
        assert payload["model"]["model"] == "local-model"
        assert payload["review_complete"] is False
        assert payload["canonical_updated"] is False
        assert payload["coverage"]["exact_spans_path"].endswith("/index.json")
