from __future__ import annotations

import asyncio
import hashlib
from urllib.parse import quote

import httpx
import pytest

pytest.importorskip("textual")

from textual.widgets import Button, Input, Markdown, OptionList, Static, TextArea
from textual.worker import WorkerCancelled

from backend.app import create_app
from backend.persistence.files import SafeStorage
from backend.tests.test_tui_app import CockpitServer
from scholarweave_tui.client import ScholarWeaveClient
from scholarweave_tui.app import ScholarWeaveApp
from scholarweave_tui.widgets import ChatMessage, ConfirmDiscard


@pytest.fixture(autouse=True)
def isolated_preferences(tmp_path, monkeypatch):
    monkeypatch.setenv("SCHOLARWEAVE_DATA_DIR", str(tmp_path))


async def settle(app, pilot) -> None:
    await pilot.pause()
    while app.workers:
        try:
            await app.workers.wait_for_complete()
        except WorkerCancelled:
            pass
        await pilot.pause()


class NotesServer(CockpitServer):
    def __init__(self) -> None:
        super().__init__()
        self.note_requests: list[httpx.Request] = []
        self.records = [
            {
                **self.note,
                "path": f"knowledge/idea-{index:03}.md",
                "name": f"idea-{index:03}.md",
                "note_name": f"Insight {index:03}",
                "content": f"Evidence {index}",
                "kind": "paper_notes" if index == 0 else "note",
                "tags": ["physics", "reviewed"] if index % 2 == 0 else ["physics"],
            }
            for index in range(126)
        ]

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api")
        if path in {"/workspace/notes", "/workspace/search"}:
            self.note_requests.append(request)
            query = request.url.params.get("query", "").casefold()
            tags = request.url.params.get_list("tags")
            records = [
                item for item in self.records
                if (not query or query in f'{item["note_name"]} {item["content"]}'.casefold())
                and all(tag in item["tags"] for tag in tags)
            ]
            offset, limit = int(request.url.params.get("offset", 0)), int(request.url.params.get("limit", 50))
            return httpx.Response(200, json=[
                {key: value for key, value in item.items() if key != "content"}
                for item in records[offset:offset + limit]
            ])
        if path == "/workspace/files/content" and request.method == "GET":
            self.note_requests.append(request)
            item = next((item for item in self.records if item["path"] == request.url.params["path"]), None)
            if item is None:
                return httpx.Response(404, json={"detail": "Note not found"})
            return httpx.Response(200, json={
                **item, "sha256": hashlib.sha256(item["content"].encode()).hexdigest(),
            })
        if path == "/workspace/index":
            self.note_requests.append(request)
            return httpx.Response(200, json={"engine": "sqlite-fts5-bm25", "indexed_files": len(self.records)})
        return super().handle(request)


def test_notes_catalog_pages_past_one_hundred_and_filters_tags() -> None:
    async def scenario() -> None:
        server = NotesServer()
        app = server.app()
        async with app.run_test(size=(140, 42)) as pilot:
            await settle(app, pilot)
            assert len(server.note_requests) == 1
            assert server.note_requests[0].url.params["limit"] == "25"
            app.action_view("notes")
            await settle(app, pilot)
            assert app.query_one("#catalog", OptionList).option_count == 25
            assert "Paper note" in str(app.query_one("#catalog", OptionList).get_option_at_index(0).prompt)
            assert "Standalone note" in str(app.query_one("#catalog", OptionList).get_option_at_index(1).prompt)
            for _ in range(5):
                app.change_notes_page(1)
                await settle(app, pilot)
            assert app._entries == ["knowledge/idea-125.md"]
            assert app.query_one("#notes-next", Button).disabled
            app.open_entry(app._entries[0])
            await settle(app, pilot)
            assert app.note.path == "knowledge/idea-125.md"
            app.query_one("#search", Input).value = "Evidence"
            app.query_one("#notes-tags", Input).value = "physics, reviewed"
            await settle(app, pilot)
            assert app._notes_offset == 0
            request = server.note_requests[-1]
            assert request.url.params.get_list("tags") == ["physics", "reviewed"]
            assert request.url.params.get_list("kinds") == ["note", "paper_notes"]
            assert all(int(path.split("-")[-1].removesuffix(".md")) % 2 == 0 for path in app._entries)
            app.query_one("#search", Input).value = "missing"
            await settle(app, pilot)
            assert not app._entries
            app.query_one("#search", Input).value = ""
            app.query_one("#notes-tags", Input).value = ""
            await settle(app, pilot)
            assert len(app._entries) == 25
            assert app._notes_offset == 0
            assert all(int(request.url.params.get("limit", 25)) == 25 for request in server.note_requests)

    asyncio.run(asyncio.wait_for(scenario(), timeout=30))


def test_note_links_preserve_dirty_drafts_and_open_exact_paths_internally(monkeypatch) -> None:
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda *args, **kwargs: opened.append(args))

    async def scenario() -> None:
        server = NotesServer()
        target = "library/papers/Paper & questions #1/notes+100%25.md"
        server.records[1]["path"] = target
        server.records[1]["kind"] = "paper_notes"
        app = server.app()
        async with app.run_test(size=(140, 42)) as pilot:
            await settle(app, pilot)
            app.action_view("notes")
            app.open_entry(server.records[0]["path"])
            await settle(app, pilot)
            app.edit_note()
            app.query_one("#note-editor", TextArea).load_text("Keep my unsaved draft")
            await pilot.pause()
            href = "/library/notes?path=" + quote(target, safe="")
            preview = app.query_one("#note-preview", Markdown)
            preview.post_message(Markdown.LinkClicked(preview, href))
            await pilot.pause()
            assert isinstance(app.screen, ConfirmDiscard)
            await pilot.press("escape")
            await settle(app, pilot)
            assert app.query_one("#note-editor", TextArea).text == "Keep my unsaved draft"
            assert app.note.path == server.records[0]["path"]
            preview.post_message(Markdown.LinkClicked(preview, href))
            await pilot.pause()
            await pilot.click("#discard")
            await settle(app, pilot)
            assert app.note.path == target
            assert not app.note_dirty
            assert not opened
            app.action_view("chat")
            message = ChatMessage("assistant", f"[Open canonical note]({href})")
            await app.query_one("#transcript").mount(message)
            markdown = message.query_one(Markdown)
            markdown.post_message(Markdown.LinkClicked(markdown, href))
            await settle(app, pilot)
            assert app.view == "notes"
            assert app.note.path == target
            assert not opened
            assert any(
                request.url.params.get("path") == target
                for request in server.note_requests if request.url.path.endswith("/content")
            )
            app.action_view("skills")
            app.open_entry("web-synthesis")
            await settle(app, pilot)
            app.edit_skill()
            app.query_one("#skill-editor", TextArea).load_text("Keep my skill draft")
            await pilot.pause()
            skill_preview = app.query_one("#skill-preview", Markdown)
            skill_preview.post_message(Markdown.LinkClicked(skill_preview, href))
            await pilot.pause()
            assert isinstance(app.screen, ConfirmDiscard)
            await pilot.press("escape")
            await settle(app, pilot)
            assert app.view == "skills"
            assert app.query_one("#skill-editor", TextArea).text == "Keep my skill draft"
            assert not opened

    asyncio.run(asyncio.wait_for(scenario(), timeout=30))


def test_refresh_index_retains_draft_and_discuss_only_prepares_chat() -> None:
    async def scenario() -> None:
        server = NotesServer()
        app = server.app()
        async with app.run_test(size=(140, 42)) as pilot:
            await settle(app, pilot)
            app.action_view("notes")
            app.open_entry(server.records[0]["path"])
            await settle(app, pilot)
            app.edit_note()
            app.query_one("#note-editor", TextArea).load_text("Unsaved supporting evidence")
            await pilot.pause()
            assert app.query_one("#discuss-note", Button).disabled
            app.refresh_notes_index()
            await settle(app, pilot)
            assert app.query_one("#note-editor", TextArea).text == "Unsaved supporting evidence"
            assert app.note_dirty
            assert any(request.method == "POST" and request.url.path.endswith("/index") for request in server.note_requests)
            app.query_one("#note-editor", TextArea).load_text(app.note.content)
            await pilot.pause()
            assert not app.query_one("#discuss-note", Button).disabled
            app.discuss_note()
            await settle(app, pilot)
            assert app.view == "chat"
            draft = app.query_one("#composer", TextArea).text
            assert server.records[0]["path"] in draft
            assert "Do not save, create, or overwrite" in draft
            assert not any(path.endswith("/messages") or method == "PUT" for method, path, _ in server.requests)

    asyncio.run(asyncio.wait_for(scenario(), timeout=30))


def test_real_notes_pagination_tag_conjunction_and_external_refresh(test_settings) -> None:
    async def scenario() -> None:
        backend = create_app(test_settings)
        transport = httpx.ASGITransport(app=backend)
        async with backend.router.lifespan_context(backend):
            expected = set()
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as setup:
                for index in range(31):
                    tags = ["physics", "reviewed"] if index % 2 == 0 else ["physics"]
                    response = await setup.post("/api/workspace/files/notes", json={
                        "name": f"Evidence {index}", "content": f"linkedresearch {index}", "tags": tags,
                    })
                    assert response.status_code == 200, response.text
                    if index % 2 == 0:
                        expected.add(response.json()["path"])
            client = ScholarWeaveClient(transport=transport)
            try:
                first = await client.notes_page()
                second = await client.notes_page(offset=25)
                assert len(first) == 25 and len(second) == 6
                assert not {item.path for item in first} & {item.path for item in second}
                exact = await client.read_note(second[0].path)
                assert exact.path == second[0].path
                matches = await client.notes_page(query="linkedresearch", tags=["physics", "reviewed"])
                assert {item.path for item in matches} == expected
                SafeStorage(test_settings).write_text(
                    test_settings.workspace_dir, exact.path, "externalrefreshneedle",
                )
                assert not await client.notes_page(query="externalrefreshneedle")
                status = await client.refresh_notes_index()
                assert status.indexed_files >= 31
                refreshed = await client.notes_page(query="externalrefreshneedle")
                assert [item.path for item in refreshed] == [exact.path]
                assert refreshed[0].tags == exact.tags
            finally:
                await client.close()

    asyncio.run(scenario())


def test_initial_reload_cannot_replace_a_newer_note_search_page() -> None:
    async def scenario() -> None:
        server = NotesServer()
        initial_started, release_initial = asyncio.Event(), asyncio.Event()

        async def handle(request: httpx.Request) -> httpx.Response:
            response = server.handle(request)
            if request.url.path.endswith("/workspace/notes") and not initial_started.is_set():
                initial_started.set()
                await release_initial.wait()
            return response

        app = ScholarWeaveApp(ScholarWeaveClient(transport=httpx.MockTransport(handle)))
        async with app.run_test(size=(140, 42)) as pilot:
            try:
                await initial_started.wait()
                server.records[0]["note_name"] = "Fresh title"
                app.action_view("notes")
                app.query_one("#search", Input).value = "Fresh"
                async with asyncio.timeout(10):
                    while len(app.notes) != 1:
                        await pilot.pause()
                app.query_one("#search", Input).value = ""
                async with asyncio.timeout(10):
                    while len(app.notes) != 25:
                        await pilot.pause()
                assert app.notes[0].note_name == "Fresh title"
            finally:
                release_initial.set()
            await settle(app, pilot)
            assert app.notes[0].note_name == "Fresh title"
            assert app._notes_query == ""

    asyncio.run(asyncio.wait_for(scenario(), timeout=30))
