from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.core.errors import ValidationError
from backend.persistence.files import SafeStorage
from backend.workspace.service import WorkspaceService


@pytest.mark.parametrize("content", [{"value": [1, True, None]}, ["a", 2], "plain string", 3, False, None])
def test_workspace_editor_json_round_trips_typed_values(test_settings, content):
    app = create_app(test_settings)
    workspace = app.state.services.workspace
    workspace.write_file("data/receipt.json", {"original": True})
    with TestClient(app) as client:
        original = client.get("/api/workspace/files/content", params={"path": "data/receipt.json"}).json()
        saved = client.put("/api/workspace/files/content", json={
            "path": original["path"], "content": content, "expected_sha256": original["sha256"],
        })
        assert saved.status_code == 200, saved.text
        assert saved.json()["content"] == content
        reread = client.get("/api/workspace/files/content", params={"path": original["path"]})
        assert reread.json()["content"] == content


@pytest.mark.parametrize("path", [
    "library/papers/paper--p1/summaries/run.md",
    "library/papers/paper--p1/summaries/run.json",
    "library/papers/paper--p1/summary.provenance.json",
    "library/papers/paper--p1/evidence/source/index.json",
    "research/track/review/receipt.json",
    "inbox/attachments/upload/source.md",
    ".scholarweave/config.json",
])
def test_generic_editor_cannot_modify_or_delete_generated_artifacts(test_settings, path):
    workspace = WorkspaceService(SafeStorage(test_settings))
    workspace.write_file(path, {"keep": True} if path.endswith(".json") else "Keep this artifact.")
    original = workspace.read_file(path)
    with pytest.raises(ValidationError, match="read-only"):
        workspace.write_user_file(path, "corrupt", expected_sha256=original.sha256)
    with pytest.raises(ValidationError, match="read-only"):
        workspace.delete_user_file(path)
    assert workspace.read_file(path).content == original.content


def test_generic_editor_protects_tracks_but_keeps_canonical_paper_notes_editable(test_settings):
    storage = SafeStorage(test_settings)
    path = "projects/tracks/my-track--t1/problem.md"
    storage.write_workspace_file(path, "Managed problem")
    workspace = WorkspaceService(storage)
    with pytest.raises(ValidationError, match="Managed track"):
        workspace.write_user_file(path, "corrupt")
    paper = workspace.ensure_paper_folder("p1", "Paper")
    for path in (paper["notes_path"], paper["summary_path"]):
        original = workspace.read_file(path)
        saved = workspace.write_user_file(path, "Human edits", expected_sha256=original.sha256)
        assert saved.content == "Human edits"


def test_workspace_folder_delete_keeps_review_receipts_recoverable(test_settings):
    from backend.research.reviews.schemas import ReviewCreateRequest

    app = create_app(test_settings)
    review_id = "protected-review"
    root = f"research/unlinked/{review_id}"
    reference = {"provider_profile_id": "test-profile", "model": "test-model"}
    specification = ReviewCreateRequest(
        title="Protected review", objective="Preserve research receipts.", main_model=reference,
        summary_model={"model_reference": reference},
        embedding_model={"model_reference": reference},
    )
    receipt = {"key": "topics", "topics": []}
    with TestClient(app) as client:
        services = app.state.services
        services.reviews.repository.create(review_id, specification.model_dump(mode="json"), {
            "workspace_path": root, "status": "paused", "stage": "screening",
        })
        services.reviews.repository.save_job(review_id, "topics", "screening", receipt)
        receipts = services.reviews.repository.workspace_receipts(review_id)
        services.workspace.refresh_index()
        paths = {entry.path for entry in services.workspace.list_files()}
        for path in ("research", "research/unlinked", root, f"{root}/receipts", root.upper()):
            response = client.delete("/api/workspace/files/folder", params={"path": path})
            assert response.status_code == 400, response.text
            assert services.reviews.repository.job(review_id, "topics") == receipt
            assert {entry.path for entry in services.workspace.list_files()} == paths
            for receipt_path, expected in receipts.items():
                assert services.storage.file_hash(test_settings.workspace_dir, receipt_path) == expected

        services.workspace.write_file("knowledge/disposable/note.md", "Standalone note")
        assert client.delete("/api/workspace/files/folder", params={"path": "knowledge/disposable"}).status_code == 204
        assert not (test_settings.workspace_dir / "knowledge" / "disposable").exists()

    restarted = create_app(test_settings)
    with TestClient(restarted) as client:
        assert client.get(f"/api/literature-reviews/{review_id}").status_code == 200
        assert restarted.state.services.reviews.repository.job(review_id, "topics") == receipt


@pytest.mark.parametrize("path", [".scholarweave", "inbox/attachments", "inbox/attachments/upload"])
def test_workspace_folder_delete_cannot_remove_protected_artifacts(test_settings, path):
    workspace = WorkspaceService(SafeStorage(test_settings))
    saved = workspace.write_file(f"{path}/receipt.json", {"preserve": True})
    with pytest.raises(ValidationError, match="Managed research"):
        workspace.delete_folder(path)
    assert workspace.read_file(saved.path).sha256 == saved.sha256
