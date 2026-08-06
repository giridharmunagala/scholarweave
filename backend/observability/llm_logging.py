"""Persistent, credential-safe audit logging for model requests."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import anyio

if TYPE_CHECKING:
    from backend.core.config import Settings

_SENSITIVE_KEYS = {"api_key", "authorization", "x-api-key", "token"}


class LLMCallLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def write(
        self,
        *,
        provider: str,
        model: str | None,
        operation: str,
        request: Any,
        response: Any = None,
        error: BaseException | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "provider": provider,
            "model": model,
            "operation": operation,
            "request": _redact(request),
        }
        if error is not None:
            entry["error"] = {"type": type(error).__name__, "message": str(error)}
        else:
            entry["response"] = _redact(response)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(entry, ensure_ascii=True, default=str) + "\n")


class LoggingTransport(httpx.AsyncBaseTransport):
    """Logs OpenAI-compatible LLM endpoints used by the Agents SDK."""

    def __init__(self, logger: LLMCallLogger, provider: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._logger = logger
        self._provider = provider
        self._transport = transport or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not _is_llm_endpoint(request.url.path):
            return await self._transport.handle_async_request(request)

        request_body = _decode_body(request.content)
        model = request_body.get("model") if isinstance(request_body, dict) else None
        try:
            response = await self._transport.handle_async_request(request)
            body = await response.aread()
            self._logger.write(
                provider=self._provider,
                model=str(model) if model else None,
                operation=request.url.path,
                request=request_body,
                response=_decode_response_body(body),
            )
            return response
        except Exception as exc:
            self._logger.write(
                provider=self._provider,
                model=str(model) if model else None,
                operation=request.url.path,
                request=request_body,
                error=exc,
            )
            raise

    async def aclose(self) -> None:
        await self._transport.aclose()


class LockedTransport(httpx.AsyncBaseTransport):
    """Keeps an inference request inside a shared provider lifecycle gate."""

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport,
        request_lock: anyio.Lock,
    ) -> None:
        self._transport = transport
        self._request_lock = request_lock

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        async with self._request_lock:
            return await self._transport.handle_async_request(request)

    async def aclose(self) -> None:
        await self._transport.aclose()


def _is_llm_endpoint(path: str) -> bool:
    return path.endswith(("/chat/completions", "/embeddings", "/responses"))


def logged_http_client(
    settings: Settings,
    provider: str,
    request_lock: anyio.Lock | None = None,
) -> httpx.AsyncClient:
    transport: httpx.AsyncBaseTransport = LoggingTransport(
        LLMCallLogger(settings.llm_log_path),
        provider,
    )
    if request_lock is not None:
        transport = LockedTransport(transport, request_lock)
    return httpx.AsyncClient(
        transport=transport,
        timeout=settings.request_timeout_seconds,
    )


def _decode_body(body: bytes) -> Any:
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body.decode("utf-8", errors="replace")


def _decode_response_body(body: bytes) -> Any:
    decoded = _decode_body(body)
    if not isinstance(decoded, str):
        return decoded
    return _combine_sse_chunks(decoded) or decoded


def _combine_sse_chunks(body: str) -> dict[str, Any] | None:
    chunks: list[dict[str, Any]] = []
    for event in body.split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            return None
        if not isinstance(chunk, dict):
            return None
        chunks.append(chunk)

    if not chunks:
        return None

    response = {key: value for chunk in chunks for key, value in chunk.items() if key != "choices"}
    response["object"] = "chat.completion"
    choices: dict[int, dict[str, Any]] = {}
    for chunk in chunks:
        for position, choice in enumerate(chunk.get("choices", [])):
            if not isinstance(choice, dict):
                return None
            index = choice.get("index", position)
            if not isinstance(index, int):
                return None
            combined = choices.setdefault(index, {"index": index, "message": {}, "finish_reason": None})
            payload = choice.get("delta", choice.get("message", {}))
            if not isinstance(payload, dict):
                return None
            _merge_message(combined["message"], payload)
            if choice.get("finish_reason") is not None:
                combined["finish_reason"] = choice["finish_reason"]

    response["choices"] = [choices[index] for index in sorted(choices)]
    return response


def _merge_message(message: dict[str, Any], delta: dict[str, Any]) -> None:
    for key, value in delta.items():
        if value is None:
            continue
        if key in {"content", "reasoning", "refusal"}:
            message[key] = f"{message.get(key, '')}{value}"
            _append_message_part(message, key, value)
        elif key == "tool_calls" and isinstance(value, list):
            _merge_tool_calls(message, value)
        else:
            message[key] = value


def _append_message_part(message: dict[str, Any], kind: str, value: Any) -> None:
    if not value:
        return
    parts = message.setdefault("parts", [])
    if not isinstance(parts, list):
        raise TypeError("A streamed message has an invalid parts value.")
    if parts and isinstance(parts[-1], dict) and parts[-1].get("type") == kind:
        parts[-1]["text"] = f"{parts[-1].get('text', '')}{value}"
    else:
        parts.append({"type": kind, "text": value})


def _merge_tool_calls(message: dict[str, Any], delta_calls: list[Any]) -> None:
    calls = message.setdefault("tool_calls", [])
    if not isinstance(calls, list):
        raise TypeError("A streamed message has an invalid tool_calls value.")
    calls_by_index = {
        call.get("index"): call for call in calls if isinstance(call, dict) and isinstance(call.get("index"), int)
    }
    for position, delta_call in enumerate(delta_calls):
        if not isinstance(delta_call, dict):
            raise TypeError("A streamed tool call must be an object.")
        index = delta_call.get("index", position)
        if not isinstance(index, int):
            raise TypeError("A streamed tool call index must be an integer.")
        call = calls_by_index.get(index)
        if call is None:
            call = {"index": index}
            calls.append(call)
            calls_by_index[index] = call
        for key, value in delta_call.items():
            if key == "function" and isinstance(value, dict):
                function = call.setdefault("function", {})
                if not isinstance(function, dict):
                    raise TypeError("A streamed tool call has an invalid function value.")
                for function_key, function_value in value.items():
                    if function_key == "arguments" and function_value is not None:
                        function[function_key] = f"{function.get(function_key, '')}{function_value}"
                    elif function_value is not None:
                        function[function_key] = function_value
            elif value is not None:
                call[key] = value


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() in _SENSITIVE_KEYS else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value
