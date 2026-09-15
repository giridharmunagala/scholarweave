from __future__ import annotations

import asyncio

import httpx
import pytest

pytest.importorskip("textual")

from textual.widgets import Checkbox, TextArea

from backend.app import create_app
from scholarweave_tui.app import ScholarWeaveApp
from scholarweave_tui.client import ScholarWeaveClient
from scholarweave_tui.widgets import ChatMessage


async def settle(app: ScholarWeaveApp, pilot) -> None:
    await pilot.pause()
    while app.workers:
        await app.workers.wait_for_complete()
        await pilot.pause()


@pytest.mark.parametrize("initially_enabled", [True, False])
def test_terminal_chat_and_notes_use_real_local_backend(
    test_settings, stub_provider, initially_enabled,
) -> None:
    stub_provider.reply = "A connection grounded in your local evidence."

    async def scenario() -> None:
        backend = create_app(test_settings)
        transport = httpx.ASGITransport(app=backend)
        async with backend.router.lifespan_context(backend):
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as setup:
                provider = await setup.post("/api/providers", json={
                    "name": "TUI integration provider",
                    "kind": "openai_compatible",
                    "base_url": f"{stub_provider.base_url}/v1",
                    "models": [{
                        "name": "stub-model", "capabilities": ["chat", "tools"],
                        "enabled": initially_enabled,
                    }],
                })
                assert provider.status_code == 201, provider.text
                settings = await setup.put("/api/settings", json={
                    "default_model_references": {"chat": {
                        "provider_profile_id": provider.json()["id"], "model": "stub-model",
                    }},
                })
                assert settings.status_code == 200, settings.text
            client = ScholarWeaveClient(transport=transport)
            app = ScholarWeaveApp(client)
            async with app.run_test(size=(140, 42)) as pilot:
                await settle(app, pilot)
                # The real backend resolves the configured chat model for this session.
                assert app.model_reference.model == "stub-model"
                if not initially_enabled:
                    app.run_command("model")
                    await pilot.pause()
                    await pilot.press("enter")
                    await pilot.pause()
                    assert not app.screen.query_one("#model-enabled", Checkbox).value
                    app.screen.query_one("#model-enabled", Checkbox).value = True
                    await pilot.click("#use-model")
                    await settle(app, pilot)
                    rediscovered = await client.discover_provider_models(provider.json()["id"])
                    assert next(
                        model for model in rediscovered.models if model.name == "stub-model"
                    ).enabled
                app.query_one("#composer", TextArea).load_text("Explain how to connect two ideas.")
                app.run_command("effort", "quick")
                app.query_one("#composer", TextArea).load_text("Explain how to connect two ideas.")
                await pilot.click("#send")
                await asyncio.wait_for(settle(app, pilot), timeout=30)
                assert app.run_record is not None
                assert app.run_record.status == "completed", app.run_record.error
                assert app.live.assistant == stub_provider.reply
                assert len(stub_provider.requests) == 1
                first_conversation_id = app.current_id
                first_run_id = app.run_record.id
                app.open_entry(app.current_id)
                await settle(app, pilot)
                assert len(app.query(ChatMessage)) == 2
                assert app.live.assistant == stub_provider.reply

                note = await client.create_note("Terminal ideas", "Original thought.")
                app.action_view("notes")
                app.open_entry(note.path)
                await settle(app, pilot)
                app.edit_note()
                app.query_one("#note-editor", TextArea).load_text("# Spectroscopy connections")
                await pilot.pause()
                await pilot.press("ctrl+s")
                await settle(app, pilot)
                assert not app.note_dirty
                saved = await client.read_note(note.path)
                assert saved.content == "# Spectroscopy connections"
                matches = await client.search_notes("spectroscopy")
                assert note.path in {match.path for match in matches}
                assert len(stub_provider.requests) == 1

            restarted = ScholarWeaveApp(ScholarWeaveClient(transport=transport))
            async with restarted.run_test(size=(140, 42)) as pilot:
                await settle(restarted, pilot)
                assert restarted.current_id is None
                assert restarted.run_record is None
                assert len(restarted.query(ChatMessage)) == 0
                assert [item.id for item in restarted.conversations] == [first_conversation_id]
                restarted.run_command("effort", "quick")
                restarted.query_one("#composer", TextArea).load_text("Start a separate discussion.")
                await pilot.click("#send")
                await asyncio.wait_for(settle(restarted, pilot), timeout=30)
                assert restarted.run_record is not None
                assert restarted.run_record.status == "completed", restarted.run_record.error
                assert restarted.current_id != first_conversation_id
                assert len(await restarted.client.conversations()) == 2
                assert len(stub_provider.requests) == 2

                restarted.open_entry(first_conversation_id)
                await settle(restarted, pilot)
                assert restarted.current_id == first_conversation_id
                assert restarted.run_record.id == first_run_id
                assert len(restarted.query(ChatMessage)) == 2

    asyncio.run(scenario())
