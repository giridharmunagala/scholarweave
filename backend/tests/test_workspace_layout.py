from __future__ import annotations

import pytest

from backend.workspace.layout import WorkspaceLayout


@pytest.mark.parametrize("title, slug", [
    ("KV Cache: Experiments / Results?", "kv-cache-experiments-results"),
    ("CON", "con"),
    ("caf\u00e9", "cafe"),
    ("\u7814\u7a76", "untitled"),
    ("...", "untitled"),
    ("x" * 200, "x" * 48),
])
def test_readable_components_are_bounded_and_windows_safe(title, slug):
    component = WorkspaceLayout.named_component(title, "paper-1")
    assert component == f"{slug}--paper-1"
    assert WorkspaceLayout.paper_folder("paper-1", title) == f"library/papers/{component}"
    assert WorkspaceLayout.knowledge_note("paper-1", title) == f"knowledge/{component}.md"


@pytest.mark.parametrize("identifier", ["", "../escape", "C:\\root", "a/b", ".", "a" * 65])
def test_layout_rejects_unsafe_identifiers(identifier):
    with pytest.raises(ValueError, match="identifier"):
        WorkspaceLayout.paper_folder(identifier, "Paper")


def test_paper_identity_and_canonical_files_are_independent_of_title():
    folder = WorkspaceLayout.paper_folder("paper-1", "A Study")
    assert WorkspaceLayout.paper_id(f"{folder}/notes.md") == "paper-1"
    assert WorkspaceLayout.kind(f"{folder}/notes.md") == "paper_notes"
    assert WorkspaceLayout.kind(f"{folder}/summary.md") == "paper_summary"
    assert WorkspaceLayout.kind(f"{folder}/summaries/summary.md") == "paper_file"
    assert WorkspaceLayout.kind("knowledge/a-study--note-1.md") == "note"
    assert WorkspaceLayout.paper_id("library/papers/unmanaged/notes.md") is None
    assert WorkspaceLayout.paper_id("papers/paper-1/notes.md") is None