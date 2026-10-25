import os
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.core.errors import ConflictError, ValidationError
from backend.core.http import services
from backend.persistence.database import create_session_factory
from backend.persistence.files import SafeStorage
from backend.tracks.linked_repositories import LinkedRepositoryService
from backend.tracks.repository import TrackRepository
from backend.tracks.repository_router import router
from backend.tracks.repository_schemas import ArtifactLinkRequest, ArtifactPromotionRequest
from backend.tracks.service import TrackService
from backend.workspace.repository import WorkspaceRepository
from backend.workspace.service import WorkspaceService


@pytest.fixture
def linked(test_settings):
    sessions = create_session_factory(test_settings)
    storage = SafeStorage(test_settings)
    workspace = WorkspaceService(storage, WorkspaceRepository(sessions))
    tracks = TrackService(workspace, storage, SimpleNamespace(), TrackRepository(sessions))
    container = SimpleNamespace(storage=storage, tracks=tracks)
    service = LinkedRepositoryService(container.storage, container.tracks)
    track = container.tracks.create("Repository continuity")
    section = container.tracks.read_section(track.id, "experiments")
    container.tracks.write_section(track.id, "experiments",
        "| ID | Experiment | Status | Success criterion | Evidence |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| EXP-001 | Baseline | Planned | Reproduce | |\n", section.sha256)
    repo = test_settings.data_dir.parent / "code"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / ".git" / "HEAD").write_text("a" * 40)
    (repo / "results.csv").write_text("accuracy\n0.8\n", encoding="utf-8")
    link = service.link(track.id, str(repo.resolve()), True)
    return container, service, track.id, repo, link


def test_durable_manual_edit_artifact_refresh_and_unlink(linked):
    container, service, track, repo, link = linked
    preview = service.preview(track, link.id, "results.csv")
    assert preview["editable"]
    with pytest.raises(ConflictError):
        service.edit(track, link.id, "results.csv", "other", "0" * 64)
    saved = service.edit(track, link.id, "results.csv", "accuracy\n0.9\n", preview["sha256"])
    receipt = service.artifact(track, link.id, ArtifactLinkRequest(
        path="results.csv", experiment_id="EXP-001", expected_sha256=saved["sha256"],
    )).artifacts[0]
    assert receipt.observed_revision == "a" * 40
    assert receipt.producing_revision is None
    restarted = LinkedRepositoryService(container.storage, container.tracks)
    assert restarted.list(track)[0].artifacts[0].sha256 == saved["sha256"]
    (repo / "results.csv").write_text("accuracy\n0.7\n")
    (repo / ".git" / "HEAD").write_text("b" * 40)
    refreshed = restarted.refresh(track, link.id)
    assert refreshed.artifacts[0].status == "changed"
    assert refreshed.artifacts[0].observed_revision == "a" * 40
    assert refreshed.current_revision == "b" * 40
    restarted.unlink(track, link.id)
    assert restarted.list(track) == []
    assert (repo / "results.csv").exists()


def test_refresh_and_comparison_do_not_bypass_manual_edit_revision_guard(linked):
    _, service, track, repo, link = linked
    original = service.preview(track, link.id, "results.csv")
    (repo / "results.csv").write_bytes(b"External researcher text\n")
    service.refresh(track, link.id)
    compared = service.preview(track, link.id, original["path"])
    assert compared["content"] == "External researcher text\n"
    with pytest.raises(ConflictError):
        service.edit(track, link.id, original["path"], "My draft", original["sha256"])
    (repo / "results.csv").write_bytes(b"Another external revision\n")
    with pytest.raises(ConflictError):
        service.edit(track, link.id, compared["path"], "My draft\nExternal researcher text\n", compared["sha256"])
    current = service.preview(track, link.id, compared["path"])
    reconciled = "My draft\nExternal researcher text\nAnother external revision\n"
    saved = service.edit(track, link.id, current["path"], reconciled, current["sha256"])
    assert saved["content"] == reconciled
    assert (repo / "results.csv").read_text(encoding="utf-8") == reconciled


@pytest.mark.parametrize("path", [
    "../results.csv", "/results.csv", "C:\\results.csv", "results.csv:stream", ".git/HEAD",
    ".env", "node_modules/package.json", "credentials.json", "secrets.txt",
    "folder/../results.csv", "results.csv.", "NUL", "\\\\server\\share\\file",
], ids=[f"unsafe-{index}" for index in range(13)])
def test_unsafe_paths_rejected(linked, path):
    _, service, track, _, link = linked
    with pytest.raises(ValidationError):
        service.preview(track, link.id, path)


def test_bounded_browse_utf8_and_experiment_validation(linked):
    _, service, track, repo, link = linked
    (repo / ".env").write_text("private")
    (repo / "credentials.json").write_text("{}")
    listing = service.browse(track, link.id)
    assert [item["name"] for item in listing["entries"]] == ["results.csv"]
    (repo / "large.log").write_bytes(b"x" * (service.TEXT_LIMIT + 1))
    assert not service.preview(track, link.id, "large.log")["editable"]
    (repo / "huge.log").write_bytes(b"x" * (service.FILE_LIMIT + 1))
    with pytest.raises(ValidationError):
        service.preview(track, link.id, "huge.log")
    (repo / "bad.log").write_bytes(b"\xff")
    assert service.preview(track, link.id, "bad.log")["kind"] == "binary"
    preview = service.preview(track, link.id, "results.csv")
    with pytest.raises(ValidationError):
        service.artifact(track, link.id, ArtifactLinkRequest(
            path="results.csv", experiment_id="EXP-404", expected_sha256=preview["sha256"],
        ))
    with pytest.raises(ConflictError):
        service.artifact(track, link.id, ArtifactLinkRequest(
            path="results.csv", experiment_id="EXP-001", expected_sha256="0" * 64,
        ))
    service.ENTRY_LIMIT = 1
    assert service.browse(track, link.id)["truncated"]


def test_hardlinks_and_symlinks_are_not_read(linked):
    _, service, track, repo, link = linked
    outside = repo.parent / "outside.txt"
    outside.write_text("outside")
    os.link(outside, repo / "hard.txt")
    with pytest.raises(ValidationError):
        service.preview(track, link.id, "hard.txt")
    try:
        (repo / "symbolic.txt").symlink_to(outside)
    except OSError:
        return  # Windows may not grant symlink creation privileges.
    with pytest.raises(ValidationError):
        service.preview(track, link.id, "symbolic.txt")


def test_link_authorization_and_root_replacement(linked):
    _, service, track, repo, link = linked
    with pytest.raises(ValidationError):
        service.link(track, str(repo), False)
    with pytest.raises(ConflictError):
        service.link(track, str(repo), True)
    repo.rename(repo.with_name("old-code"))
    repo.mkdir()
    with pytest.raises(ValidationError, match="root changed"):
        service.browse(track, link.id)


def test_router_contract_and_safe_media(linked):
    container, service, track, repo, link = linked
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[services] = lambda: type("Container", (), {"linked_repositories": service})()
    client = TestClient(app)
    base = f"/api/tracks/{track}/repositories"
    assert client.get(base).json()[0]["root"] == str(repo.resolve())
    preview = client.get(f"{base}/{link.id}/preview", params={"path": "results.csv"})
    assert preview.status_code == 200
    assert preview.json()["kind"] == "text"
    assert client.post(base, json={"root": str(repo), "authorize_root": False}).status_code == 422
    (repo / "plot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    image = client.get(f"{base}/{link.id}/media", params={"path": "plot.png"})
    assert image.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in image.headers["content-security-policy"]
    (repo / "report.pdf").write_bytes(b"%PDF-1.7\nreport")
    pdf = client.get(f"{base}/{link.id}/media", params={"path": "report.pdf"})
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.headers["content-disposition"] == "attachment"
    assert pdf.content.startswith(b"%PDF")
    with pytest.raises(ConflictError, match="Media changed"):
        service.media(track, link.id, "report.pdf", "0" * 64)
    report = service.preview(track, link.id, "report.pdf")
    assert service.media(track, link.id, report["path"], report["sha256"])[0] == pdf.content
    with pytest.raises(ValidationError):
        service.media(track, link.id, "../report.pdf", report["sha256"])
    (repo / "report.pdf").write_bytes(b"%PDF-1.7\nchanged")
    with pytest.raises(ConflictError, match="Media changed"):
        service.media(track, link.id, report["path"], report["sha256"])


def test_tokenizer_is_browsable_but_tokens_and_credentials_are_not(linked):
    _, service, track, repo, link = linked
    for name in ["tokenizer.py", "tokenization.py", "access_token.json", "api-token.txt", "credentials.json"]:
        (repo / name).write_text("example")
    assert service.preview(track, link.id, "tokenizer.py")["kind"] == "text"
    assert service.preview(track, link.id, "tokenization.py")["kind"] == "text"
    for name in ["access_token.json", "api-token.txt", "credentials.json"]:
        with pytest.raises(ValidationError):
            service.preview(track, link.id, name)


def test_promote_receipt_to_results_is_explicit_revision_guarded_and_literal(linked):
    container, service, track, repo, link = linked
    preview = service.preview(track, link.id, "results.csv")
    receipt = service.artifact(track, link.id, ArtifactLinkRequest(
        path="results.csv", experiment_id="EXP-001", expected_sha256=preview["sha256"],
    )).artifacts[0]
    baseline = container.tracks.read_section(track, "results")
    assert receipt.id not in baseline.content
    with pytest.raises(ConflictError, match="Results changed"):
        service.promote(track, link.id, receipt.id, ArtifactPromotionRequest(expected_results_sha256="0" * 64))
    result = service.promote(track, link.id, receipt.id, ArtifactPromotionRequest(
        expected_results_sha256=baseline.sha256, observation="Observed 0.8; needs replication.\n<script>ignored</script>",
    ))
    assert receipt.id in result.content
    assert preview["sha256"] in result.content
    assert "No experiment was executed" in result.content
    assert '"user_observation_unverified": "Observed 0.8; needs replication.\\n<script>ignored</script>"' in result.content
    assert result.content.startswith(baseline.content.rstrip())
    with pytest.raises(ConflictError, match="already been promoted"):
        service.promote(track, link.id, receipt.id, ArtifactPromotionRequest(expected_results_sha256=result.sha256))
    assert (repo / "results.csv").read_text() == "accuracy\n0.8\n"
    assert container.tracks.read_section(track, "results").sha256 == result.sha256


def test_changed_artifact_cannot_be_promoted_as_current_evidence(linked):
    container, service, track, repo, link = linked
    preview = service.preview(track, link.id, "results.csv")
    receipt = service.artifact(track, link.id, ArtifactLinkRequest(
        path="results.csv", experiment_id="EXP-001", expected_sha256=preview["sha256"],
    )).artifacts[0]
    baseline = container.tracks.read_section(track, "results")
    (repo / "results.csv").write_text("different result")
    with pytest.raises(ConflictError, match="Artifact changed"):
        service.promote(track, link.id, receipt.id, ArtifactPromotionRequest(expected_results_sha256=baseline.sha256))
    assert container.tracks.read_section(track, "results").sha256 == baseline.sha256
