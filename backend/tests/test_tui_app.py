from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

pytest.importorskip("textual")

from textual.widgets import Button, ContentSwitcher, Input, Markdown, OptionList, Select, Static, TextArea

from backend.core.config import Settings as CoreSettings
from backend.core.settings_service import SettingsResponse
from backend.runs.schemas import RunResponse
from scholarweave_tui.app import ScholarWeaveApp
from scholarweave_tui.client import ScholarWeaveClient
from scholarweave_tui.preferences import Preferences
from scholarweave_tui.stream import LiveRun
from scholarweave_tui.themes import DEFAULT_THEME, THEMES
from scholarweave_tui.widgets import (
    ChatMessage,
    ChoicePicker,
    ConfirmDiscard,
    ModelConfig,
    NoteName,
    ProviderForm,
    Spinner,
    ThemePicker,
    TranscriptNote,
    Welcome,
)

NOW = datetime.now(UTC).isoformat()


@pytest.fixture(autouse=True)
def local_preferences(tmp_path, monkeypatch):
    """Keep cockpit preferences out of the researcher's real data directory."""

    monkeypatch.setenv("SCHOLARWEAVE_DATA_DIR", str(tmp_path))


def provider_record() -> dict:
    return {
        "id": "provider-1", "name": "Local runtime", "kind": "openai_compatible",
        "base_url": "http://127.0.0.1:11434/v1", "api_key_set": False, "state": "active",
        "models": [
            {"name": "weave-fast", "capabilities": ["chat"], "reasoning_efforts": None,
             "preserve_thinking": False, "context_window_tokens": None, "enabled": True},
            {"name": "weave-deep", "capabilities": ["chat", "tools"],
             "reasoning_efforts": ["low", "high"], "preserve_thinking": True,
             "context_window_tokens": None, "enabled": True},
        ],
        "serialize_model_switches": False, "created_at": NOW, "updated_at": NOW,
    }


def settings_payload(last_reference: dict) -> dict:
    base = CoreSettings()
    values = {
        name: getattr(base, name)
        for name in SettingsResponse.model_fields
        if hasattr(base, name)
    }
    values.update(
        data_dir=str(base.data_dir), workspace_dir=str(base.workspace_dir),
        artifacts_dir=str(base.artifacts_dir), documents_dir=str(base.documents_dir),
        database_path=str(base.database_path), ocr_engine="tesseract",
        default_model_references={"chat": {"provider_profile_id": "provider-1", "model": "weave-fast"}},
        last_chat_model_reference=last_reference,
    )
    return json.loads(SettingsResponse(**values).model_dump_json())


def conversation(identifier: str = "chat-1") -> dict:
    return {
        "id": identifier, "title": "A research thread", "kind": "autonomous",
        "model_reference": {}, "session_policy": {}, "status": "active",
        "last_message_preview": "", "created_at": NOW, "updated_at": NOW,
    }


def run_record(status: str = "completed") -> dict:
    return {
        "id": "run-1", "conversation_id": "chat-1", "agent_name": "Researcher",
        "status": status, "input": "What connects these ideas?",
        "context_window_tokens": 32_768,
        "final_output": "Evidence **connects** the ideas." if status == "completed" else None,
        "last_agent_name": "Researcher", "usage": {
            "input_tokens": 120,
            "output_tokens": 30,
            "performance": {"model_calls": 1, "cached_input_tokens": 40, "cache_reported_calls": 1},
        },
        "error": None, "cancel_requested": False, "created_at": NOW, "started_at": NOW,
        "finished_at": NOW if status == "completed" else None,
        "items": [], "events": [], "epochs": [], "tool_attempts": [], "goal_state": None,
    }


class CockpitServer:
    """HTTP-boundary fixture; no harness or model behavior is mocked."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict]] = []
        self.chats: list[dict] = []
        self.runs: list[dict] = []
        self.offline = False
        self.reject_send = False
        self.providers = [provider_record()]
        self.last_chat_model_reference: dict = {}
        self.note = {
            "path": "knowledge/ideas/notes.md", "name": "notes.md", "note_name": "Connected ideas",
            "media_type": "text/markdown", "size_bytes": 20, "modified_at": NOW,
            "tags": ["research"], "kind": "note", "content": "# A useful connection",
        }
        self.paper = {
            "id": "paper-1", "title": "Evidence and ideas", "source_filename": "evidence.pdf",
            "content_type": "application/pdf", "status": "ready", "page_count": 8,
            "metadata": {}, "created_at": NOW, "updated_at": NOW,
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        path = request.url.path.removeprefix("/api")
        self.requests.append((request.method, path, body))
        if self.offline:
            raise httpx.ConnectError("Server is offline", request=request)
        if path == "/providers":
            if request.method == "POST":
                created = {
                    "id": "provider-2",
                    **body,
                    "api_key_set": bool(body.get("api_key")),
                    "state": "active",
                    "models": body.get("models", []),
                    "serialize_model_switches": body.get("serialize_model_switches") or False,
                    "created_at": NOW,
                    "updated_at": NOW,
                }
                created.pop("api_key", None)
                self.providers.append(created)
                return httpx.Response(201, json=created)
            return httpx.Response(200, json=self.providers)
        if path.startswith("/providers/") and path.endswith("/models"):
            provider_id = path.split("/")[2]
            provider = next(item for item in self.providers if item["id"] == provider_id)
            if not provider["models"]:
                provider["models"] = [{
                    "name": "discovered-chat",
                    "capabilities": ["chat", "tools"],
                    "reasoning_efforts": None,
                    "preserve_thinking": False,
                    "context_window_tokens": None,
                    "enabled": True,
                }]
            return httpx.Response(200, json={
                "models": provider["models"], "discovery_error": None,
            })
        if path.startswith("/providers/") and request.method == "PUT":
            provider_id = path.split("/")[2]
            provider = next(item for item in self.providers if item["id"] == provider_id)
            for key, value in body.items():
                if key == "api_key":
                    provider["api_key_set"] = bool(value)
                else:
                    provider[key] = value
            provider.pop("api_key", None)
            provider["updated_at"] = NOW
            return httpx.Response(200, json=provider)
        if path == "/settings":
            if request.method == "PUT":
                self.last_chat_model_reference = body["last_chat_model_reference"]
            return httpx.Response(200, json=settings_payload(self.last_chat_model_reference))
        if path == "/agent/conversations":
            if request.method == "POST":
                self.chats = [conversation()]
                return httpx.Response(201, json=self.chats[0])
            return httpx.Response(200, json=self.chats)
        if path.endswith("/messages"):
            if self.reject_send:
                return httpx.Response(400, json={"detail": "Configure a default chat model in Settings."})
            self.runs = [run_record()]
            return httpx.Response(202, json={"conversation": conversation(), "run": run_record("running")})
        if path.startswith("/agent/conversations/"):
            return httpx.Response(200, json={**conversation(path.split("/")[-1]), "items": []})
        if path == "/runs":
            return httpx.Response(200, json=self.runs)
        if path.endswith("/events"):
            events = [
                {"sequence": 0, "event_type": "tool.started", "payload": {"tool_name": "search_research_notes"}},
                {"sequence": 1, "event_type": "model.stream", "payload": {
                    "raw_type": "response.output_text.delta", "delta": "Evidence **connects** the ideas.",
                }},
                {"sequence": 2, "event_type": "run.completed", "payload": {}},
            ]
            data = "".join(f"event: {event['event_type']}\ndata: {json.dumps(event)}\n\n" for event in events)
            return httpx.Response(200, text=data, headers={"content-type": "text/event-stream"})
        if path.endswith("/steering"):
            return httpx.Response(202, json={"id": "steer-1", "content": body["content"], "status": "queued"})
        if path.endswith("/cancel"):
            return httpx.Response(200, json=run_record("cancelled"))
        if path == "/runs/run-1":
            return httpx.Response(200, json=run_record())
        if path == "/documents":
            return httpx.Response(200, json=[self.paper])
        if path == "/documents/paper-1":
            return httpx.Response(200, json={
                **self.paper, "artifacts": [], "chunks": [{
                    "id": "chunk-1", "chunk_index": 0, "section_title": "Abstract",
                    "page_start": 1, "page_end": 1, "citation": "[paper-1, p. 1]",
                    "text": "A grounded connection.", "metadata": {},
                }],
            })
        if path in {"/workspace/notes", "/workspace/search"}:
            note = {key: value for key, value in self.note.items() if key != "content"}
            return httpx.Response(200, json=[note])
        if path == "/workspace/files/content":
            if request.method == "PUT":
                self.note["content"] = body["content"]
            return httpx.Response(200, json=self.note)
        if path == "/workspace/files/notes":
            self.note["note_name"] = body["name"]
            self.note["content"] = body.get("content", "")
            return httpx.Response(200, json=self.note)
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    def app(self) -> ScholarWeaveApp:
        return ScholarWeaveApp(ScholarWeaveClient(transport=httpx.MockTransport(self.handle)))


async def settle(app: ScholarWeaveApp, pilot) -> None:
    await pilot.pause()
    while app.workers:
        await app.workers.wait_for_complete()
        await pilot.pause()
    await pilot.pause()


@pytest.mark.parametrize("size", [(150, 46), (100, 32), (80, 24), (60, 24)])
def test_cockpit_layout_and_keyboard_navigation(size) -> None:
    async def scenario() -> None:
        app = CockpitServer().app()
        async with app.run_test(size=size) as pilot:
            await settle(app, pilot)
            assert "CONNECTED" in str(app.query_one("#connection", Static).render())
            assert not app.focus_mode
            assert app.query_one("#sidebar").display == (size[0] >= 90)
            assert not app.query_one("#observatory").display
            await pilot.press("ctrl+f")
            assert app.focus_mode
            assert not app.query_one("#observatory").display
            assert not app.query_one("#sidebar").display
            send = app.query_one("#send", Button).region
            assert send.width >= 9
            assert send.right <= size[0]
            assert send.bottom < size[1]
            assert send.bottom <= app.query_one("#composer-controls").content_region.bottom
            hint = app.query_one("#composer-hint")
            if hint.display:
                assert hint.region.y >= send.bottom
            assert app.query_one("#stop", Button).region.right <= size[0]
            if size[0] in {150, 80}:
                assert app.query_one("#transcript").max_scroll_y == 0
            await pilot.press("ctrl+2")
            assert app.query_one("#pages", ContentSwitcher).current == "paper-pane"
            await pilot.press("ctrl+k")
            assert app.query_one("#sidebar").display
            assert app.focused is app.query_one("#search")
            await pilot.press("ctrl+3")
            assert app.view == "notes"
            was_visible = app.query_one("#observatory").display
            await pilot.press("ctrl+o")
            assert app.query_one("#observatory").display != was_visible
            await pilot.press("ctrl+1")
            assert app.view == "chat"
            if size[0] < 70:
                assert app.query_one("#stage").display
    asyncio.run(scenario())


def test_focus_mode_is_opt_in_and_remembered(tmp_path) -> None:
    async def scenario() -> None:
        preferences = Preferences(path=tmp_path / "tui-preferences.json")
        server = CockpitServer()
        app = ScholarWeaveApp(
            ScholarWeaveClient(transport=httpx.MockTransport(server.handle)), preferences=preferences
        )
        async with app.run_test(size=(150, 46)) as pilot:
            await settle(app, pilot)
            assert not app.has_class("focused")
            assert app.query_one("#sidebar").display
            await pilot.press("ctrl+f")
            await pilot.pause()
            assert app.focus_mode
            assert not app.query_one("#sidebar").display
            assert app.has_class("focused")
            assert Preferences(path=preferences.path).focus_mode is True
    asyncio.run(scenario())


def test_legacy_focus_default_migrates_to_visible_history(tmp_path) -> None:
    path = tmp_path / "tui-preferences.json"
    path.write_text(json.dumps({"theme": "parchment", "focus_mode": True}), encoding="utf-8")

    preferences = Preferences(path=path)

    assert preferences.focus_mode is False
    assert preferences.theme == "parchment"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "theme": "parchment",
        "focus_mode": False,
        "version": 2,
    }


def test_send_stream_and_restore_without_duplicate_messages() -> None:
    async def scenario() -> None:
        server = CockpitServer()
        app = server.app()
        async with app.run_test(size=(140, 42)) as pilot:
            await settle(app, pilot)
            app.query_one("#composer", TextArea).load_text("What connects these ideas?")
            app.run_command("effort", "thorough")
            app.run_command("web", "off")
            app.query_one("#composer", TextArea).load_text("What connects these ideas?")
            await pilot.click("#send")
            await settle(app, pilot)
            assert app.current_id == "chat-1"
            assert app.live.assistant == "Evidence **connects** the ideas."
            assert app.query_one("#composer", TextArea).text == ""
            assert app.effort == "auto"
            sent = next(body for method, path, body in server.requests if path.endswith("/messages"))
            assert sent["response_effort"] == "thorough"
            assert sent["web_enabled"] is False
            assert len(app.query(ChatMessage)) == 2
            app.open_entry("chat-1")
            await settle(app, pilot)
            assert len(app.query(ChatMessage)) == 2
            assert app.run_record.status == "completed"
    asyncio.run(scenario())


@pytest.mark.parametrize("focused", [False, True])
@pytest.mark.parametrize("status", ["completed", "running"])
def test_each_launch_starts_a_fresh_chat_and_refresh_preserves_its_draft(focused, status) -> None:
    async def scenario() -> None:
        server = CockpitServer()
        server.chats = [conversation()]
        server.runs = [run_record(status)]
        for _ in range(2):
            server.requests.clear()
            app = server.app()
            app.focus_mode = focused
            app.preferences.save_focus_mode(focused)
            async with app.run_test(size=(150, 46)) as pilot:
                await settle(app, pilot)
                assert app.current_id is None
                assert app.current_title == "A new thread"
                assert app.run_record is None
                assert not app.live.assistant
                assert len(app.query(Welcome)) == 1
                assert len(app.query(ChatMessage)) == 0
                assert app.query_one("#composer", TextArea).text == ""
                assert app.query_one("#catalog", OptionList).option_count == 1
                assert not any(path == "/runs" or path.endswith("/chat-1") for _, path, _ in server.requests)
                assert not any(method == "POST" for method, _, _ in server.requests)

                app.query_one("#composer", TextArea).load_text("A fresh unsent question")
                app.action_reload()
                await settle(app, pilot)
                assert app.current_id is None
                assert app.query_one("#composer", TextArea).text == "A fresh unsent question"

                await pilot.press("ctrl+k")
                await pilot.click("#catalog", offset=(2, 1))
                await settle(app, pilot)
                assert app.current_id == "chat-1"
                assert len(app.query(ChatMessage)) == 2
                assert not any(path.endswith("/cancel") for _, path, _ in server.requests)

    asyncio.run(scenario())


def test_completed_turn_keeps_progress_usage_and_context_inline() -> None:
    async def scenario() -> None:
        server = CockpitServer()
        server.chats = [conversation()]
        record = run_record()
        record["events"] = [
            {
                "sequence": 0,
                "event_type": "run.started",
                "payload": {},
                "created_at": "2026-09-12T12:00:00Z",
            },
            {
                "sequence": 1,
                "event_type": "context.prepared",
                "payload": {
                    "estimated_input_tokens": 8_000,
                    "context_window_tokens": 32_768,
                },
                "created_at": "2026-09-12T12:00:01Z",
            },
            {
                "sequence": 2,
                "event_type": "tool.started",
                "payload": {"tool_name": "search_research_notes", "tool_call_id": "call-1"},
                "created_at": "2026-09-12T12:00:02Z",
            },
            {
                "sequence": 3,
                "event_type": "tool.completed",
                "payload": {"tool_name": "search_research_notes", "tool_call_id": "call-1"},
                "created_at": "2026-09-12T12:00:04Z",
            },
            {
                "sequence": 4,
                "event_type": "run.completed",
                "payload": {},
                "created_at": "2026-09-12T12:00:05Z",
            },
        ]
        server.runs = [record]
        app = server.app()
        async with app.run_test(size=(150, 46)) as pilot:
            await settle(app, pilot)
            app.open_entry("chat-1")
            await settle(app, pilot)
            assert app.current_id == "chat-1"
            assert app.current_title == "A research thread"
            answer = list(app.query(ChatMessage))[-1]
            metadata = app.query_one(".turn-metadata", TranscriptNote)
            progress = str(metadata.query_one(Static).render())
            usage = str(answer.query_one(".message-usage", Static).render())
            transcript_children = list(app.query_one("#transcript").children)
            question = next(iter(app.query(ChatMessage)))
            assert transcript_children.index(question) < transcript_children.index(metadata)
            assert transcript_children.index(metadata) < transcript_children.index(answer)
            assert "Context prepared" in progress
            assert "Search research notes" in progress
            assert "2s" in progress
            assert "in 120" in usage
            assert "out 30" in usage
            assert "cache 40" in usage
            assert "session 150" in usage
            assert "context 8k / 32.8k" in usage
            assert "left" not in usage

    asyncio.run(scenario())


@pytest.mark.parametrize("focused", [False, True])
def test_short_messages_center_and_long_answers_expand(focused) -> None:
    async def scenario() -> None:
        app = CockpitServer().app()
        async with app.run_test(size=(280, 70)) as pilot:
            await settle(app, pilot)
            if focused:
                app.action_focus_mode()
            transcript = app.query_one("#transcript")
            await transcript.query(Welcome).remove()
            question = ChatMessage("user", "A quick question.")
            short = ChatMessage("assistant", "A concise answer.")
            metadata = TranscriptNote("ACTIVITY\nContext prepared", classes="turn-metadata")
            notice = TranscriptNote("Run stopped.", classes="run-note")
            long = ChatMessage("assistant", "Starting an answer.")
            await transcript.mount(question, metadata, short, notice, long)
            await pilot.pause()
            fixed_bodies = [
                question.query_one(".message-body"),
                metadata.query_one(".note-body"),
                short.query_one(".message-body"),
                notice.query_one(".note-body"),
                app.query_one("#composer-box"),
            ]
            original_geometry = [(body.region.x, body.region.width) for body in fixed_bodies]
            await long.set_content("Long answer. " * 80)
            await pilot.pause()
            assert not short.has_class("long")
            assert long.has_class("long")
            assert short.query_one(".message-body").region.width == 120
            assert long.query_one(".message-body").region.width == 160
            assert [(body.region.x, body.region.width) for body in fixed_bodies] == original_geometry
            assert_transcript_centered(app)

            assert transcript.max_scroll_y == 0
            await long.set_content("\n\n".join(["A longer paragraph. " * 30] * 30))
            metadata.update("ACTIVITY\nContext prepared\nWriting the answer")
            await pilot.pause()
            assert transcript.max_scroll_y > 0
            assert [(body.region.x, body.region.width) for body in fixed_bodies] == original_geometry
            assert "Writing the answer" in str(metadata.query_one(Static).render())
            assert_transcript_centered(app)

            await long.set_content("A short replacement.")
            await pilot.pause()
            assert not long.has_class("long")
            assert [(body.region.x, body.region.width) for body in fixed_bodies] == original_geometry
            assert_transcript_centered(app)

    asyncio.run(scenario())


def assert_transcript_centered(app: ScholarWeaveApp) -> None:
    transcript = app.query_one("#transcript")
    viewport = transcript.scrollable_content_region
    for body in transcript.query(".message-body, .note-body"):
        left_gap = body.region.x - viewport.x
        right_gap = viewport.right - body.region.right
        assert left_gap >= 0
        assert right_gap >= 0
        assert abs(left_gap - right_gap) <= 1
    assert transcript.max_scroll_x == 0
    composer = app.query_one("#composer-box")
    stage = app.query_one("#stage")
    left_gap = composer.region.x - stage.region.x
    right_gap = stage.region.right - composer.region.right
    assert abs(left_gap - right_gap) <= 1


def test_message_expansion_thresholds_apply_to_questions_and_answers() -> None:
    async def scenario() -> None:
        app = CockpitServer().app()
        async with app.run_test(size=(200, 60)) as pilot:
            await settle(app, pilot)
            transcript = app.query_one("#transcript")
            await transcript.query(Welcome).remove()
            messages = [ChatMessage(role, "") for role in ("user", "assistant")]
            await transcript.mount(*messages)
            for content, expanded in [
                ("a" * 699, False),
                ("a" * 700, True),
                ("\n".join(["line"] * 10), False),
                ("\n".join(["line"] * 11), True),
                ("Short again.", False),
            ]:
                for message in messages:
                    await message.set_content(content)
                await pilot.pause()
                for message in messages:
                    assert message.has_class("long") is expanded
                    assert message.query_one(".message-body").region.width == (160 if expanded else 120)
                assert_transcript_centered(app)

    asyncio.run(scenario())


def test_centered_live_activity_updates_and_removes_with_its_row() -> None:
    async def scenario() -> None:
        app = CockpitServer().app()
        async with app.run_test(size=(200, 60)) as pilot:
            await settle(app, pilot)
            transcript = app.query_one("#transcript")
            await transcript.query(Welcome).remove()
            app.run_record = RunResponse.model_validate(run_record("running"))
            app.live = LiveRun.restore(app.run_record)
            metadata = TranscriptNote("Stale progress", classes="turn-metadata live-progress")
            await transcript.mount(
                metadata,
                ChatMessage("assistant", "", id="live-response", thinking=True),
            )
            app._paint_clock()
            assert "Starting" in str(metadata.query_one(Static).render())
            assert metadata.display

            await app._show_steering("steer-1", "Focus on the methods.")
            await pilot.pause()
            assert metadata not in transcript.children
            assert len(transcript.children) == 3
            assert len(transcript.query(TranscriptNote)) == 1
            assert len(transcript.query("#live-response")) == 1
            assert_transcript_centered(app)

            app.run_record = RunResponse.model_validate(run_record())
            app.live = LiveRun.restore(app.run_record)
            await app._paint_live()
            await pilot.pause()
            assert not transcript.query_one(".live-progress", TranscriptNote).display
            assert transcript.query_one("#live-response", ChatMessage).content == app.live.assistant

    asyncio.run(scenario())


def test_message_updates_are_safe_before_mount_and_during_removal() -> None:
    async def scenario() -> None:
        app = CockpitServer().app()
        async with app.run_test(size=(200, 60)) as pilot:
            await settle(app, pilot)
            transcript = app.query_one("#transcript")
            message = ChatMessage("assistant", "", thinking=True)
            await message.set_content("Updated before mounting.")
            message.set_usage("Tokens  in 120")
            await transcript.mount(message)
            await pilot.pause()
            assert "Updated before mounting." in str(message.query_one("MarkdownParagraph").render())
            assert message.query_one(Markdown).display
            assert not message.query_one(Spinner).display
            assert message.query_one(".message-usage", Static).display

            removing = message.remove()
            await message.set_content("A final in-flight update.")
            message.set_activity_label("Finishing")
            message.set_usage("Tokens  in 120  ·  out 30")
            await removing
            assert message not in transcript.children

    asyncio.run(scenario())


@pytest.mark.parametrize("focused", [False, True])
def test_restored_transcript_stays_centered_on_resize_and_panel_changes(focused) -> None:
    async def scenario() -> None:
        server = CockpitServer()
        server.chats = [conversation()]
        record = run_record("cancelled")
        record["input"] = "A detailed question. " * 50
        record["final_output"] = (
            "# A detailed answer\n\n"
            + "A paragraph with **emphasis** and evidence. " * 30
            + "\n\n| Source | Finding |\n| --- | --- |\n| Paper | Evidence |\n\n"
            + "```text\n" + "wide content " * 25 + "\n```"
        )
        server.runs = [record]
        app = server.app()
        async with app.run_test(size=(280, 70)) as pilot:
            await settle(app, pilot)
            app.open_entry("chat-1")
            await settle(app, pilot)
            if focused:
                app.action_focus_mode()
            assert len(app.query(ChatMessage)) == 2
            assert all(message.has_class("long") for message in app.query(ChatMessage))
            notice = app.query_one(".run-note", TranscriptNote)
            assert "Partial output may be incomplete" in str(notice.query_one(Static).render())
            for width in (280, 160, 100, 80, 60, 280):
                await pilot.resize_terminal(width, 70)
                await pilot.pause()
                assert_transcript_centered(app)
                viewport = app.query_one("#transcript").scrollable_content_region
                for body in app.query(".message-body"):
                    assert body.region.width == min(160, viewport.width)
                assert app.query_one("#send").region.right <= width
            for key in ("ctrl+b", "ctrl+o", "ctrl+f"):
                await pilot.press(key)
                await pilot.pause()
                assert_transcript_centered(app)

    asyncio.run(scenario())


def test_slash_menu_completes_and_applies_commands() -> None:
    async def scenario() -> None:
        server = CockpitServer()
        app = server.app()
        async with app.run_test(size=(140, 42)) as pilot:
            await settle(app, pilot)
            composer = app.query_one("#composer", TextArea)
            menu = app.query_one("#slash", OptionList)
            composer.focus()
            await pilot.press("slash", "e", "f")
            await pilot.pause()
            assert menu.display
            assert menu.option_count == 1
            await pilot.press("tab")
            await pilot.pause()
            assert composer.text == "/effort "
            for key in "thorough":
                await pilot.press(key)
            await pilot.pause()
            await pilot.press("enter")
            await settle(app, pilot)
            assert app.effort == "thorough"
            assert composer.text == ""
            assert not menu.display
            assert "thorough" in str(app.query_one("#statusline", Static).render())
            # Escape leaves the typed text alone so a slash can still be prose.
            composer.load_text("/eff")
            await pilot.pause()
            assert menu.display
            composer.focus()
            await pilot.press("escape")
            await pilot.pause()
            assert not menu.display
            assert composer.text == "/eff"
    asyncio.run(scenario())


def test_model_picker_saves_choice_and_new_threads_use_it() -> None:
    async def scenario() -> None:
        server = CockpitServer()
        app = server.app()
        async with app.run_test(size=(140, 42)) as pilot:
            await settle(app, pilot)
            assert app.model_reference.model == "weave-fast"
            await pilot.press("ctrl+m")
            await pilot.pause()
            assert isinstance(app.screen, ChoicePicker)
            await pilot.press("down", "enter")
            await pilot.pause()
            assert isinstance(app.screen, ModelConfig)
            app.screen.query_one("#model-reasoning", Select).value = "high"
            app.screen.query_one("#model-context", Input).value = "65536"
            await pilot.click("#use-model")
            await settle(app, pilot)
            assert app.model_reference.model == "weave-deep"
            assert app.reasoning_effort == "high"
            assert app.context_window_tokens == 65_536
            assert server.last_chat_model_reference == {
                "provider_profile_id": "provider-1", "model": "weave-deep",
            }
            status = str(app.query_one("#statusline", Static).render())
            assert "weave-deep" in status
            assert "65,536 context" in status
            app.query_one("#composer", TextArea).load_text("Which model is thinking?")
            await pilot.click("#send")
            await settle(app, pilot)
            created = next(
                body for method, path, body in server.requests
                if method == "POST" and path == "/agent/conversations"
            )
            assert created["model_reference"] == {
                "provider_profile_id": "provider-1", "model": "weave-deep",
            }
            sent = next(
                body for method, path, body in server.requests
                if method == "POST" and path.endswith("/messages")
            )
            assert sent["reasoning_effort"] == "high"
            assert sent["context_window_tokens"] == 65_536

            app.run_command("model")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ModelConfig)
            assert app.screen.query_one("#model-reasoning", Select).value == "high"
            assert app.screen.query_one("#model-context", Input).value == "65536"
            await pilot.press("escape")
    asyncio.run(scenario())


def test_provider_command_adds_and_discovers_profile() -> None:
    async def scenario() -> None:
        server = CockpitServer()
        app = server.app()
        async with app.run_test(size=(140, 42)) as pilot:
            await settle(app, pilot)
            app.run_command("provider")
            await pilot.pause()
            assert isinstance(app.screen, ChoicePicker)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ProviderForm)
            app.screen.query_one("#provider-name", Input).value = "Lab server"
            app.screen.query_one("#provider-url", Input).value = "http://127.0.0.1:8080/v1"
            await pilot.click("#save-provider")
            await settle(app, pilot)

            created = next(
                body for method, path, body in server.requests
                if method == "POST" and path == "/providers"
            )
            assert created == {
                "name": "Lab server",
                "kind": "ollama",
                "base_url": "http://127.0.0.1:8080/v1",
                "api_key": None,
                "models": [],
                "serialize_model_switches": None,
            }
            assert any(
                method == "GET" and path == "/providers/provider-2/models"
                for method, path, _ in server.requests
            )
            assert app.providers[-1].models[0].name == "discovered-chat"
    asyncio.run(scenario())


def test_provider_command_updates_connection_without_clearing_saved_key() -> None:
    async def scenario() -> None:
        server = CockpitServer()
        server.providers[0]["api_key_set"] = True
        app = server.app()
        async with app.run_test(size=(140, 42)) as pilot:
            await settle(app, pilot)
            app.run_command("provider")
            await pilot.pause()
            await pilot.press("down", "enter")
            await pilot.pause()
            assert isinstance(app.screen, ProviderForm)
            app.screen.query_one("#provider-name", Input).value = "Renamed runtime"
            app.screen.query_one("#provider-url", Input).value = "http://127.0.0.1:9000/v1"
            await pilot.click("#save-provider")
            await settle(app, pilot)

            updated = next(
                body for method, path, body in server.requests
                if method == "PUT" and path == "/providers/provider-1"
            )
            assert updated == {
                "name": "Renamed runtime",
                "kind": "openai_compatible",
                "base_url": "http://127.0.0.1:9000/v1",
            }
            assert server.providers[0]["api_key_set"] is True
            assert app.providers[0].name == "Renamed runtime"
    asyncio.run(scenario())


def test_reasoning_is_limited_to_declared_levels_and_remembered(tmp_path) -> None:
    async def scenario() -> None:
        preferences = Preferences(path=tmp_path / "tui-preferences.json")
        server = CockpitServer()
        app = ScholarWeaveApp(
            ScholarWeaveClient(transport=httpx.MockTransport(server.handle)), preferences=preferences
        )
        async with app.run_test(size=(140, 42)) as pilot:
            await settle(app, pilot)
            # weave-fast declares no reasoning levels, so nothing can be forced on it.
            app.run_command("reasoning", "high")
            await settle(app, pilot)
            assert app.reasoning_effort is None
            await pilot.press("ctrl+g")
            await pilot.pause()
            assert app.screen.query_one("#choices-empty", Static).display
            assert not app.screen.query_one("#choices", OptionList).display
            await pilot.press("escape")
            await settle(app, pilot)
            await pilot.press("ctrl+m")
            await pilot.pause()
            await pilot.press("down", "enter")
            await pilot.pause()
            assert isinstance(app.screen, ModelConfig)
            await pilot.click("#use-model")
            await settle(app, pilot)
            assert app.model_reference.model == "weave-deep"
            app.run_command("reasoning", "high")
            await settle(app, pilot)
            assert app.reasoning_effort == "high"
            assert Preferences(path=preferences.path).reasoning("provider-1", "weave-deep") == "high"
            app.query_one("#composer", TextArea).load_text("Think this through.")
            await pilot.click("#send")
            await settle(app, pilot)
            sent = next(body for method, path, body in server.requests if path.endswith("/messages"))
            assert sent["reasoning_effort"] == "high"
    asyncio.run(scenario())


def test_failed_send_retains_draft_and_surfaces_error() -> None:
    async def scenario() -> None:
        server = CockpitServer()
        server.reject_send = True
        app = server.app()
        async with app.run_test(size=(130, 40)) as pilot:
            await settle(app, pilot)
            app.query_one("#composer", TextArea).load_text("Keep this question")
            await pilot.click("#send")
            await settle(app, pilot)
            assert app.query_one("#composer", TextArea).text == "Keep this question"
            assert app.run_record is None
            assert "FAILED" in str(app.query_one("#connection", Static).render())
            assert not app.query_one("#send", Button).disabled
            assert sum(path.endswith("/messages") for _, path, _ in server.requests) == 1
    asyncio.run(scenario())


def test_offline_startup_can_refresh_without_exiting() -> None:
    async def scenario() -> None:
        server = CockpitServer()
        server.offline = True
        app = server.app()
        async with app.run_test() as pilot:
            await settle(app, pilot)
            assert "FAILED" in str(app.query_one("#connection", Static).render())
            server.offline = False
            await pilot.press("ctrl+r")
            await settle(app, pilot)
            assert "CONNECTED" in str(app.query_one("#connection", Static).render())
    asyncio.run(scenario())


def test_notes_preview_edit_save_and_conflict_protection() -> None:
    async def scenario() -> None:
        server = CockpitServer()
        app = server.app()
        async with app.run_test(size=(140, 42)) as pilot:
            await settle(app, pilot)
            app.action_view("notes")
            app.open_entry(server.note["path"])
            await settle(app, pilot)
            await pilot.click("#edit-note")
            app.query_one("#note-editor", TextArea).load_text("# A stronger connection")
            await pilot.pause()
            assert app.note_dirty
            await pilot.press("ctrl+s")
            await settle(app, pilot)
            assert server.note["content"] == "# A stronger connection"
            assert not app.note_dirty
            saved = next(body for method, path, body in server.requests if method == "PUT")
            assert "tags" not in saved
            await pilot.click("#edit-note")
            app.query_one("#note-editor", TextArea).load_text("Keep my draft")
            server.note["content"] = "External edit"
            await pilot.press("ctrl+s")
            await settle(app, pilot)
            assert server.note["content"] == "External edit"
            assert app.query_one("#note-editor", TextArea).text == "Keep my draft"
            assert app.note_dirty
            app.action_quit()
            await pilot.pause()
            assert isinstance(app.screen, ConfirmDiscard)
            await pilot.press("escape")
            await settle(app, pilot)
            assert app.note_dirty
    asyncio.run(scenario())


def test_create_note_and_revert_guard() -> None:
    async def scenario() -> None:
        app = CockpitServer().app()
        async with app.run_test(size=(140, 42)) as pilot:
            await settle(app, pilot)
            await pilot.press("ctrl+3", "ctrl+n")
            await pilot.pause()
            assert isinstance(app.screen, NoteName)
            app.screen.query_one(Input).value = "A new insight"
            await pilot.click("#create")
            await settle(app, pilot)
            assert app.note.note_name == "A new insight"
            assert app.query_one("#note-content", ContentSwitcher).current == "note-editor"
            app.query_one("#note-editor", TextArea).load_text("Unsaved")
            await pilot.pause()
            await pilot.click("#revert-note")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmDiscard)
            await pilot.click("#discard")
            await settle(app, pilot)
            assert app.query_one("#note-editor", TextArea).text == ""
    asyncio.run(scenario())


def test_paper_reader_discussion_and_local_filter() -> None:
    async def scenario() -> None:
        server = CockpitServer()
        app = server.app()
        async with app.run_test(size=(140, 42)) as pilot:
            await settle(app, pilot)
            await pilot.press("ctrl+2")
            app.query_one("#search", Input).value = "missing"
            await pilot.pause()
            assert app.query_one("#catalog", OptionList).option_count == 0
            app.query_one("#search", Input).value = "Evidence"
            await pilot.pause()
            assert app.query_one("#catalog", OptionList).option_count == 1
            app.open_entry("paper-1")
            await settle(app, pilot)
            assert app.paper.chunks[0].citation == "[paper-1, p. 1]"
            await pilot.click("#discuss")
            await settle(app, pilot)
            assert app.view == "chat"
            assert "paper-1" in app.query_one("#composer", TextArea).text
            assert not any(path.endswith("/messages") for _, path, _ in server.requests)
    asyncio.run(scenario())


def test_active_run_steering_and_stop_use_run_routes() -> None:
    async def scenario() -> None:
        server = CockpitServer()
        app = server.app()
        async with app.run_test(size=(140, 42)) as pilot:
            await settle(app, pilot)
            from backend.runs.schemas import RunResponse
            app.run_record = RunResponse.model_validate(run_record("running"))
            app.current_id = "chat-1"
            app._controls()
            app._paint_clock()
            await pilot.pause()
            assert str(app.query_one("#send", Button).label) == "Steer"
            assert app.query_one("#runline").display
            assert app.query_one("#stop", Button).display
            assert ":" in str(app.query_one("#run-elapsed", Static).render())
            app.query_one("#composer", TextArea).load_text("Focus on the methods.")
            await pilot.click("#send")
            await settle(app, pilot)
            assert any(path == "/runs/run-1/steering" for _, path, _ in server.requests)
            assert not any(path.endswith("/messages") for _, path, _ in server.requests)
            assert any(message.content.endswith("Focus on the methods.") for message in app.query(ChatMessage))
            await pilot.click("#stop")
            await settle(app, pilot)
            assert any(path == "/runs/run-1/cancel" for _, path, _ in server.requests)
    asyncio.run(scenario())


def test_switching_conversations_preserves_each_draft_without_cancelling_runs() -> None:
    async def scenario() -> None:
        server = CockpitServer()
        server.chats = [conversation("chat-1"), conversation("chat-2")]
        app = server.app()
        async with app.run_test(size=(140, 42)) as pilot:
            await settle(app, pilot)
            await pilot.press("ctrl+n")
            await settle(app, pilot)
            app.query_one("#composer", TextArea).load_text("New conversation draft")
            app.open_entry("chat-1")
            await settle(app, pilot)
            app.query_one("#composer", TextArea).load_text("First conversation draft")
            app.open_entry("chat-2")
            await settle(app, pilot)
            app.query_one("#composer", TextArea).load_text("Second conversation draft")
            app.open_entry("chat-1")
            await settle(app, pilot)
            assert app.query_one("#composer", TextArea).text == "First conversation draft"
            await pilot.press("ctrl+n")
            await settle(app, pilot)
            assert app.query_one("#composer", TextArea).text == "New conversation draft"
            assert not any(path.endswith("/cancel") for _, path, _ in server.requests)
    asyncio.run(scenario())


def test_search_notes_uses_backend_index() -> None:
    async def scenario() -> None:
        server = CockpitServer()
        app = server.app()
        async with app.run_test(size=(140, 42)) as pilot:
            await settle(app, pilot)
            await pilot.press("ctrl+3")
            app.query_one("#search", Input).value = "spectroscopy"
            await settle(app, pilot)
            assert any(path == "/workspace/search" for _, path, _ in server.requests)
            assert app.query_one("#catalog", OptionList).option_count == 1
    asyncio.run(scenario())


def test_live_stream_reconnects_after_last_sequence_without_duplicate_text() -> None:
    class ReconnectingServer(CockpitServer):
        def __init__(self) -> None:
            super().__init__()
            self.cursors: list[int] = []

        def handle(self, request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/events"):
                self.cursors.append(int(request.url.params["after"]))
                events = [
                    {"sequence": 0, "event_type": "tool.started", "payload": {"tool_name": "read_file"}},
                    {"sequence": 1, "event_type": "model.stream", "payload": {
                        "raw_type": "response.output_text.delta", "delta": "Part",
                    }},
                ] if len(self.cursors) == 1 else [
                    {"sequence": 1, "event_type": "model.stream", "payload": {
                        "raw_type": "response.output_text.delta", "delta": "Part",
                    }},
                    {"sequence": 2, "event_type": "model.stream", "payload": {
                        "raw_type": "response.output_text.delta", "delta": " two",
                    }},
                    {"sequence": 3, "event_type": "run.completed", "payload": {}},
                ]
                return httpx.Response(200, text="".join(
                    f"data: {json.dumps(event)}\n\n" for event in events
                ), headers={"content-type": "text/event-stream"})
            if request.url.path == "/api/runs/run-1":
                record = run_record("running" if len(self.cursors) == 1 else "completed")
                record["final_output"] = None if len(self.cursors) == 1 else "Part two"
                return httpx.Response(200, json=record)
            return super().handle(request)

    async def scenario() -> None:
        server = ReconnectingServer()
        app = server.app()
        async with app.run_test(size=(140, 42)) as pilot:
            await settle(app, pilot)
            app.query_one("#composer", TextArea).load_text("Follow this thread.")
            await pilot.click("#send")
            await asyncio.wait_for(settle(app, pilot), timeout=10)
            assert server.cursors == [-1, 1]
            assert app.live.assistant == "Part two"
            assert app.run_record.status == "completed"
            assert len(app.query(ChatMessage)) == 2
    asyncio.run(scenario())


def test_enter_sends_and_shift_enter_keeps_writing() -> None:
    async def scenario() -> None:
        server = CockpitServer()
        app = server.app()
        async with app.run_test(size=(140, 42)) as pilot:
            await settle(app, pilot)
            composer = app.query_one("#composer", TextArea)
            composer.focus()
            await pilot.press("W", "h", "y", "shift+enter", "n", "o", "w")
            assert composer.text == "Why\nnow"
            await pilot.press("enter")
            await settle(app, pilot)
            sent = next(body for method, path, body in server.requests if path.endswith("/messages"))
            assert sent["content"] == "Why\nnow"
            assert composer.text == ""
    asyncio.run(scenario())


def test_composer_grows_from_three_lines_then_scrolls_at_cap() -> None:
    async def scenario() -> None:
        app = CockpitServer().app()
        async with app.run_test(size=(140, 42)) as pilot:
            await settle(app, pilot)
            composer = app.query_one("#composer", TextArea)
            assert composer.region.height == 3

            composer.load_text("\n".join(f"Line {index}" for index in range(6)))
            await pilot.pause()
            assert composer.region.height == 6

            composer.load_text("\n".join(f"Line {index}" for index in range(14)))
            await pilot.pause()
            assert composer.region.height == 8
            assert composer.max_scroll_y > 0

            composer.load_text("A long wrapped draft " * 100)
            await pilot.pause()
            assert composer.region.height == 8
            assert composer.max_scroll_y > 0

            composer.clear()
            await pilot.pause()
            assert composer.region.height == 3

    asyncio.run(scenario())


def test_stop_stays_hidden_until_a_run_is_active() -> None:
    async def scenario() -> None:
        server = CockpitServer()
        app = server.app()
        async with app.run_test(size=(140, 42)) as pilot:
            await settle(app, pilot)
            assert app.query_one("#stop", Button).display is False
            assert app.query_one("#runline").display is False
            assert app.query_one("#busy", Spinner).display is False
            app.query_one("#composer", TextArea).load_text("What connects these ideas?")
            await pilot.click("#send")
            await settle(app, pilot)
            assert app.query_one("#stop", Button).display is False
            assert app.query_one("#runline").display is False
            assert str(app.query_one("#send", Button).label) == "Send"
    asyncio.run(scenario())


def test_theme_picker_previews_applies_and_remembers_choice(tmp_path) -> None:
    async def scenario() -> None:
        preferences = Preferences(path=tmp_path / "tui-preferences.json")
        server = CockpitServer()
        app = ScholarWeaveApp(
            ScholarWeaveClient(transport=httpx.MockTransport(server.handle)), preferences=preferences
        )
        async with app.run_test(size=(140, 42)) as pilot:
            await settle(app, pilot)
            assert app.theme == DEFAULT_THEME
            assert {theme.name for theme in THEMES} <= set(app.available_themes)
            await pilot.press("ctrl+t")
            await pilot.pause()
            picker = app.screen
            assert isinstance(picker, ThemePicker)
            await pilot.press("down")
            await pilot.pause()
            previewed = app.theme
            assert previewed != DEFAULT_THEME
            await pilot.press("escape")
            await pilot.pause()
            assert app.theme == DEFAULT_THEME
            await pilot.press("ctrl+t")
            await pilot.pause()
            await pilot.press("down", "enter")
            await settle(app, pilot)
            assert app.theme == previewed
            assert Preferences(path=preferences.path).theme == previewed
    asyncio.run(scenario())


def test_notes_toggle_between_editing_and_preview() -> None:
    async def scenario() -> None:
        app = CockpitServer().app()
        async with app.run_test(size=(150, 46)) as pilot:
            await settle(app, pilot)
            await pilot.press("ctrl+3")
            app.open_entry("knowledge/ideas/notes.md")
            await settle(app, pilot)
            content = app.query_one("#note-content", ContentSwitcher)
            assert content.current == "note-scroll"
            await pilot.click("#edit-note")
            await settle(app, pilot)
            assert content.current == "note-editor"
            assert app.query_one("#edit-note", Button).label.plain == "Preview"
            app.query_one("#note-editor", TextArea).load_text("# Reworked")
            app.query_one("#catalog", OptionList).focus()
            await pilot.press("ctrl+e")
            await settle(app, pilot)
            assert content.current == "note-scroll"
            assert app.query_one("#edit-note", Button).label.plain == "Edit"
    asyncio.run(scenario())
