from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app


def seed_summary(services):
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF", filename="paper.pdf", title="Paper",
    )
    paper = services.workspace.ensure_paper_folder(document.id, document.title)
    path = f"{paper['folder']}/summaries/version.md"
    services.workspace.write_file(path, "# Saved version\n\nFindings from source.")
    services.workspace.write_file(path.removesuffix(".md") + ".json", {
        "id": "version", "run_id": "run", "document_id": document.id, "path": path,
        "created_at": "2026-10-01T00:00:00Z", "prompt_revision": "revision",
        "review_summary": "Reviewed.", "citation_count": 1, "status": "reviewed",
    })
    services.workspace.write_file(paper["summary_path"], "# Human edits\n\nKeep every word.\r\n")
    services.workspace.write_file(paper["notes_path"], "Keep my notes.")
    return document.id, paper


def test_promotion_requires_consent_and_retains_the_prior_canonical_as_a_version(test_settings):
    app = create_app(test_settings)
    with TestClient(app) as client:
        document_id, paper = seed_summary(app.state.services)
        workspace = app.state.services.workspace
        original = workspace.read_file(paper["summary_path"])
        url = f"/api/documents/{document_id}/summaries/version/promote"
        assert client.post(url).status_code == 422
        assert client.post(url, json={
            "expected_sha256": original.sha256, "replace_existing": False,
        }).status_code == 422
        response = client.post(url, json={
            "expected_sha256": original.sha256, "replace_existing": True,
        })
        assert response.status_code == 200, response.text
        versions = client.get(f"/api/documents/{document_id}/summaries").json()
        preserved = next(item for item in versions if item["status"] == "preserved")
        opened = client.get(f"/api/documents/{document_id}/summaries/{preserved['id']}")
        assert opened.json()["content"] == original.content
        assert preserved["content_hash"] == original.sha256
        assert workspace.read_file(paper["summary_path"]).content == "# Saved version\n\nFindings from source."
        assert workspace.read_file(paper["notes_path"]).content == "Keep my notes."


@pytest.mark.parametrize("during_write", [False, True])
def test_promotion_cannot_replace_a_concurrent_human_edit(test_settings, monkeypatch, during_write):
    app = create_app(test_settings)
    with TestClient(app) as client:
        services = app.state.services
        document_id, paper = seed_summary(services)
        workspace = services.workspace
        original = workspace.read_file(paper["summary_path"])
        if during_write:
            write = workspace.write_file

            def external_edit(path, content, **kwargs):
                if path == paper["summary_path"]:
                    workspace._storage.write_workspace_file(path, "# New external edit")
                return write(path, content, **kwargs)

            monkeypatch.setattr(workspace, "write_file", external_edit)
        else:
            workspace.write_file(paper["summary_path"], "# New external edit")
        response = client.post(f"/api/documents/{document_id}/summaries/version/promote", json={
            "expected_sha256": original.sha256, "replace_existing": True,
        })
        assert response.status_code == 409, response.text
        assert workspace.read_file(paper["summary_path"]).content == "# New external edit"
        assert workspace.read_file(paper["notes_path"]).content == "Keep my notes."


def test_active_summary_jobs_are_durable_and_duplicate_starts_are_rejected(test_settings):
    app = create_app(test_settings)
    with TestClient(app) as client:
        services = app.state.services
        repository = services.runs._repository
        active = repository.create(
            conversation_id=None, agent_name="ScholarWeave paper summary", input_value="Request",
            blueprint={}, runtime_metadata={"paper_summary_document_id": "p1", "prompt_revision": "saved-revision"},
        )
        repository.create(
            conversation_id=None, agent_name="Ordinary chat", input_value="Request", blueprint={},
        )
        finished = repository.create(
            conversation_id=None, agent_name="ScholarWeave paper overview", input_value="Request",
            blueprint={}, runtime_metadata={"paper_summary_document_id": "p2", "prompt_revision": "older"},
        )
        repository.fail(finished.id, "Stopped previously")
        first = client.get("/api/documents/summary-runs").json()
        reopened = client.get("/api/documents/summary-runs", params={"document_id": "p1"}).json()
        assert [item["run"]["id"] for item in first] == [active.id]
        assert reopened == first
        assert reopened[0]["prompt_revision"] == "saved-revision"
        assert reopened[0]["document_id"] == "p1"
        assert client.get("/api/documents/summary-runs", params={"document_id": "missing"}).json() == []
        assert client.post("/api/documents/p1/summaries", json={}).status_code == 409
        assert client.post("/api/documents/summary-batches", json={
            "document_ids": ["p1"], "model_reference": {"provider_profile_id": "profile", "model": "model"},
        }).status_code == 409
        assert len(repository.list()) == 3


def test_promotion_recovers_a_partially_written_preserved_version(test_settings, monkeypatch):
    app = create_app(test_settings)
    with TestClient(app):
        services = app.state.services
        document_id, paper = seed_summary(services)
        original = services.workspace.read_file(paper["summary_path"])
        write = services.workspace.write_file

        def fail_receipt(path, content, **kwargs):
            if "/summaries/preserved-" in path and path.endswith(".json"):
                raise OSError("Could not save preserved receipt")
            return write(path, content, **kwargs)

        monkeypatch.setattr(services.workspace, "write_file", fail_receipt)
        with pytest.raises(OSError, match="preserved receipt"):
            services.summaries.promote(
                document_id, "version", expected_sha256=original.sha256, replace_existing=True,
            )
        assert services.workspace.read_file(paper["summary_path"]).content == original.content
        monkeypatch.setattr(services.workspace, "write_file", write)
        services.summaries.promote(
            document_id, "version", expected_sha256=original.sha256, replace_existing=True,
        )
        preserved = next(item for item in services.summaries.versions(document_id) if item["status"] == "preserved")
        assert services.summaries.version(document_id, preserved["id"])[1] == original.content
