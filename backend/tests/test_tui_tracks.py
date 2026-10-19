from __future__ import annotations

import asyncio

import httpx
import pytest

pytest.importorskip("textual")

from textual.widgets import Button, ContentSwitcher, Input, OptionList, TextArea
from textual.worker import WorkerCancelled

from backend.app import create_app
from backend.providers.schemas import ProviderCreate, ProviderModel
from backend.research.reviews.schemas import ReviewCreateRequest
from scholarweave_tui.app import ScholarWeaveApp
from scholarweave_tui.client import ScholarWeaveClient
from scholarweave_tui.preferences import Preferences
from scholarweave_tui.widgets import ChoicePicker, ConfirmDiscard, NoteName


async def settle(app, pilot):
    await pilot.pause()
    while app.workers:
        try:
            await app.workers.wait_for_complete()
        except WorkerCancelled:
            pass
        await pilot.pause()


async def click_dialog_button(app, pilot, selector):
    async def visible():
        while app.screen.query_one(selector, Button).region.width == 0:
            await pilot.pause()

    await asyncio.wait_for(visible(), timeout=5)
    await pilot.click(selector)


async def wait_for_screen(app, pilot, screen_type):
    async def wait():
        while not isinstance(app.screen, screen_type):
            await pilot.pause()

    await asyncio.wait_for(wait(), timeout=5)
    await pilot.pause()


def test_terminal_switches_review_main_model_without_changing_chat_model(test_settings, monkeypatch):
    async def scenario():
        backend = create_app(test_settings)
        services = backend.state.services
        monkeypatch.setattr(services.reviews, "_schedule", lambda review_id: None)
        provider = services.providers.create(ProviderCreate(
            name="Local models", kind="openai_compatible", base_url="http://127.0.0.1:12345/v1",
            models=[ProviderModel(name=name, capabilities={"chat"}) for name in ("original", "replacement")],
        ))
        spec = ReviewCreateRequest(
            title="Saved research", objective="Compare methods.",
            main_model={"provider_profile_id": provider.id, "model": "original"},
        )
        services.reviews.repository.create("terminal-review", spec.model_dump(mode="json"), {
            "status": "paused", "stage": "reading", "stage_history": {"discovery": "completed"},
            "work_budget": {"screened_papers": 100, "read_papers": 20, "comparison_passes": 1},
        })
        client = ScholarWeaveClient(transport=httpx.ASGITransport(app=backend))
        app = ScholarWeaveApp(client, Preferences(test_settings.data_dir / "tui-preferences.json"))
        try:
            async with app.run_test(size=(160, 48)) as pilot:
                await settle(app, pilot)
                chat_model = app.model_reference
                app.choose_literature_review()
                await wait_for_screen(app, pilot, ChoicePicker)
                await pilot.press("down", "enter")
                await wait_for_screen(app, pilot, ChoicePicker)
                await pilot.press("end", "enter")
                await wait_for_screen(app, pilot, ChoicePicker)
                await pilot.press("end", "enter")
                await settle(app, pilot)
                review = services.reviews.get("terminal-review")
                assert review.main_model.model == "replacement"
                assert review.status == "running" and review.stage == "reading"
                assert review.stage_history == {"discovery": "completed"}
                assert app.model_reference == chat_model
        finally:
            await client.close()
            await services.close()

    asyncio.run(scenario())


def test_terminal_exploration_editor_history_and_discussion(test_settings):
    async def scenario():
        backend = create_app(test_settings)
        client = ScholarWeaveClient(transport=httpx.ASGITransport(app=backend))
        track = await client.create_track("Exploration")
        first = await client.save_track_section(track.id, "exploration/ideas", "# Tentative ideas", None)
        app = ScholarWeaveApp(client, Preferences(test_settings.data_dir / "tui-preferences.json"))
        try:
            async with app.run_test(size=(160, 48)) as pilot:
                await settle(app, pilot)
                app.action_view("tracks")
                await settle(app, pilot)
                app.open_entry(track.id)
                await settle(app, pilot)
                app.change_track_section("exploration/ideas")
                await settle(app, pilot)
                assert app.track_section.section == "exploration/ideas"
                app.edit_track()
                app.query_one("#track-editor", TextArea).load_text(first.content + "\nNew lead")
                await pilot.pause()
                app.action_save_note()
                await settle(app, pilot)
                assert not app.track_dirty
                assert (await client.track_section(track.id, "exploration/ideas")).content.endswith("New lead")
                history = await client.track_history(track.id, "exploration/ideas")
                assert (await client.track_revision(track.id, "exploration/ideas", history[0].revision)).content == first.content
                app.choose_track_exploration()
                await wait_for_screen(app, pilot, ChoicePicker)
                assert isinstance(app.screen, ChoicePicker)
                await pilot.press("enter")
                await wait_for_screen(app, pilot, NoteName)
                assert isinstance(app.screen, NoteName)
                app.screen.query_one("#note-name", Input).value = "open-questions"
                await pilot.press("enter")
                await settle(app, pilot)
                assert app.track_section.section == "exploration/open-questions"
                assert len((await client.track(track.id)).exploration) == 2
                app.discuss_track()
                await settle(app, pilot)
                assert app.view == "chat"
        finally:
            await client.close()
            await backend.state.services.close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=60))


def test_terminal_track_editing_conflicts_and_section_draft_protection(test_settings):
    async def scenario():
        backend = create_app(test_settings)
        client = ScholarWeaveClient(transport=httpx.ASGITransport(app=backend))
        first = await client.create_track("Baseline research")
        second = await client.create_track("Another question")
        app = ScholarWeaveApp(client, Preferences(test_settings.data_dir / "tui-preferences.json"))
        try:
            async with app.run_test(size=(160, 48)) as pilot:
                await settle(app, pilot)
                app.action_view("tracks")
                await settle(app, pilot)
                assert app.query_one("#catalog", OptionList).option_count == 2
                app.open_entry(first.id)
                await settle(app, pilot)
                assert app.track.id == first.id
                original = app.track_section
                app.edit_track()
                editor = app.query_one("#track-editor", TextArea)
                draft = original.content + "\n\nPreserve the supplied constraint.\n"
                editor.load_text(draft)
                await pilot.pause()
                assert app.track_dirty
                app.action_view("chat")
                app.action_view("tracks")
                await settle(app, pilot)
                assert editor.text == draft
                app.change_track_section("results")
                await pilot.pause()
                assert isinstance(app.screen, ConfirmDiscard)
                await click_dialog_button(app, pilot, "#keep")
                await settle(app, pilot)
                assert app.track_section.section == "problem" and editor.text == draft
                await client.save_track_section(first.id, "problem", original.content + "\nExternal edit.\n", original.sha256)
                app.action_save_note()
                await settle(app, pilot)
                assert editor.text == draft and app.track_dirty
                assert (await client.track_section(first.id, "problem")).content.endswith("External edit.\n")
                app.open_entry(second.id)
                await pilot.pause()
                assert isinstance(app.screen, ConfirmDiscard)
                await click_dialog_button(app, pilot, "#keep")
                await settle(app, pilot)
                assert app.track.id == first.id and editor.text == draft
                app.open_entry(first.id)
                await pilot.pause()
                await click_dialog_button(app, pilot, "#discard")
                await settle(app, pilot)
                assert not app.track_dirty
                editor.load_text(app.track_section.content + "\nReconciled findings.\n")
                await pilot.pause()
                app.action_save_note()
                await settle(app, pilot)
                assert not app.track_dirty
                assert "External edit." in (await client.track_section(first.id, "problem")).content
                assert await client.track_history(first.id, "problem")
                app.change_track_section("experiments")
                await settle(app, pilot)
                assert app.track_section.section == "experiments"
                assert app.query_one("#track-content", ContentSwitcher).current == "track-scroll"
        finally:
            await client.close()
            await backend.state.services.close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=60))


def test_terminal_track_discussion_is_unsent_and_attaches_created_chat(test_settings):
    async def scenario():
        backend = create_app(test_settings)
        client = ScholarWeaveClient(transport=httpx.ASGITransport(app=backend))
        track = await client.create_track("Literature comparison")
        app = ScholarWeaveApp(client, Preferences(test_settings.data_dir / "tui-preferences.json"))
        try:
            async with app.run_test(size=(160, 48)) as pilot:
                await settle(app, pilot)
                app.action_view("tracks")
                await settle(app, pilot)
                app.open_entry(track.id)
                await settle(app, pilot)
                app.change_track_section("literature")
                await settle(app, pilot)
                app.discuss_track()
                await settle(app, pilot)
                assert app.view == "chat"
                assert app.current_id is None
                assert app._chat_track_id == track.id
                assert "literature-review" in app.query_one("#composer", TextArea).text
                assert not await client.conversations()
                assert not await client.runs()
                conversation = await client.create_conversation(track_id=track.id)
                assert conversation.track_id == track.id
                await client.unlink_track_conversation(track.id, conversation.id)
                assert (await client.conversation(conversation.id)).track_id is None
                await client.link_track_conversation(track.id, conversation.id)
                assert (await client.conversation(conversation.id)).track_id == track.id
        finally:
            await client.close()
            await backend.state.services.close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=60))
