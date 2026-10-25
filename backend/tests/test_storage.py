from __future__ import annotations

import uuid
import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from backend.core.errors import ConflictError, ValidationError
from backend.persistence.files import SafeStorage, StorageError
from backend.workspace.service import WorkspaceService


@pytest.mark.parametrize("content,removed", [
    ("", True),
    ("x" * 1023, True),
    ("x" * 1024, False),
    ("\u00e9" * 511 + "x", True),
    ("\u00e9" * 512, False),
    ("x" * 1022 + "\r\n", False),
], ids=["empty", "1023-bytes", "1024-bytes", "unicode-1023", "unicode-1024", "crlf-1024"])
def test_note_cleanup_uses_strict_raw_byte_boundary(test_settings, content, removed) -> None:
    workspace = WorkspaceService(SafeStorage(test_settings))
    path = "knowledge/boundary.md"
    workspace.write_file(path, content)
    preview = workspace.preview_note_cleanup()
    assert [item["path"] for item in preview["candidates"]] == ([path] if removed else [])
    assert preview["failed"] == []
    if removed:
        candidate = preview["candidates"][0]
        assert candidate["size_bytes"] == len(content.encode("utf-8"))
        assert candidate["sha256"] == hashlib.sha256(content.encode("utf-8")).hexdigest()
    result = workspace.cleanup_notes(preview["candidates"])
    assert result == {"deleted_paths": [path] if removed else [], "skipped": [], "failed": []}
    assert [item.path for item in workspace.list_files()] == ([] if removed else [path])
    assert (test_settings.workspace_dir / path).exists() is not removed


def test_note_cleanup_limits_scope_and_rechecks_protected_paths(test_settings) -> None:
    storage = SafeStorage(test_settings)
    workspace = WorkspaceService(storage)
    note = workspace.create_note(name="Stub", content="")
    paper = workspace.ensure_paper_folder("cleanup-paper", "Cleanup paper")
    protected = [
        paper["summary_path"], f"{paper['folder']}/summaries/version.md",
        f"{paper['folder']}/attachments/notes.md", "research/review/report.md",
        "projects/tracks/track--cleanup/problem.md",
        "projects/tracks/track--cleanup/exploration/note.md",
        ".scholarweave/internal.md", "inbox/attachments/upload/notes.md",
        "knowledge/morning-brief/receipt.json", "other/notes.md",
    ]
    for path in protected:
        storage.write_workspace_file(path, {} if path.endswith(".json") else "protected")
    workspace.refresh_index()
    preview = workspace.preview_note_cleanup()
    assert {item["path"] for item in preview["candidates"]} == {note.path, paper["notes_path"]}
    forged = [
        {"path": path, "size_bytes": storage.workspace_file_info(path).size_bytes,
         "sha256": storage.file_hash(test_settings.workspace_dir, path)}
        for path in protected
    ]
    result = workspace.cleanup_notes([*preview["candidates"], *forged])
    assert set(result["deleted_paths"]) == {note.path, paper["notes_path"]}
    assert {item["path"] for item in result["skipped"]} == set(protected)
    assert result["failed"] == []
    assert all((test_settings.workspace_dir / path).is_file() for path in protected)
    assert workspace.search(query="Stub") == []


def test_note_cleanup_revalidates_changes_and_reports_partial_failure(test_settings, monkeypatch) -> None:
    storage = SafeStorage(test_settings)
    workspace = WorkspaceService(storage)
    paths = {name: f"knowledge/{name}.md" for name in ["grown", "changed", "gone", "denied", "remove"]}
    for path in paths.values():
        workspace.write_file(path, "stub")
    preview = workspace.preview_note_cleanup()
    storage.write_workspace_file(paths["grown"], "x" * 1024)
    storage.write_workspace_file(paths["changed"], "edit")
    storage.delete_workspace_file(paths["gone"])
    delete = storage.delete_workspace_file

    def deny_one(path):
        if path == paths["denied"]:
            raise PermissionError("File is locked")
        delete(path)

    monkeypatch.setattr(storage, "delete_workspace_file", deny_one)
    result = workspace.cleanup_notes(preview["candidates"])
    assert result["deleted_paths"] == [paths["remove"]]
    assert {item["path"] for item in result["skipped"]} == {paths["grown"], paths["changed"], paths["gone"]}
    assert result["failed"] == [{"path": paths["denied"], "reason": "File is locked"}]
    assert workspace.read_file(paths["changed"]).content == "edit"
    assert workspace.read_file(paths["grown"]).content == "x" * 1024
    assert {item.path for item in workspace.list_files()} == {paths["grown"], paths["changed"], paths["denied"]}


def test_note_cleanup_preview_uses_disk_not_stale_index_and_never_deletes(test_settings) -> None:
    storage = SafeStorage(test_settings)
    workspace = WorkspaceService(storage)
    workspace.write_file("knowledge/shrunk.md", "x" * 1024)
    workspace.write_file("knowledge/grown.md", "stub")
    storage.write_workspace_file("knowledge/shrunk.md", "")
    storage.write_workspace_file("knowledge/grown.md", "x" * 1024)
    storage.write_workspace_file("knowledge/new.md", "new external note")
    preview = workspace.preview_note_cleanup()
    assert {item["path"] for item in preview["candidates"]} == {"knowledge/shrunk.md", "knowledge/new.md"}
    assert all((test_settings.workspace_dir / item["path"]).exists() for item in preview["candidates"])
    assert workspace.cleanup_notes([]) == {"deleted_paths": [], "skipped": [], "failed": []}


def test_note_cleanup_keeps_unreadable_notes_and_reports_preview_errors(test_settings, monkeypatch) -> None:
    storage = SafeStorage(test_settings)
    workspace = WorkspaceService(storage)
    path = "knowledge/unreadable.md"
    workspace.write_file(path, "stub")
    snapshot = storage.read_workspace_file_snapshot

    def unreadable(candidate):
        if candidate == path:
            raise PermissionError("Cannot read note")
        return snapshot(candidate)

    monkeypatch.setattr(storage, "read_workspace_file_snapshot", unreadable)
    assert workspace.preview_note_cleanup() == {
        "threshold_bytes": 1024, "candidates": [],
        "failed": [{"path": path, "reason": "Cannot read note"}],
    }
    assert (test_settings.workspace_dir / path).is_file()


def test_note_cleanup_serializes_duplicate_confirmations(test_settings) -> None:
    workspace = WorkspaceService(SafeStorage(test_settings))
    note = workspace.create_note(name="One stub", content="")
    candidates = workspace.preview_note_cleanup()["candidates"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(workspace.cleanup_notes, [candidates, candidates]))
    assert sorted(len(result["deleted_paths"]) for result in results) == [0, 1]
    assert sorted(len(result["skipped"]) for result in results) == [0, 1]
    assert all(result["failed"] == [] for result in results)
    assert not (test_settings.workspace_dir / note.path).exists()
    assert workspace.list_files() == []


def test_batched_tag_paths_match_search_order_and_casefolding(test_settings) -> None:
    workspace = WorkspaceService(SafeStorage(test_settings))
    older = workspace.create_note(name="Earlier", content="Earlier content", tags=["shared", "separate"])
    newer = workspace.create_note(name="Later", content="Later content", tags=["shared", "\u00c4stronomy"])
    paths = workspace.paths_for_tags(["SHARED", "separate", "\u00e4stronomy", "missing"])
    assert paths == {"shared": newer.path, "separate": older.path, "\u00e4stronomy": newer.path}
    assert paths["shared"] == workspace.search(tags=["shared"], limit=1)[0].path
    assert workspace.paths_for_tags([]) == {}


def test_note_edits_preserve_identity_and_exact_content_hash(test_settings) -> None:
    workspace = WorkspaceService(SafeStorage(test_settings))
    note = workspace.create_note(name="Evidence", content="Original", tags=["keep"])
    initial = workspace.read_file(note.path)
    assert initial.sha256 == hashlib.sha256(initial.content.encode("utf-8")).hexdigest()
    updated = workspace.edit_note(
        note.path, content="appendneedle", expected_sha256=initial.sha256,
    )
    assert updated.content == initial.content + "appendneedle"
    assert (updated.note_id, updated.note_name, updated.tags) == (
        initial.note_id, initial.note_name, initial.tags,
    )
    assert workspace.search(query="appendneedle")[0].path == note.path
    with pytest.raises(ConflictError, match="changed"):
        workspace.edit_note(note.path, content="duplicate", expected_sha256=initial.sha256)
    inserted = workspace.edit_note(
        note.path, operation="insert_after", selection="Original",
        content="\nNew evidence", expected_sha256=updated.sha256,
    )
    patched = workspace.edit_note(
        note.path, operation="replace", selection="New evidence",
        content="Correct evidence", expected_sha256=inserted.sha256,
    )
    assert "Original\nCorrect evidence" in patched.content
    assert workspace.read_file(note.path).sha256 == patched.sha256


def test_note_patch_rejects_missing_and_overlapping_ambiguous_selection(test_settings) -> None:
    workspace = WorkspaceService(SafeStorage(test_settings))
    note = workspace.create_note(name="Selections", content="aaa")
    for selection in ("missing", "aa", ""):
        with pytest.raises(ValidationError):
            workspace.edit_note(note.path, operation="replace", selection=selection, content="no")
        assert workspace.read_file(note.path).content == note.content


def test_note_operations_protect_summary_and_other_managed_artifacts(test_settings) -> None:
    workspace = WorkspaceService(SafeStorage(test_settings))
    paper = workspace.ensure_paper_folder("paper-1", "Paper")
    original = workspace.read_file(paper["summary_path"])
    for operation in ("append", "replace", "insert_after", "overwrite"):
        with pytest.raises(ValidationError, match="protected"):
            workspace.edit_note(
                paper["summary_path"], operation=operation, content="bad",
                selection="Contribution" if operation in {"replace", "insert_after"} else None,
            )
    for path in (
        f"{paper['folder']}/summaries/version.md", "inbox/attachments/upload/text.md",
        ".scholarweave/internal.md", f"{paper['folder']}/../notes.md",
    ):
        with pytest.raises(ValidationError):
            workspace.edit_note(path, content="bad")
    assert workspace.read_file(paper["summary_path"]).content == original.content
    updated = workspace.edit_note(paper["notes_path"], content="paperneedle")
    assert updated.paper_id == "paper-1"
    assert updated.kind == "paper_notes"
    assert workspace.search(query="paperneedle")[0].path == paper["notes_path"]


def test_note_concurrent_appends_and_conditional_saves_are_serialized(test_settings) -> None:
    workspace = WorkspaceService(SafeStorage(test_settings))
    note = workspace.create_note(name="Concurrent", content="")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: workspace.edit_note(note.path, content=f"entry-{i}\n"), range(24)))
    content = workspace.read_file(note.path).content
    for i in range(24):
        assert content.splitlines().count(f"entry-{i}") == 1
    revision = workspace.read_file(note.path).sha256

    def save(content: str) -> bool:
        try:
            workspace.write_file(note.path, content, expected_sha256=revision)
            return True
        except ConflictError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save, ["first", "second"])) == [False, True]


def test_conditional_save_detects_external_edit_and_deletion(test_settings) -> None:
    storage = SafeStorage(test_settings)
    workspace = WorkspaceService(storage)
    note = workspace.create_note(name="External", content="initial")
    storage.write_workspace_file(note.path, "external\r\ncontent")
    read = workspace.read_file(note.path)
    assert read.sha256 == hashlib.sha256(b"external\r\ncontent").hexdigest()
    with pytest.raises(ConflictError):
        workspace.write_file(note.path, "stale", expected_sha256=note.sha256)
    workspace.delete_file(note.path)
    with pytest.raises(ConflictError, match="deleted"):
        workspace.write_file(note.path, "resurrected", expected_sha256=read.sha256)
    assert workspace.list_files() == []


def test_workspace_atomic_write_failure_preserves_original_and_index(test_settings, monkeypatch) -> None:
    storage = SafeStorage(test_settings)
    workspace = WorkspaceService(storage)
    note = workspace.create_note(name="Atomic", content="originalneedle", tags=["keep"])
    target = storage.resolve_path(test_settings.workspace_dir, note.path)
    replace = Path.replace

    def fail_replace(source, destination):
        if destination == target:
            raise OSError("Replacement failed")
        return replace(source, destination)

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="Replacement failed"):
        workspace.edit_note(note.path, content="Not saved")
    assert workspace.read_file(note.path).content == note.content
    assert workspace.search(query="originalneedle")[0].tags == note.tags
    assert list(target.parent.glob("*.tmp")) == []


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_atomic_write_retries_windows_replacement_errors(test_settings, monkeypatch, winerror) -> None:
    storage = SafeStorage(test_settings)
    target = storage.write_bytes_atomic(test_settings.workspace_dir, "retry.json", b"old").absolute_path
    replace = Path.replace
    attempts = []
    delays = []

    def transient_replace(source, destination):
        attempts.append(source)
        if len(attempts) < 3:
            error = PermissionError("File is busy")
            error.winerror = winerror
            raise error
        return replace(source, destination)

    monkeypatch.setattr(Path, "replace", transient_replace)
    monkeypatch.setattr("backend.persistence.files.time.sleep", delays.append)
    result = storage.write_bytes_atomic(test_settings.workspace_dir, "retry.json", b"new")
    assert target.read_bytes() == b"new"
    assert result.sha256 == hashlib.sha256(b"new").hexdigest()
    assert len(attempts) == 3
    assert len(set(attempts)) == 1
    assert delays == [0.05, 0.1]
    assert not list(target.parent.glob("*.tmp"))


@pytest.mark.parametrize("winerror,expected_attempts", [(5, 5), (32, 5), (33, 5), (None, 1), (87, 1)])
def test_atomic_write_propagates_persistent_errors(test_settings, monkeypatch, winerror, expected_attempts) -> None:
    storage = SafeStorage(test_settings)
    target = storage.write_bytes_atomic(test_settings.workspace_dir, "retry.json", b"old").absolute_path
    attempts = []
    delays = []
    error = PermissionError("Replacement denied")
    if winerror is not None:
        error.winerror = winerror

    def fail_replace(source, destination):
        attempts.append(source)
        raise error

    monkeypatch.setattr(Path, "replace", fail_replace)
    monkeypatch.setattr("backend.persistence.files.time.sleep", delays.append)
    with pytest.raises(PermissionError) as raised:
        storage.write_bytes_atomic(test_settings.workspace_dir, "retry.json", b"new")
    assert raised.value is error
    assert len(attempts) == expected_attempts
    assert len(delays) == expected_attempts - 1
    assert sum(delays) <= 0.75
    assert target.read_bytes() == b"old"
    assert not list(target.parent.glob("*.tmp"))


def test_workspace_json_read_hash_covers_original_bytes(test_settings) -> None:
    storage = SafeStorage(test_settings)
    raw = b'{ "value": 1 }\r\n'
    storage.write_bytes(test_settings.workspace_dir, "data.json", raw)
    document = WorkspaceService(storage).read_file("data.json")
    assert document.content == {"value": 1}
    assert document.sha256 == hashlib.sha256(raw).hexdigest()


def test_workspace_atomic_writer_keeps_its_own_size_limit(test_settings) -> None:
    settings = test_settings.model_copy(update={"max_artifact_bytes": 4, "max_workspace_file_bytes": 32})
    storage = SafeStorage(settings)
    assert storage.write_workspace_file("note.md", "longer than four").size_bytes == 16
    with pytest.raises(StorageError, match="maximum"):
        storage.write_bytes_atomic(settings.artifacts_dir, "file.txt", b"too long")


def test_workspace_document_uses_upload_limit_without_relaxing_artifact_limits(test_settings) -> None:
    settings = test_settings.model_copy(update={"max_artifact_bytes": 4, "max_upload_bytes": 16})
    storage = SafeStorage(settings)
    path = "research/task/papers/paper/source.pdf"
    stored = storage.write_workspace_document(path, b"x" * 16)
    assert stored.absolute_path.read_bytes() == b"x" * 16
    with pytest.raises(StorageError, match="Document exceeds"):
        storage.write_workspace_document(path, b"x" * 17)
    assert stored.absolute_path.read_bytes() == b"x" * 16
    with pytest.raises(StorageError, match="Artifact exceeds"):
        storage.write_bytes_atomic(settings.workspace_dir, "other.bin", b"x" * 16)
    with pytest.raises(StorageError, match="Unsupported file type"):
        storage.write_workspace_document("other.bin", b"x" * 16)
    with pytest.raises(StorageError, match="escapes"):
        storage.write_workspace_document("../outside.pdf", b"x" * 16)
    assert not list(settings.workspace_dir.rglob("*.tmp"))


def test_workspace_read_write_and_escape_protection(test_settings) -> None:
    storage = SafeStorage(test_settings)

    stored = storage.write_workspace_file("notes/example.json", {"hello": "world"})
    media_type, content = storage.read_workspace_file("notes/example.json")

    assert stored.relative_path == "notes/example.json"
    assert media_type == "application/json"
    assert content == {"hello": "world"}

    with pytest.raises(StorageError):
        storage.write_workspace_file("../escape.txt", "nope")


def test_workspace_folder_delete_is_recursive_and_protects_metadata(test_settings) -> None:
    storage = SafeStorage(test_settings)
    storage.write_workspace_file("notes/topic/one.md", "# One")
    storage.write_workspace_file("notes/topic/nested/two.md", "# Two")
    storage.write_workspace_file(".scholarweave/tags.json", {})

    storage.delete_workspace_folder("notes/topic")

    assert not (test_settings.workspace_dir / "notes/topic").exists()
    assert (test_settings.workspace_dir / ".scholarweave/tags.json").is_file()
    with pytest.raises(StorageError, match="metadata"):
        storage.delete_workspace_folder(".scholarweave")


def test_list_workspace_markdown_returns_nested_notes_only(test_settings) -> None:
    storage = SafeStorage(test_settings)
    storage.write_workspace_file("root.md", "# Root")
    storage.write_workspace_file("research/llm/attention.md", "# Attention")
    storage.write_workspace_file("research/data.json", {"ignored": True})

    notes = storage.list_workspace_markdown()

    assert [note.relative_path for note in notes] == ["research/llm/attention.md", "root.md"]
    assert all(note.size_bytes > 0 for note in notes)


def test_list_workspace_files_includes_only_safe_text_formats(test_settings) -> None:
    storage = SafeStorage(test_settings)
    storage.write_workspace_file("notes/readme.md", "# Read me")
    storage.write_workspace_file("data/result.json", {"ok": True})
    storage.write_workspace_file("plain.txt", "hello")
    (test_settings.workspace_dir / "ignored.csv").write_text("a,b", encoding="utf-8")

    assert storage.list_workspace_files() == [
        "data/result.json",
        "notes/readme.md",
        "plain.txt",
    ]


def test_delete_stored_tree_refuses_storage_root(test_settings) -> None:
    storage = SafeStorage(test_settings)

    with pytest.raises(StorageError, match="storage root"):
        storage.delete_stored_tree(test_settings.artifacts_dir, ".")


def test_workspace_targeted_markdown_edits_preserve_tags(test_settings) -> None:
    workspace = WorkspaceService(SafeStorage(test_settings))
    workspace.write_file(
        "papers/paper-1/notes.md",
        "# Notes\n\nOriginal detail.",
        tags=["Paper", "transformers"],
    )

    workspace.replace_markdown(
        "papers/paper-1/notes.md",
        "Original detail.",
        "Corrected detail.",
    )
    document = workspace.append_markdown(
        "papers/paper-1/notes.md",
        "\n## Follow-up\nNew evidence.",
    )

    assert document.content == (
        "# Notes\n\nCorrected detail.\n\n## Follow-up\nNew evidence."
    )
    assert document.tags == ("Paper", "transformers")
    assert [item.path for item in workspace.list_files(tags=["TRANSFORMERS"])] == [
        "papers/paper-1/notes.md"
    ]
    assert ".scholarweave/tags.json" not in [
        item.path for item in workspace.list_files()
    ]


def test_workspace_folder_delete_clears_tags_and_paper_name(test_settings) -> None:
    workspace = WorkspaceService(SafeStorage(test_settings))
    paper = workspace.ensure_paper_folder("paper-1", "A Study")
    workspace.write_file(
        f"{paper['folder']}/nested/evidence.md",
        "# Evidence",
        tags=["evidence"],
    )

    workspace.delete_paper_folder("paper-1")

    assert workspace.list_files() == []
    recreated = workspace.ensure_paper_folder("paper-1", "A New Study")
    assert recreated["paper_name"] == "A New Study"
    assert workspace.list_files(tags=["evidence"]) == []


def test_workspace_service_refuses_to_delete_papers_root(test_settings) -> None:
    workspace = WorkspaceService(SafeStorage(test_settings))
    workspace.ensure_paper_folder("paper-1", "A Study")

    for path in ("library", "library/papers", workspace.paper_folder("paper-1")):
        with pytest.raises(ValidationError, match="Managed research"):
            workspace.delete_folder(path)

    assert len(workspace.list_files()) == 2


def test_recreating_paper_notes_is_serialized_and_preserves_external_content(test_settings) -> None:
    storage = SafeStorage(test_settings)
    workspace = WorkspaceService(storage)
    paper = workspace.ensure_paper_folder("restore-notes", "Restore notes")
    workspace.delete_user_file(paper["notes_path"])
    workspace.delete_user_file(paper["summary_path"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        notes = list(pool.map(lambda _: workspace.ensure_paper_notes("restore-notes", "Restore notes"), range(2)))
    assert notes[0].sha256 == notes[1].sha256
    assert notes[0].path == notes[1].path == paper["notes_path"]
    assert not (test_settings.workspace_dir / paper["summary_path"]).exists()

    storage.write_workspace_file(paper["notes_path"], "Notes changed outside the application.")
    restored = workspace.ensure_paper_notes("restore-notes", "Restore notes")
    assert restored.content == "Notes changed outside the application."
    assert workspace.search(query="outside", tags=["paper:restore-notes"])[0].path == restored.path


def test_workspace_organization_preserves_note_identity_and_search_on_move(test_settings) -> None:
    storage = SafeStorage(test_settings)
    workspace = WorkspaceService(storage)
    note = workspace.create_note(name="Move me", content="movingneedle", tags=["keep"])

    workspace.organize_file(
        action="move_file", path=note.path, destination="projects/topic/moved.md",
    )

    assert not storage.resolve_path(test_settings.workspace_dir, note.path).exists()
    restarted = WorkspaceService(storage)
    restarted.refresh_index()
    moved = restarted.search(query="movingneedle")[0]
    assert moved.path == "projects/topic/moved.md"
    assert moved.note_id == note.note_id
    assert moved.note_name == note.note_name
    assert moved.kind == "note"
    assert moved.tags == ("note", "keep")


@pytest.mark.parametrize("path", [
    "../escape.md", "/absolute.md", "C:\\outside.md", ".", "knowledge",
    ".scholarweave/config.json", "library/papers/managed--paper-1/notes.md",
    "inbox/attachments/upload/source.md", "knowledge/*.md",
    "knowledge/unindexed.md", "library/../.scholarweave/config.json",
], ids=[
    "escape", "absolute", "windows-drive", "root", "folder", "metadata", "paper",
    "attachment", "wildcard", "unindexed", "metadata-alias",
])
def test_workspace_organization_rejects_unsafe_or_unmanaged_targets(test_settings, path) -> None:
    storage = SafeStorage(test_settings)
    workspace = WorkspaceService(storage)
    note = workspace.create_note(name="Keep", content="safe")
    storage.write_workspace_file(".scholarweave/config.json", {"keep": True})
    storage.write_workspace_file("knowledge/unindexed.md", "not discovered")
    workspace.ensure_paper_folder("paper-1", "Managed")
    workspace.write_file("inbox/attachments/upload/source.md", "attachment")

    with pytest.raises((ValidationError, ValueError, FileNotFoundError)):
        workspace.organize_file(action="delete_file", path=path)
    assert workspace.read_file(note.path).content == note.content
    assert storage.read_workspace_file(".scholarweave/config.json")[1] == {"keep": True}
    assert len(workspace.list_collection("summaries")) == 1


def test_workspace_organization_refuses_overwrite_and_rolls_back_failed_move(test_settings, monkeypatch) -> None:
    storage = SafeStorage(test_settings)
    workspace = WorkspaceService(storage)
    note = workspace.create_note(name="Source", content="original")
    workspace.write_file("projects/occupied.md", "keep")
    with pytest.raises(ValidationError, match="exists"):
        workspace.organize_file(action="move_file", path=note.path, destination="projects/occupied.md")
    assert workspace.read_file("projects/occupied.md").content == "keep"

    def fail_index(*args, **kwargs):
        raise RuntimeError("Index unavailable")

    monkeypatch.setattr(workspace._repository, "upsert", fail_index)
    with pytest.raises(RuntimeError, match="Index unavailable"):
        workspace.organize_file(action="move_file", path=note.path, destination="projects/moved.md")
    assert workspace.read_file(note.path).content == note.content
    assert not storage.resolve_path(test_settings.workspace_dir, "projects/moved.md").exists()
    assert workspace.search(query="original")[0].path == note.path


@pytest.mark.parametrize("destination", [
    "../escaped.md", ".scholarweave/moved.md", "library/papers/managed--paper-1/notes.md",
    "inbox/attachments/upload/moved.md", "projects/moved.json", "projects/../moved.md",
], ids=["escape", "metadata", "paper", "attachment", "extension", "traversal"])
def test_workspace_organization_rejects_invalid_move_destinations(test_settings, destination) -> None:
    workspace = WorkspaceService(SafeStorage(test_settings))
    note = workspace.create_note(name="Keep", content="original")
    with pytest.raises(ValidationError):
        workspace.organize_file(action="move_file", path=note.path, destination=destination)
    assert workspace.read_file(note.path).content == note.content
    assert workspace.search(query="original")[0].path == note.path


def test_workspace_paper_folder_is_canonical_and_non_destructive(test_settings) -> None:
    workspace = WorkspaceService(SafeStorage(test_settings))

    first = workspace.ensure_paper_folder("paper-1", "A Study")
    workspace.append_markdown(first["notes_path"], "\nExisting note.")
    second = workspace.ensure_paper_folder("paper-1", "A Study")

    assert first["folder"] == "library/papers/a-study--paper-1"
    assert first["created"] == [
        "library/papers/a-study--paper-1/summary.md",
        "library/papers/a-study--paper-1/notes.md",
    ]
    assert second["created"] == []
    assert first["paper_name"] == "A Study"
    assert "Existing note." in workspace.read_file(first["notes_path"]).content
    assert workspace.read_file(first["summary_path"]).tags == (
        "paper",
        "paper:paper-1",
        "summary",
    )

    renamed = workspace.set_paper_name("paper-1", "A Better Display Name")

    assert renamed["paper_name"] == "A Better Display Name"
    assert renamed["folder"] == first["folder"]
    restarted = WorkspaceService(SafeStorage(test_settings))
    assert restarted.ensure_paper_folder("paper-1", "Changed Title")["folder"] == first["folder"]
    assert workspace.read_file(first["summary_path"]).paper_name == (
        "A Better Display Name"
    )


def test_generic_notes_use_server_generated_uuid_and_are_searchable(test_settings) -> None:
    workspace = WorkspaceService(SafeStorage(test_settings))

    note = workspace.create_note(
        name="KV cache experiments",
        content="Compare paged attention with a contiguous cache.",
        tags=["inference"],
    )

    assert note.note_id is not None
    assert uuid.UUID(note.note_id)
    assert note.path == f"knowledge/kv-cache-experiments--{note.note_id}.md"
    assert note.note_name == "KV cache experiments"
    assert note.kind == "note"
    assert "# KV cache experiments" in note.content
    assert [item.path for item in workspace.search(query="paged attention")] == [
        note.path
    ]
    assert [item.path for item in workspace.search(kinds=["note"], tags=["INFERENCE"])] == [
        note.path
    ]


def test_workspace_bm25_ranks_relevance_before_recency_and_returns_excerpts(test_settings) -> None:
    workspace = WorkspaceService(SafeStorage(test_settings))
    best = workspace.create_note(name="Quasar spectroscopy", content="quasar spectra", tags=["Astro"])
    workspace.write_file("notes/recent.md", "quasar " + "unrelated " * 300)
    workspace.write_file("notes/other.md", "spectroscopy")

    hits = workspace.search(query="quasar spectroscopy")

    assert hits[0].path == best.path
    assert len(hits) == 3  # Natural-language retrieval matches any word, not an exact phrase.
    assert all(hit.score is not None and hit.score > 0 for hit in hits)
    assert [hit.score for hit in hits] == sorted((hit.score for hit in hits), reverse=True)
    assert "quasar" in hits[0].excerpt.lower()
    assert len(hits[1].excerpt) < 400
    assert workspace.search(query="quasar", tags=["astro"])[0].path == best.path
    assert workspace.search(query="quasar spectroscopy", limit=1, offset=1)[0].path == hits[1].path
    assert workspace.search(query="QUASAR QUASAR")[0].score == workspace.search(query="quasar")[0].score
    assert workspace.search(query="qua") == []
    assert workspace.search(query='""" ** ()') == []


@pytest.mark.parametrize("word", ["Stra\u00dfe", "wei\u00df", "\ufb02ow", "caf\u00e9"])
def test_workspace_search_uses_the_same_unicode_tokenizer_as_the_index(test_settings, word) -> None:
    workspace = WorkspaceService(SafeStorage(test_settings))
    workspace.write_file("notes/unicode.md", word)
    workspace.write_file("notes/tagged.md", "Different content", tags=[word])
    expected = {"notes/unicode.md", "notes/tagged.md"}
    assert {hit.path for hit in workspace.search(query=word)} == expected
    workspace.refresh_index()
    assert {hit.path for hit in workspace.search(query=word)} == expected


def test_workspace_discovery_does_not_scan_or_read_files(test_settings, monkeypatch) -> None:
    storage = SafeStorage(test_settings)
    workspace = WorkspaceService(storage)
    workspace.write_file("notes/indexed.md", "cachedneedle")

    def unexpected_disk_read(*args):
        pytest.fail("Discovery must query the persistent index, not scan workspace files.")

    monkeypatch.setattr(storage, "list_workspace_files", unexpected_disk_read)
    monkeypatch.setattr(storage, "read_workspace_file", unexpected_disk_read)
    assert workspace.search(query="cachedneedle")[0].path == "notes/indexed.md"
    assert workspace.list_collection("files")[0].path == "notes/indexed.md"
    assert workspace.index_status()["indexed_files"] == 1


def test_workspace_collections_and_index_updates_survive_restart(test_settings) -> None:
    storage = SafeStorage(test_settings)
    workspace = WorkspaceService(storage)
    note = workspace.create_note(name="Attention", content="obsolete", tags=["keep"])
    paper = workspace.ensure_paper_folder("paper-1", "Named paper")
    workspace.write_file("knowledge/imported.md", "imported")
    workspace.write_file("data.json", {"finding": "jsonneedle"})

    assert {item.path for item in workspace.list_collection("notes")} == {
        note.path, "knowledge/imported.md", paper["notes_path"],
    }
    assert [item.path for item in workspace.list_collection("summaries")] == [paper["summary_path"]]
    assert workspace.search(query="jsonneedle")[0].path == "data.json"
    workspace.write_file(note.path, "replacement")
    assert workspace.search(query="obsolete") == []
    assert workspace.search(query="replacement")[0].tags == ("note", "keep")
    workspace = WorkspaceService(storage)
    assert workspace.search(query="replacement")[0].note_name == "Attention"
    workspace.delete_file(note.path)
    workspace.delete_paper_folder("paper-1")
    assert workspace.search(query="replacement") == []
    assert workspace.list_collection("summaries") == []


def test_workspace_refresh_reconciles_external_edits_and_preserves_metadata(test_settings) -> None:
    storage = SafeStorage(test_settings)
    workspace = WorkspaceService(storage)
    note = workspace.create_note(name="Saved display name", content="beforeedit", tags=["keep"])
    paper = workspace.ensure_paper_folder("paper-1", "Paper title")
    storage.write_workspace_file(note.path, "afteredit")
    storage.delete_workspace_file(paper["notes_path"])
    storage.write_workspace_file("knowledge/external.md", "externalneedle")
    storage.write_workspace_file(".scholarweave/private.json", {"hidden": "privateneedle"})
    before = workspace.index_status()

    result = workspace.refresh_index()

    assert result == {**before, "removed_files": 1}
    assert workspace.search(query="beforeedit") == []
    hit = workspace.search(query="afteredit")[0]
    assert (hit.note_id, hit.note_name, hit.tags) == (note.note_id, note.note_name, note.tags)
    assert workspace.search(query="externalneedle")[0].kind == "note"
    assert workspace.search(query="privateneedle") == []
    assert workspace.list_collection("summaries")[0].paper_name == "Paper title"
    assert WorkspaceService(storage).search(query="afteredit")[0].path == note.path
    assert workspace.refresh_index()["removed_files"] == 0


def test_failed_refresh_keeps_previous_search_index(test_settings, monkeypatch) -> None:
    storage = SafeStorage(test_settings)
    workspace = WorkspaceService(storage)
    workspace.write_file("notes/saved.md", "originalneedle")
    storage.write_workspace_file("notes/saved.md", "changedneedle")
    storage.write_workspace_file("notes/unreadable.md", "bad")
    read = storage.read_workspace_file

    def fail_one(path):
        if path == "notes/unreadable.md":
            raise StorageError("Cannot read source")
        return read(path)

    monkeypatch.setattr(storage, "read_workspace_file", fail_one)
    with pytest.raises(StorageError, match="Cannot read"):
        workspace.refresh_index()
    assert workspace.search(query="originalneedle")[0].path == "notes/saved.md"
    assert workspace.search(query="changedneedle") == []


@pytest.mark.parametrize("arguments", [
    {"query": " "}, {"query": "x" * 2001}, {"kinds": ["unknown"]},
])
def test_workspace_rejects_invalid_search_inputs(test_settings, arguments) -> None:
    from backend.core.errors import ValidationError

    with pytest.raises(ValidationError):
        WorkspaceService(SafeStorage(test_settings)).search(**arguments)
