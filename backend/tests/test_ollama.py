from __future__ import annotations

import anyio
import httpx
import pytest

from backend.providers.ollama import OllamaClient


@pytest.mark.anyio
async def test_generate_sends_multimodal_images(test_settings) -> None:
    client = OllamaClient(test_settings)
    captured: dict[str, object] = {}

    async def fake_request(method: str, path: str, **kwargs: object) -> httpx.Response:
        captured.update({"method": method, "path": path, **kwargs})
        return httpx.Response(200, json={"response": "markdown"})

    client._request = fake_request  # type: ignore[method-assign]

    result = await client.generate(
        "vision-model",
        "reconstruct",
        stream=False,
        images=["base64-image"],
        think=False,
    )

    assert result == {"response": "markdown"}
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/generate"
    assert captured["json"] == {
        "model": "vision-model",
        "prompt": "reconstruct",
        "stream": False,
        "images": ["base64-image"],
        "think": False,
    }


@pytest.mark.anyio
async def test_requests_wait_for_shared_gpu_lock(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_lock = anyio.Lock()
    client = OllamaClient(test_settings, request_lock=request_lock)
    requested = anyio.Event()

    async def fake_request(
        _method: str,
        _path: str,
        **_kwargs: object,
    ) -> httpx.Response:
        requested.set()
        return httpx.Response(200, json={"models": []})

    monkeypatch.setattr(client, "_request_unlocked", fake_request)
    await request_lock.acquire()

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(client.list_models)
        await anyio.sleep(0.01)
        assert not requested.is_set()
        request_lock.release()

    assert requested.is_set()
