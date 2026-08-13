from __future__ import annotations

import asyncio
from io import BytesIO

from fastapi import UploadFile
from fastapi.testclient import TestClient
import pytest

from backend.app import create_app
from backend.providers.builtin_speech import (
    BuiltInSpeechRuntime,
    MODEL_FILES,
    MODEL_PACKAGE,
)
from backend.providers.errors import ProviderRuntimeError
from backend.providers import router as provider_router


@pytest.fixture()
def anyio_backend() -> str:
    return "asyncio"


def install_test_model(runtime: BuiltInSpeechRuntime) -> None:
    model_dir = runtime.root / "models" / MODEL_PACKAGE
    model_dir.mkdir(parents=True)
    for filename in MODEL_FILES:
        (model_dir / filename).write_bytes(b"model")
    runtime._write_manifest()


def test_nemotron_installation_requires_manifest_and_can_be_deleted(
    test_settings,
) -> None:
    runtime = BuiltInSpeechRuntime(test_settings)
    install_test_model(runtime)

    with TestClient(create_app(test_settings)) as client:
        status = client.get("/api/providers/speech/builtin/status")
        (runtime.model_dir / MODEL_FILES[0]).unlink()
        corrupted_status = client.get("/api/providers/speech/builtin/status")
        removed = client.delete("/api/providers/speech/builtin")

    assert status.status_code == 200
    assert status.json()["state"] == "ready"
    assert status.json()["model"] == "nvidia/nemotron-speech-streaming-en-0.6b"
    assert corrupted_status.status_code == 200
    assert corrupted_status.json()["state"] == "not_installed"
    assert removed.status_code == 200
    assert removed.json()["state"] == "not_installed"
    assert not runtime.root.exists()


@pytest.mark.anyio
async def test_uninstall_cancels_the_active_install_task(
    test_settings,
    monkeypatch,
) -> None:
    runtime = BuiltInSpeechRuntime(test_settings)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_install() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(runtime, "_install_and_load", blocked_install)

    await runtime.install()
    await started.wait()
    status = await runtime.uninstall()

    assert cancelled.is_set()
    assert runtime._install_task is None
    assert status.state == "not_installed"


@pytest.mark.anyio
async def test_start_loads_the_installed_nemotron_model(
    test_settings,
    monkeypatch,
) -> None:
    runtime = BuiltInSpeechRuntime(test_settings)
    install_test_model(runtime)
    recognizer = object()
    monkeypatch.setattr(runtime, "_load_recognizer", lambda: recognizer)

    status = await runtime.start()

    assert runtime._recognizer is recognizer
    assert status.state == "running"


def test_websocket_streams_partial_and_final_transcripts(
    test_settings,
    monkeypatch,
) -> None:
    class FakeSession:
        async def accept_pcm(self, content: bytes) -> str:
            assert content == b"\x00\x01"
            return "live transcript"

        async def finish(self) -> str:
            return "final transcript"

    app = create_app(test_settings)

    async def create_session() -> FakeSession:
        return FakeSession()

    monkeypatch.setattr(app.state.services.builtin_speech, "create_session", create_session)

    with TestClient(app) as client:
        with client.websocket_connect("/api/providers/speech/builtin/stream") as socket:
            assert socket.receive_json() == {"type": "ready"}
            socket.send_bytes(b"\x00\x01")
            assert socket.receive_json() == {
                "type": "partial",
                "text": "live transcript",
            }
            socket.send_text("finish")
            assert socket.receive_json() == {
                "type": "final",
                "text": "final transcript",
            }


@pytest.mark.anyio
async def test_audio_upload_is_rejected_before_reading_past_limit(monkeypatch) -> None:
    monkeypatch.setattr(provider_router, "MAX_AUDIO_BYTES", 4)
    upload = UploadFile(file=BytesIO(b"12345"), filename="recording.wav")

    with pytest.raises(ProviderRuntimeError, match="25 MB limit"):
        await provider_router._read_audio_upload(upload)

    assert upload.file.closed
