from __future__ import annotations

import uuid

import pytest

from backend.persistence.files import SafeStorage, StorageError
from backend.workspace.service import WorkspaceService


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

    workspace.delete_folder(paper["folder"])

    assert workspace.list_files() == []
    recreated = workspace.ensure_paper_folder("paper-1", "A New Study")
    assert recreated["paper_name"] == "A New Study"
    assert workspace.list_files(tags=["evidence"]) == []


def test_workspace_service_refuses_to_delete_papers_root(test_settings) -> None:
    workspace = WorkspaceService(SafeStorage(test_settings))
    workspace.ensure_paper_folder("paper-1", "A Study")

    with pytest.raises(ValueError, match="papers folder"):
        workspace.delete_folder("papers")

    assert len(workspace.list_files()) == 2


def test_workspace_paper_folder_is_canonical_and_non_destructive(test_settings) -> None:
    workspace = WorkspaceService(SafeStorage(test_settings))

    first = workspace.ensure_paper_folder("paper-1", "A Study")
    workspace.append_markdown(first["notes_path"], "\nExisting note.")
    second = workspace.ensure_paper_folder("paper-1", "A Study")

    assert first["folder"] == "papers/paper-1"
    assert first["created"] == [
        "papers/paper-1/summary.md",
        "papers/paper-1/notes.md",
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
    assert note.path == f"notes/{note.note_id}/note.md"
    assert note.note_name == "KV cache experiments"
    assert note.kind == "note"
    assert "# KV cache experiments" in note.content
    assert [item.path for item in workspace.search(query="paged attention")] == [
        note.path
    ]
    assert [item.path for item in workspace.search(kinds=["note"], tags=["INFERENCE"])] == [
        note.path
    ]
