from __future__ import annotations

import asyncio

import anyio
import httpx
import pytest

from backend.providers.ollama import OllamaClient
from backend.providers.inference import InferenceScheduler


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


@pytest.mark.anyio
async def test_native_calls_share_global_lane_without_blocking_discovery(
    test_settings, monkeypatch,
) -> None:
    scheduler = InferenceScheduler()
    scheduler.configure("local", serialize_model_switches=True)
    client = OllamaClient(test_settings, inference_scheduler=scheduler, profile_id="local")
    calls = []

    async def respond(_method, path, **kwargs):
        calls.append((path, (kwargs.get("json") or {}).get("model")))
        return httpx.Response(200, json={"response": "ok", "models": [], "embeddings": [[1.0]]})

    monkeypatch.setattr(client, "_request_unlocked", respond)
    async with asyncio.timeout(1):
        async with scheduler.request(profile_id="other-provider", model="main"):
            generation = asyncio.create_task(client.generate("main", "hello"))
            pending = asyncio.create_task(client.embed("small", "hello"))
            await asyncio.sleep(0)
            assert not generation.done()
            assert not pending.done()
            assert await client.list_models() == []
            assert calls == [("/api/tags", None)]
        assert (await generation)["response"] == "ok"
        assert await pending == [[1.0]]
    assert calls[-1] == ("/api/embed", "small")
