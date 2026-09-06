from __future__ import annotations

import json
from contextlib import asynccontextmanager
from collections.abc import Awaitable, Callable
from typing import Any, AsyncIterator

import anyio
import httpx

from backend.core.config import Settings
from backend.providers.inference import InferenceScheduler


class OllamaError(RuntimeError):
    pass


class OllamaClient:
    def __init__(
        self,
        settings: Settings,
        base_url: str | None = None,
        request_lock: anyio.Lock | None = None,
        inference_scheduler: InferenceScheduler | None = None,
        profile_id: str | None = None,
    ) -> None:
        if request_lock is not None and inference_scheduler is not None:
            raise ValueError("Configure either a request lock or an inference scheduler, not both.")
        self.settings = settings
        self._base_url = base_url.rstrip("/") if base_url else None
        self._request_lock = request_lock
        self._inference_scheduler = inference_scheduler
        self.profile_id = profile_id

    @property
    def base_url(self) -> str:
        return self._base_url or self.settings.ollama_base_url.rstrip("/")

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if self._request_lock is None and path not in {
            "/api/chat", "/api/generate", "/api/embed", "/api/embeddings",
        }:
            return await self._request_unlocked(method, path, **kwargs)
        async with self._request_guard(model=(kwargs.get("json") or {}).get("model")):
            return await self._request_unlocked(method, path, **kwargs)

    async def _request_unlocked(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response:
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=self.settings.request_timeout_seconds) as client:
                response = await client.request(method, path, **kwargs)
                response.raise_for_status()
                return response
        except httpx.HTTPError as exc:
            raise OllamaError(f"Ollama request failed: {exc}") from exc

    async def list_models(self) -> list[dict[str, Any]]:
        response = await self._request("GET", "/api/tags")
        payload = response.json()
        return payload.get("models", [])

    async def show_model(self, model: str) -> dict[str, Any]:
        response = await self._request("POST", "/api/show", json={"model": model})
        return response.json()

    async def running_models(self) -> list[dict[str, Any]]:
        response = await self._request("GET", "/api/ps")
        payload = response.json()
        return payload.get("models", [])

    async def unload_model(self, model: str) -> None:
        await self._request(
            "POST",
            "/api/generate",
            json={"model": model, "prompt": "", "keep_alive": 0, "stream": False},
        )

    async def unload_all_models(self) -> list[str]:
        names = [
            str(item.get("name") or item.get("model"))
            for item in await self.running_models()
            if item.get("name") or item.get("model")
        ]
        for name in names:
            await self.unload_model(name)
        return names

    async def generate(
        self,
        model: str,
        prompt: str,
        *,
        stream: bool = False,
        options: dict[str, Any] | None = None,
        system: str | None = None,
        format_: dict[str, Any] | str | None = None,
        images: list[str] | None = None,
        think: bool | str | None = None,
        on_token: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": model, "prompt": prompt, "stream": stream}
        if options:
            payload["options"] = options
        if system:
            payload["system"] = system
        if format_ is not None:
            payload["format"] = format_
        if images:
            payload["images"] = images
        if think is not None:
            payload["think"] = think
        if not stream:
            response = await self._request("POST", "/api/generate", json=payload)
            return response.json()

        chunks: list[str] = []
        async with self._request_guard(model=model):
            async with httpx.AsyncClient(base_url=self.base_url, timeout=None) as client:
                try:
                    async with client.stream("POST", "/api/generate", json=payload) as response:
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            if not line:
                                continue
                            message = json.loads(line)
                            token = message.get("response", "")
                            if token:
                                chunks.append(token)
                                if on_token:
                                    await on_token(token)
                            if message.get("done"):
                                return {"response": "".join(chunks), "raw": message}
                except httpx.HTTPError as exc:
                    raise OllamaError(f"Ollama streaming generate failed: {exc}") from exc
        return {"response": "".join(chunks), "raw": {}}

    async def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        stream: bool = False,
        options: dict[str, Any] | None = None,
        format_: dict[str, Any] | str | None = None,
        on_token: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": model, "messages": messages, "stream": stream}
        if options:
            payload["options"] = options
        if format_ is not None:
            payload["format"] = format_
        if not stream:
            response = await self._request("POST", "/api/chat", json=payload)
            return response.json()

        chunks: list[str] = []
        async with self._request_guard(model=model):
            async with httpx.AsyncClient(base_url=self.base_url, timeout=None) as client:
                try:
                    async with client.stream("POST", "/api/chat", json=payload) as response:
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            if not line:
                                continue
                            message = json.loads(line)
                            token = message.get("message", {}).get("content", "")
                            if token:
                                chunks.append(token)
                                if on_token:
                                    await on_token(token)
                            if message.get("done"):
                                return {"message": {"content": "".join(chunks)}, "raw": message}
                except httpx.HTTPError as exc:
                    raise OllamaError(f"Ollama streaming chat failed: {exc}") from exc
        return {"message": {"content": "".join(chunks)}, "raw": {}}

    async def embed(self, model: str, inputs: str | list[str]) -> list[list[float]]:
        payload = {"model": model, "input": inputs}
        try:
            response = await self._request("POST", "/api/embed", json=payload)
            data = response.json()
            if "embeddings" in data:
                return data["embeddings"]
            if "embedding" in data:
                return [data["embedding"]]
        except OllamaError:
            response = await self._request("POST", "/api/embeddings", json={"model": model, "prompt": inputs if isinstance(inputs, str) else "\n".join(inputs)})
            data = response.json()
            return [data.get("embedding", [])]
        raise OllamaError("Unexpected Ollama embeddings response")

    @asynccontextmanager
    async def _request_guard(self, *, model: str | None) -> AsyncIterator[None]:
        if self._inference_scheduler is not None:
            async with self._inference_scheduler.request(
                profile_id=self.profile_id, model=model, server_url=self.base_url,
            ):
                yield
            return
        if self._request_lock is None:
            yield
            return
        async with self._request_lock:
            yield
