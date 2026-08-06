from __future__ import annotations

import pytest

from backend.persistence.files import SafeStorage, StorageError


def test_workspace_read_write_and_escape_protection(test_settings) -> None:
    storage = SafeStorage(test_settings)

    stored = storage.write_workspace_file("notes/example.json", {"hello": "world"})
    media_type, content = storage.read_workspace_file("notes/example.json")

    assert stored.relative_path == "notes/example.json"
    assert media_type == "application/json"
    assert content == {"hello": "world"}

    with pytest.raises(StorageError):
        storage.write_workspace_file("../escape.txt", "nope")


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
