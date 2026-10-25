from __future__ import annotations

import uuid

import httpx
import pytest

from backend.app import create_app
from backend.core.errors import ConflictError, NotFoundError, ValidationError
from backend.persistence.files import SafeStorage
from backend.workspace.highlights import highlights
from backend.workspace.service import WorkspaceService


def capture(text: str, page: int = 2, identifier: str | None = None) -> str:
    marker = f"<!-- scholarweave-highlight:{identifier} -->\n" if identifier else ""
    return f"{marker}> {text}\n>\n> \u2014 p. {page}\n"


def notes(workspace, paper_id, content):
    folder = workspace.ensure_paper_folder(paper_id, f"Paper {paper_id}")
    workspace.write_file(folder["notes_path"], f"# Personal notes\n\n{content}")
    return folder


def test_reader_capture_parser_preserves_legacy_ids_and_ignores_generated_prose():
    old = capture("Earlier quote")
    first = highlights("notes.md", old)[0]
    assert highlights("notes.md", f"# Changed heading\n\n{old}\nOther annotation")[0] == first
    identifier = str(uuid.uuid4())
    parsed = highlights("notes.md", capture("Line one\n> Line two", identifier=identifier))
    assert parsed[0].id == identifier
    assert parsed[0].text == "Line one\nLine two"
    assert highlights("notes.md", capture("Older first line\nUnprefixed second line"))[0].text == "Older first line\nUnprefixed second line"
    assert highlights("notes.md", "> Generated discussion without a reader page attribution\n") == []
    assert len(highlights("notes.md", old + "\n" + old)) == 2
    with pytest.raises(ValidationError, match="Duplicate"):
        highlights("notes.md", capture("A", identifier=identifier) + "\n" + capture("B", identifier=identifier))


def test_highlight_connections_are_bidirectional_searchable_and_revision_guarded(test_settings):
    workspace = WorkspaceService(SafeStorage(test_settings))
    first = notes(workspace, "p1", capture("Quasar acceleration"))
    second = notes(workspace, "p2", capture("Distant stellar evidence"))
    notes(workspace, "p3", capture("Unrelated observation"))
    workspace.write_file(first["summary_path"], capture("Generated quasar summary"))
    source = workspace.reader_notes(paper_ids=["p1"])["items"][0]
    target = workspace.reader_notes(paper_ids=["p2"])["items"][0]
    original = workspace.read_file(second["notes_path"])
    connected = workspace.connect_reader_notes(source["id"], target["id"], source["sha256"])
    assert capture("Quasar acceleration") in connected.content
    assert workspace.read_file(second["notes_path"]).content == original.content
    assert workspace.reader_notes(paper_ids=["p2"])["items"][0]["connection_ids"] == [source["id"]]
    hits = workspace.reader_notes(query="quasar")["items"]
    assert [(item["paper_id"], item["match_type"]) for item in hits] == [("p1", "direct"), ("p2", "connected")]
    assert hits[1]["connected_via"] == [source["id"]]
    assert [item["paper_id"] for item in workspace.reader_notes(query="stellar")["items"]] == ["p2", "p1"]
    assert workspace.reader_notes(query="quasar", paper_ids=["p3"])["total"] == 0
    assert workspace.reader_notes(connected_to=source["id"])["items"][0]["id"] == target["id"]
    again = workspace.connect_reader_notes(source["id"], target["id"], connected.sha256)
    assert again.sha256 == connected.sha256
    with pytest.raises(ConflictError):
        workspace.connect_reader_notes(source["id"], target["id"], source["sha256"])
    with pytest.raises(ValidationError):
        workspace.connect_reader_notes(source["id"], source["id"], connected.sha256)
    with pytest.raises(NotFoundError):
        workspace.connect_reader_notes(source["id"], "missing", connected.sha256)
    restored = WorkspaceService(SafeStorage(test_settings))
    assert restored.reader_notes(query="quasar") == workspace.reader_notes(query="quasar")
    results = restored.search(query="quasar", kinds=["paper_notes"], limit=1)
    assert [item.path for item in results] == [first["notes_path"]]
    related = restored.search(query="quasar", kinds=["paper_notes"], limit=1, offset=1)
    assert related[0].path == second["notes_path"]
    assert related[0].excerpt.startswith("Connected highlight:")
    assert restored.search(query="quasar", kinds=["paper_notes"], tags=["paper:p3"]) == []
    assert restored.reader_notes(note_id=source["id"])["items"][0]["id"] == source["id"]
    with pytest.raises(NotFoundError):
        restored.reader_notes(note_id="missing")


def test_connections_do_not_expand_recursively_and_refresh_reconciles_external_edits(test_settings):
    storage = SafeStorage(test_settings)
    workspace = WorkspaceService(storage)
    for index, text in enumerate(("Original quasar", "Linked observation", "Further evidence")):
        notes(workspace, f"p{index}", capture(text))
    a = workspace.reader_notes(paper_ids=["p0"])["items"][0]
    b = workspace.reader_notes(paper_ids=["p1"])["items"][0]
    c = workspace.reader_notes(paper_ids=["p2"])["items"][0]
    workspace.connect_reader_notes(a["id"], b["id"], a["sha256"])
    workspace.connect_reader_notes(b["id"], c["id"], b["sha256"])
    assert {item["id"] for item in workspace.reader_notes(query="quasar")["items"]} == {a["id"], b["id"]}
    current = workspace.read_file(c["path"])
    storage.write_workspace_file(c["path"], current.content + "\nExternal note")
    with pytest.raises(ConflictError):
        workspace.connect_reader_notes(c["id"], a["id"], c["sha256"])
    workspace.refresh_index()
    refreshed = workspace.reader_notes(paper_ids=["p2"])["items"][0]
    workspace.connect_reader_notes(c["id"], a["id"], refreshed["sha256"])
    workspace.write_file(b["path"], "# Removed highlight\n")
    assert workspace.reader_notes(connected_to=a["id"])["items"][0]["id"] == c["id"]
    assert workspace.reader_notes(connected_to=a["id"])["total"] == 1


def test_reader_highlight_pagination_and_search_reach_notes_beyond_the_first_page(test_settings):
    workspace = WorkspaceService(SafeStorage(test_settings))
    notes(workspace, "p1", "\n".join(capture(f"Observation {index}", page=index) for index in range(1, 27)))
    first = workspace.reader_notes(limit=25)
    assert len(first["items"]) == 25 and first["next_offset"] == 25
    last = workspace.reader_notes(limit=25, offset=25)
    assert len(last["items"]) == 1 and last["next_offset"] is None
    assert {item["id"] for item in first["items"]}.isdisjoint({last["items"][0]["id"]})
    notes(workspace, "p2", capture("Independent counterpart"))
    target = workspace.reader_notes(paper_ids=["p2"])["items"][0]
    source = last["items"][0]
    workspace.connect_reader_notes(source["id"], target["id"], source["sha256"])
    assert [item["id"] for item in workspace.reader_notes(query="26")["items"]] == [source["id"], target["id"]]


@pytest.mark.anyio
async def test_tracks_reuse_linked_papers_personal_highlights_without_copying(test_settings):
    app = create_app(test_settings)
    services = app.state.services
    try:
        document = services.documents.create_document_from_bytes(b"%PDF-1.4\n%%EOF", filename="paper.pdf", title="Paper")
        folder = notes(services.workspace, document.id, capture("Researcher's highlighted passage"))
        services.workspace.write_file(folder["summary_path"], "Generated summary")
        first, second = services.tracks.create("First"), services.tracks.create("Second")
        assert services.tracks.reader_notes(first.id)["total"] == 0
        services.tracks.add_paper(first.id, document.id)
        services.tracks.add_paper(second.id, document.id)
        assert services.tracks.reader_notes(first.id) == services.tracks.reader_notes(second.id)
        assert services.tracks.reader_notes(first.id)["items"][0]["path"] == folder["notes_path"]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as http:
            response = await http.get(f"/api/tracks/{first.id}/reader-notes")
            assert response.status_code == 200
            source = response.json()["items"][0]
            response = await http.get("/api/workspace/reader-notes", params={"paper_id": document.id, "note_id": source["id"]})
            assert response.json()["items"] == [source]
            other = services.documents.create_document_from_bytes(b"%PDF-1.4\nOther\n%%EOF", filename="other.pdf", title="Other")
            notes(services.workspace, other.id, capture("Connected observation"))
            target = services.workspace.reader_notes(paper_ids=[other.id])["items"][0]
            linked = await http.post("/api/workspace/reader-notes/connections", json={
                "source_id": source["id"], "target_id": target["id"], "expected_sha256": source["sha256"],
            })
            assert linked.status_code == 200, linked.text
            response = await http.get("/api/workspace/search", params={"query": "highlighted", "kinds": "paper_notes"})
            assert [row["paper_id"] for row in response.json()] == [document.id, other.id]
            assert response.json()[1]["excerpt"].startswith("Connected highlight:")
            response = await http.get(f"/api/tracks/{first.id}/reader-notes", params={"query": "highlighted"})
            assert response.json()["total"] == 1
        services.tracks.remove_paper(first.id, document.id)
        assert services.tracks.reader_notes(first.id)["total"] == 0
        assert services.tracks.reader_notes(second.id)["total"] == 1
        assert "Researcher's highlighted passage" in services.workspace.read_file(folder["notes_path"]).content
    finally:
        await services.close()
