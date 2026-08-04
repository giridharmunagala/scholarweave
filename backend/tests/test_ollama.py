from __future__ import annotations

import httpx
import pytest

from backend.ollama import OllamaClient


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
