from __future__ import annotations

import io
import json
import stat
import zipfile

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.agents.blueprint import SessionPolicySpec
from backend.bootstrap import create_services
from backend.core.errors import ConflictError, NotFoundError, ValidationError
from backend.persistence.files import StorageError
from backend.tracks.markdown import SECTIONS
from backend.tracks.repository import TrackRepository
from backend.tracks.service import TrackService


TABLE = "| ID | Experiment | Status | Success criterion | Evidence |\n| --- | --- | --- | --- | --- |\n"


def experiment(identifier="EXP-001", status="Suggested") -> str:
    return TABLE + f"| {identifier} | Ablation | {status} | Improved accuracy | [log](logs.md) |\n\n## {identifier}\n\nDetailed protocol.\n"


@pytest.fixture()
def container(test_settings):
    return create_services(test_settings)


def revisions(preview) -> dict[str, str]:
    return {item.section: item.expected_sha256 for item in preview.sections}


def write(tracks, track_id: str, section: str, content: str):
    current = tracks.read_section(track_id, section)
    return tracks.write_section(track_id, section, content, current.sha256)


def zip_bytes(files: dict[str, str | bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return output.getvalue()


def archive_files(tracks, track_id: str) -> dict[str, str]:
    data, _, _ = tracks.export(track_id)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name).decode() for name in archive.namelist()}


def test_track_restart_persistence_templates_and_conversations(container):
    track = container.tracks.create("A local research problem")
    assert len(track.sections) == 4
    assert track.status_counts["Completed"] == 0
    assert not track.warnings
    assert "Success criteria" in container.tracks.read_section(track.id, "problem").content
    conversation = container.conversations.create(
        title="Track chat", kind="agent", model_reference={}, session_policy=SessionPolicySpec(),
    )
    container.tracks.link_conversation(track.id, conversation.id)
    restarted = TrackService(
        container.workspace, container.storage, container.documents,
        TrackRepository(container.session_factory),
    )
    assert restarted.list()[0].id == track.id
    assert restarted.for_conversation(conversation.id).id == track.id
    assert restarted.get(track.id).conversation_ids == [conversation.id]
    restarted.unlink_conversation(conversation.id)
    assert restarted.for_conversation(conversation.id) is None
    reconstructed = create_services(container.settings)
    assert reconstructed.tracks.get(track.id).title == track.title
    with pytest.raises(NotFoundError):
        restarted.link_conversation(track.id, "missing")
    with pytest.raises(ValidationError):
        restarted.create("  ")


def test_exploration_files_revision_history_indexing_and_existing_tracks(container):
    track = container.tracks.create("Exploration")
    folder = container.storage.resolve_path(container.settings.workspace_dir, f"{track.folder_path}/exploration")
    assert folder.is_dir()
    assert container.tracks.get(track.id).exploration == []
    folder.rmdir()
    restarted = TrackService(container.workspace, container.storage, container.documents,
                             TrackRepository(container.session_factory))
    assert folder.is_dir()
    first = restarted.write_section(track.id, "exploration/retrieval-ideas", "# Hypotheses\n\nexplorationneedle", None)
    second = restarted.write_section(track.id, "exploration/open-questions", "# Questions", None)
    assert len(restarted.get(track.id).sections) == 4
    assert {item.section for item in restarted.get(track.id).exploration} == {first.section, second.section}
    assert container.workspace.search(query="explorationneedle", kinds=["track_section"])[0].path == first.path
    with pytest.raises(ValidationError):
        restarted.write_section(track.id, first.section, "Overwrite without reading", None)
    with pytest.raises(ConflictError):
        restarted.write_section(track.id, first.section, "Stale overwrite", "0" * 64)
    updated = restarted.write_section(track.id, first.section, first.content + "\nNew lead", first.sha256)
    assert restarted.read_history(track.id, first.section, restarted.history(track.id, first.section)[0].revision).content == first.content
    with pytest.raises(ValidationError):
        container.workspace.write_file(first.path, "Bypass")
    assert restarted.read_section(track.id, first.section).sha256 == updated.sha256
    with pytest.raises(ValidationError):
        restarted.write_section(track.id, "problem", "No revision", None)
    for section in ("exploration/../results", "exploration/a/b", "exploration/CON", "exploration/con", "exploration/a.md", "exploration/"):
        with pytest.raises(ValidationError):
            restarted.write_section(track.id, section, "Invalid path", None)


def test_exploration_zip_roundtrip_and_new_file_conflicts(container):
    tracks = container.tracks
    track = tracks.create("Roundtrip exploration")
    document = tracks.write_section(track.id, "exploration/search-leads", "Preliminary leads only.", None)
    exported, _, _ = tracks.export(track.id)
    with zipfile.ZipFile(io.BytesIO(exported)) as archive:
        assert archive.read("exploration/search-leads.md").decode() == document.content
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["format_version"] == 2
        assert manifest["exploration"] == ["exploration/search-leads.md"]
    container.storage.delete_workspace_file(document.path)
    container.workspace.index_path(document.path)
    preview = tracks.preview_zip(track.id, exported)
    assert next(item for item in preview.sections if item.section == document.section).expected_sha256 is None
    tracks.apply_import(track.id, preview.preview_id, "replace", revisions(preview), True)
    assert tracks.read_section(track.id, document.section).content == document.content
    preview = tracks.preview_import(track.id, "exploration/new-idea", "Incoming idea")
    tracks.write_section(track.id, "exploration/new-idea", "Researcher wrote first", None)
    with pytest.raises(ConflictError):
        tracks.apply_import(track.id, preview.preview_id, "replace", revisions(preview), True)
    assert tracks.read_section(track.id, "exploration/new-idea").content == "Researcher wrote first"
    legacy = archive_files(tracks, track.id)
    for name in list(legacy):
        if name.startswith("exploration/"):
            del legacy[name]
    manifest = json.loads(legacy["manifest.json"])
    manifest["format_version"] = 1
    del manifest["exploration"]
    legacy["manifest.json"] = json.dumps(manifest)
    preview = tracks.preview_zip(track.id, zip_bytes(legacy))
    tracks.apply_import(track.id, preview.preview_id, "replace", revisions(preview), True)
    assert len(tracks.get(track.id).exploration) == 2
    files = archive_files(tracks, track.id)
    manifest = json.loads(files["manifest.json"])
    manifest["exploration"] = []
    files["manifest.json"] = json.dumps(manifest)
    with pytest.raises(ValidationError, match="exploration"):
        tracks.preview_zip(track.id, zip_bytes(files))


def test_exploration_rollback_removes_created_files_and_recovers_pending_creation(container, monkeypatch):
    tracks = container.tracks
    track = tracks.create("Rollback exploration")
    original = container.workspace.index_path

    def fail_index(path):
        if path.endswith("/new-idea.md"):
            raise StorageError("Index failure")
        return original(path)

    monkeypatch.setattr(container.workspace, "index_path", fail_index)
    with pytest.raises(ConflictError, match="rollback"):
        tracks.write_section(track.id, "exploration/new-idea", "Uncommitted", None)
    monkeypatch.setattr(container.workspace, "index_path", original)
    reconstructed = TrackService(container.workspace, container.storage, container.documents,
                                 TrackRepository(container.session_factory))
    assert reconstructed.get(track.id).exploration == []
    with pytest.raises(NotFoundError):
        reconstructed.read_section(track.id, "exploration/new-idea")


def test_exploration_http_creation_read_history_and_import(test_settings):
    with TestClient(create_app(test_settings)) as client:
        track = client.post("/api/tracks", json={"title": "Researcher exploration"}).json()
        url = f"/api/tracks/{track['id']}/exploration/questions"
        first = client.put(url, json={"content": "# Questions", "expected_sha256": None})
        assert first.status_code == 200, first.text
        assert client.get(url).json()["content"] == "# Questions"
        assert client.put(url, json={"content": "Overwrite", "expected_sha256": None}).status_code == 400
        updated = client.put(url, json={"content": "# Updated questions", "expected_sha256": first.json()["sha256"]})
        assert updated.status_code == 200
        assert len(client.get(url + "/history").json()) == 1
        detail = client.get(f"/api/tracks/{track['id']}").json()
        assert detail["exploration"][0]["section"] == "exploration/questions"
        preview = client.post(f"/api/tracks/{track['id']}/imports/preview", json={
            "section": "exploration/imported-results-leads", "content": "External exploratory observations",
        })
        assert preview.status_code == 200, preview.text
        body = preview.json()
        applied = client.post(f"/api/tracks/{track['id']}/imports", json={
            "preview_id": body["preview_id"], "mode": "replace", "confirmed_replace": True,
            "expected_revisions": {"exploration/imported-results-leads": None},
        })
        assert applied.status_code == 200, applied.text
        assert len(applied.json()["exploration"]) == 2


@pytest.mark.parametrize("status", ["Running", "Completed"])
def test_experiment_status_is_markdown_authoritative(container, status):
    track = container.tracks.create("Status")
    write(container.tracks, track.id, "experiments", experiment(status=status))
    detail = container.tracks.get(track.id)
    assert detail.experiments[0].status == status
    assert detail.experiments[0].success_criterion == "Improved accuracy"
    assert detail.status_counts[status] == 1
    assert detail.experiments[0].evidence == "[log](logs.md)"


@pytest.mark.parametrize("content", [
    experiment(status="Done"),
    experiment() + TABLE,
    TABLE + "| EXP-001 | first | Planned | | |\n| EXP-001 | second | Planned | | |\n",
    TABLE + "| 001 | broken | Planned | | |\n",
    "| ID | Experiment | Status |\n| --- | --- | --- |\n",
    TABLE + "| EXP-001 | broken | Planned | |\n",
], ids=["status", "two-tables", "duplicate-id", "bad-id", "columns", "short-row"])
def test_invalid_experiment_tracker_never_mutates(container, content):
    track = container.tracks.create("Validate")
    original = container.tracks.read_section(track.id, "experiments")
    with pytest.raises(ValidationError):
        container.tracks.write_section(track.id, "experiments", content, original.sha256)
    assert container.tracks.read_section(track.id, "experiments") == original
    assert container.tracks.history(track.id, "experiments") == []


def test_review_publication_links_papers_atomically_and_is_replay_safe(container, monkeypatch):
    paper = container.document_repository.create_from_bytes(b"%PDF-local", filename="paper.pdf", title="Existing evidence")
    track = container.tracks.create("Publication")
    before = container.tracks.read_section(track.id, "literature")
    marker = "<!-- scholarweave-literature-review:test:edition -->"
    body = f"{marker}\n# Findings\n\nA source-backed result."
    original_index = container.workspace.index_path
    fail = True

    def index(path):
        nonlocal fail
        if fail and path == before.path:
            fail = False
            raise StorageError("Interrupted publication indexing")
        return original_index(path)

    monkeypatch.setattr(container.workspace, "index_path", index)
    with pytest.raises(StorageError, match="Interrupted"):
        container.tracks.append_literature_review(track.id, body, marker=marker, paper_ids=[paper.id])
    assert container.tracks.read_section(track.id, "literature").content == before.content
    assert container.tracks.get(track.id).paper_ids == []
    saved = container.tracks.append_literature_review(track.id, body, marker=marker, paper_ids=[paper.id])
    assert container.tracks.get(track.id).paper_ids == [paper.id]
    assert "| Paper |" not in saved.content
    assert container.workspace.search(query="source-backed", kinds=["track_section"])[0].path == saved.path
    edited = container.tracks.write_section(track.id, "literature", saved.content + "\nResearcher's note.", saved.sha256)
    replayed = container.tracks.append_literature_review(track.id, body, marker=marker, paper_ids=[paper.id])
    assert replayed.content == edited.content
    assert replayed.content.count(marker) == 1
    assert before.content in [
        container.tracks.read_history(track.id, "literature", item.revision).content
        for item in container.tracks.history(track.id, "literature")
    ]
    with pytest.raises(NotFoundError):
        container.tracks.append_literature_review(track.id, body, marker=marker, paper_ids=["missing"])
    assert container.tracks.read_section(track.id, "literature").content == edited.content


def test_paper_associations_link_canonical_sources_without_copying(container):
    paper = container.document_repository.create_from_bytes(
        b"%PDF-local", filename="paper.pdf", title="Existing evidence",
    )
    canonical = container.workspace.ensure_paper_folder(paper.id, paper.title)
    summary = container.workspace.read_file(canonical["summary_path"])
    track = container.tracks.create("Reuse")
    container.tracks.add_paper(track.id, paper.id)
    detail = container.tracks.add_paper(track.id, paper.id)
    assert detail.paper_ids == [paper.id]
    literature = container.tracks.read_section(track.id, "literature").content
    assert literature.count(f"({paper.id})") == 1
    assert "source.pdf" in literature and "summary.md" in literature and "notes.md" in literature
    assert container.workspace.read_file(canonical["summary_path"]) == summary
    folder = container.storage.resolve_path(container.settings.workspace_dir, track.folder_path)
    assert {path.name for path in folder.iterdir()} == {f"{section}.md" for section in SECTIONS} | {"exploration"}
    assert container.tracks.remove_paper(track.id, paper.id).paper_ids == []
    container.tracks.add_paper(track.id, paper.id)
    assert container.tracks.read_section(track.id, "literature").content == literature
    assert container.documents.get_document(paper.id) is not None
    with pytest.raises(NotFoundError):
        container.tracks.add_paper(track.id, "missing")


def test_external_edits_revision_history_and_workspace_index(container):
    tracks = container.tracks
    track = tracks.create("History")
    original = tracks.read_section(track.id, "problem")
    updated = tracks.write_section(track.id, "problem", "applicationneedle", original.sha256)
    history = tracks.history(track.id, "problem")
    assert tracks.read_history(track.id, "problem", history[0].revision).content == original.content
    with pytest.raises(ConflictError):
        tracks.write_section(track.id, "problem", "stale", original.sha256)
    container.storage.write_workspace_file(updated.path, "externalneedle")
    with pytest.raises(ConflictError):
        tracks.write_section(track.id, "problem", "lost", updated.sha256)
    detail = tracks.get(track.id)
    assert next(section for section in detail.sections if section.section == "problem").sha256 != updated.sha256
    actual = container.storage.workspace_file_info(updated.path)
    assert next(section for section in detail.sections if section.section == "problem").modified_at == actual.modified_at
    assert container.workspace.search(query="externalneedle", kinds=["track_section"])[0].path == updated.path
    current = tracks.read_section(track.id, "problem")
    tracks.write_section(track.id, "problem", "reviewedneedle", current.sha256)
    saved = [tracks.read_history(track.id, "problem", item.revision).content for item in tracks.history(track.id, "problem")]
    assert "externalneedle" in saved
    assert not container.workspace.search(query="externalneedle")
    assert all("track_history" not in item.path for item in container.workspace.list_files())
    with pytest.raises(ValidationError):
        tracks.write_section(track.id, "problem", "missing revision", None)
    container.storage.delete_workspace_file(current.path)
    with pytest.raises(ConflictError, match="deleted"):
        tracks.write_section(track.id, "problem", "deleted", current.sha256)


def test_external_invalid_status_and_ambiguous_results_have_honest_warnings(container):
    track = container.tracks.create("Warnings")
    write(container.tracks, track.id, "results", "It improved. EXP-099 has no protocol.")
    assert any("EXP-099" in warning for warning in container.tracks.get(track.id).warnings)
    write(container.tracks, track.id, "results", "It improved, but the experiment is not identified.")
    assert any("ambiguous" in warning for warning in container.tracks.get(track.id).warnings)
    current = container.tracks.read_section(track.id, "experiments")
    container.storage.write_workspace_file(current.path, experiment(status="Done"))
    detail = container.tracks.get(track.id)
    assert not detail.experiments
    assert any("Invalid experiment status" in warning for warning in detail.warnings)
    arbitrary = container.tracks.preview_import(track.id, "experiments", "# Legacy protocol\nNo status table.")
    assert any("no recognized" in warning for warning in arbitrary.warnings)


def test_single_import_confirmation_merge_collision_manual_review_and_history(container):
    tracks = container.tracks
    track = tracks.create("Import")
    write(tracks, track.id, "experiments", experiment())
    preview = tracks.preview_import(track.id, "experiments", experiment("EXP-002", "Planned"))
    tracks.apply_import(track.id, preview.preview_id, "merge", revisions(preview))
    assert [row.id for row in tracks.get(track.id).experiments] == ["EXP-001", "EXP-002"]
    assert "Imported fragment" in tracks.read_section(track.id, "experiments").content
    collision = tracks.preview_import(track.id, "experiments", experiment("EXP-001", "Running"))
    with pytest.raises(ValidationError, match="collisions"):
        tracks.apply_import(track.id, collision.preview_id, "merge", revisions(collision))
    manually = experiment("EXP-001", "Running") + "\nAdditional reviewed context."
    tracks.apply_import(track.id, collision.preview_id, "merge", revisions(collision),
                        merged_content={"experiments": manually})
    preview = tracks.preview_import(track.id, "problem", "Incoming")
    assert preview.sections[0].diff
    with pytest.raises(ValidationError, match="confirmation"):
        tracks.apply_import(track.id, preview.preview_id, "replace", revisions(preview))
    tracks.apply_import(track.id, preview.preview_id, "replace", revisions(preview), True)
    assert tracks.read_section(track.id, "problem").content == "Incoming"
    assert tracks.history(track.id, "problem")
    with pytest.raises(NotFoundError):
        tracks.apply_import(track.id, preview.preview_id, "replace", revisions(preview), True)


def test_experiment_merge_accepts_valid_tracker_without_outer_pipes(container):
    tracks = container.tracks
    track = tracks.create("Markdown dialect")
    alternate = "\n".join(line.strip("|") for line in experiment().splitlines())
    write(tracks, track.id, "experiments", alternate)
    preview = tracks.preview_import(track.id, "experiments", experiment("EXP-002"))
    tracks.apply_import(track.id, preview.preview_id, "merge", revisions(preview))
    assert {row.id for row in tracks.get(track.id).experiments} == {"EXP-001", "EXP-002"}


def test_zip_export_roundtrip_metadata_and_all_conflicts_precede_mutation(container):
    tracks = container.tracks
    paper = container.document_repository.create_from_bytes(b"%PDF", filename="existing.pdf", title="Existing")
    track = tracks.create("Roundtrip")
    tracks.add_paper(track.id, paper.id)
    write(tracks, track.id, "experiments", experiment(status="Completed"))
    exported, media, filename = tracks.export(track.id)
    assert media == "application/zip" and filename.endswith(".zip")
    preview = tracks.preview_zip(track.id, exported)
    assert preview.paper_ids == [paper.id]
    tracks.remove_paper(track.id, paper.id)
    with pytest.raises(ConflictError, match="associations"):
        tracks.apply_import(track.id, preview.preview_id, "replace", revisions(preview), True)
    preview = tracks.preview_zip(track.id, exported)
    tracks.apply_import(track.id, preview.preview_id, "replace", revisions(preview), True)
    assert tracks.get(track.id).paper_ids == [paper.id]
    single, media, filename = tracks.export(track.id, "experiments")
    assert single.decode() == experiment(status="Completed")
    assert media.startswith("text/markdown") and filename == "experiments.md"
    preview = tracks.preview_zip(track.id, exported)
    write(tracks, track.id, "results", "external local change")
    before = {section: tracks.read_section(track.id, section).content for section in SECTIONS}
    with pytest.raises(ConflictError):
        tracks.apply_import(track.id, preview.preview_id, "replace", revisions(preview), True)
    assert before == {section: tracks.read_section(track.id, section).content for section in SECTIONS}
    fresh = tracks.preview_zip(track.id, exported)
    with pytest.raises(ConflictError):
        tracks.apply_import(track.id, fresh.preview_id, "replace", {}, True)


def test_import_validates_all_manual_sections_and_rolls_back_runtime_failure(container, monkeypatch):
    tracks = container.tracks
    track = tracks.create("Atomic")
    exported, _, _ = tracks.export(track.id)
    preview = tracks.preview_zip(track.id, exported)
    before = {section: tracks.read_section(track.id, section).content for section in SECTIONS}
    with pytest.raises(ValidationError):
        tracks.apply_import(track.id, preview.preview_id, "merge", revisions(preview),
                            merged_content={"problem": "valid", "experiments": experiment(status="invalid")})
    assert before == {section: tracks.read_section(track.id, section).content for section in SECTIONS}
    original_write = container.storage.write_workspace_file
    fail_once = True

    def fail_on_literature(path, content):
        nonlocal fail_once
        if path.endswith("/literature.md") and fail_once:
            fail_once = False
            raise StorageError("Simulated disk failure")
        return original_write(path, content)

    monkeypatch.setattr(container.storage, "write_workspace_file", fail_on_literature)
    with pytest.raises(StorageError):
        tracks.apply_import(track.id, preview.preview_id, "merge", revisions(preview),
                            merged_content={"problem": "replacement", "literature": "replacement"})
    assert before == {section: tracks.read_section(track.id, section).content for section in SECTIONS}


@pytest.mark.parametrize("archive", [False, True], ids=["markdown", "zip"])
def test_refreshed_import_accepts_reviewed_merge_only_with_current_preview_revisions(container, archive):
    tracks = container.tracks
    track = tracks.create("Reconcile reviewed import")
    write(tracks, track.id, "problem", "Incoming problem")
    exported, _, _ = tracks.export(track.id)
    write(tracks, track.id, "problem", "Original problem")

    def preview():
        return tracks.preview_zip(track.id, exported) if archive else tracks.preview_import(
            track.id, "problem", "Incoming problem",
        )

    original = preview()
    reviewed = "Original problem\nReviewed incoming problem"
    write(tracks, track.id, "problem", "External evidence")
    with pytest.raises(ConflictError):
        tracks.apply_import(track.id, original.preview_id, "merge", revisions(original),
                            merged_content={"problem": reviewed})
    refreshed = preview()
    assert refreshed.preview_id != original.preview_id
    assert next(item for item in refreshed.sections if item.section == "problem").current_content == "External evidence"
    assert tracks.read_section(track.id, "problem").content == "External evidence"
    with pytest.raises(ConflictError, match="preview"):
        tracks.apply_import(track.id, refreshed.preview_id, "merge", revisions(original),
                            merged_content={"problem": reviewed})
    write(tracks, track.id, "problem", "External evidence changed again")
    with pytest.raises(ConflictError):
        tracks.apply_import(track.id, refreshed.preview_id, "merge", revisions(refreshed),
                            merged_content={"problem": reviewed + "\nExternal evidence"})
    final = preview()
    reconciled = reviewed + "\nExternal evidence changed again"
    tracks.apply_import(track.id, final.preview_id, "merge", revisions(final),
                        merged_content={"problem": reconciled})
    assert tracks.read_section(track.id, "problem").content == reconciled
    assert any(tracks.read_history(track.id, "problem", item.revision).content == "External evidence changed again"
               for item in tracks.history(track.id, "problem"))


def test_pending_mutation_recovers_previous_markdown_and_metadata_after_restart(container):
    tracks = container.tracks
    track = tracks.create("Interrupted")
    original = tracks.read_section(track.id, "problem")
    tracks._write_journal(track.id, {
        "state": "prepared", "contents": {"problem": original.content}, "paper_ids": [],
    })
    container.storage.write_workspace_file(original.path, "interrupted half-import")
    restarted = create_services(container.settings)
    assert restarted.tracks.read_section(track.id, "problem").content == original.content
    assert not restarted.workspace.search(query="half-import")
    journal = container.storage.resolve_path(container.settings.data_dir, tracks._journal_path(track.id))
    assert not journal.exists()


@pytest.mark.parametrize("unsafe_name", ["../problem.md", "/problem.md", r"..\problem.md", "nested/problem.md", "PROBLEM.md"],
                         ids=["traversal", "absolute", "windows-traversal", "nested", "case"])
def test_zip_rejects_noncanonical_and_traversal_entries(container, unsafe_name):
    track = container.tracks.create("Safe ZIP")
    files = archive_files(container.tracks, track.id)
    files[unsafe_name] = files.pop("problem.md")
    with pytest.raises(ValidationError):
        container.tracks.preview_zip(track.id, zip_bytes(files))


def test_zip_rejects_symlinks_duplicates_utf8_bombs_and_manifest_identity(container):
    tracks = container.tracks
    track = tracks.create("Malicious ZIP")
    files = archive_files(tracks, track.id)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in files.items():
            info = zipfile.ZipInfo(name)
            if name == "problem.md":
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, content)
    with pytest.raises(ValidationError):
        tracks.preview_zip(track.id, output.getvalue())
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
        with pytest.warns(UserWarning):
            archive.writestr("problem.md", "duplicate")
    with pytest.raises(ValidationError):
        tracks.preview_zip(track.id, output.getvalue())
    files["problem.md"] = b"\xff"
    with pytest.raises(ValidationError):
        tracks.preview_zip(track.id, zip_bytes(files))
    files["problem.md"] = "A" * 100_000
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    with pytest.raises(ValidationError, match="expansion"):
        tracks.preview_zip(track.id, output.getvalue())
    files["problem.md"] = "Valid"
    manifest = json.loads(files["manifest.json"])
    manifest["track_id"] = "different"
    files["manifest.json"] = json.dumps(manifest)
    with pytest.raises(ValidationError, match="identity"):
        tracks.preview_zip(track.id, zip_bytes(files))
    for data in [b"not a ZIP", b""]:
        with pytest.raises(ValidationError):
            tracks.preview_zip(track.id, data)


def test_workspace_protects_managed_tracks_across_all_mutation_paths(container):
    track = container.tracks.create("Protected")
    current = container.tracks.read_section(track.id, "problem")
    workspace = container.workspace
    for operation in [
        lambda: workspace.write_file(current.path, "bad", expected_sha256=current.sha256),
        lambda: workspace.edit_note(current.path, content="bad", expected_sha256=current.sha256),
        lambda: workspace.append_markdown(current.path, "bad"),
        lambda: workspace.replace_markdown(current.path, "Research", "bad"),
        lambda: workspace.delete_file(current.path),
        lambda: workspace.delete_folder(track.folder_path),
        lambda: workspace.delete_folder("projects/tracks"),
        lambda: workspace.organize_file(action="move_file", path=current.path, destination="other.md"),
        lambda: workspace.write_file(current.path.replace("/", "\\"), "bad"),
    ]:
        with pytest.raises((ValidationError, ValueError)):
            operation()
    note = workspace.create_note(name="Ordinary", content="keep")
    with pytest.raises(ValidationError):
        workspace.organize_file(action="move_file", path=note.path, destination=current.path)
    assert container.tracks.read_section(track.id, "problem").content == current.content


def test_track_api_contract_requires_revision_and_preview_confirmation(test_settings):
    app = create_app(test_settings)
    with TestClient(app) as client:
        response = client.post("/api/tracks", json={"title": "API research"})
        assert response.status_code == 201, response.text
        track = response.json()
        assert len(client.get("/api/tracks").json()) == 1
        base = f"/api/tracks/{track['id']}"
        section = client.get(base + "/sections/problem").json()
        assert client.put(base + "/sections/problem", json={"content": "bad"}).status_code == 422
        saved = client.put(base + "/sections/problem", json={
            "content": "New question", "expected_sha256": section["sha256"],
        })
        assert saved.status_code == 200, saved.text
        assert client.put(base + "/sections/problem", json={
            "content": "stale", "expected_sha256": section["sha256"],
        }).status_code == 409
        history = client.get(base + "/sections/problem/history").json()
        assert client.get(base + f"/sections/problem/history/{history[0]['revision']}").json()["content"] == section["content"]
        exported = client.get(base + "/export")
        preview = client.post(base + "/imports/preview-zip",
                              files={"file": ("track.zip", exported.content, "application/zip")})
        assert preview.status_code == 200, preview.text
        preview = preview.json()
        payload = {
            "preview_id": preview["preview_id"], "mode": "replace",
            "expected_revisions": {item["section"]: item["expected_sha256"] for item in preview["sections"]},
        }
        assert client.post(base + "/imports", json=payload).status_code == 400
        payload["confirmed_replace"] = True
        assert client.post(base + "/imports", json=payload).status_code == 200
        assert client.get(base + "/sections/not-a-section").status_code == 422
        assert client.put("/api/workspace/files/content", json={
            "path": section["path"], "content": "bad", "expected_sha256": saved.json()["sha256"],
        }).status_code == 400
