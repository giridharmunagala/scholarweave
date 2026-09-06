"""Persistent, credential-safe audit logging for model requests."""

from __future__ import annotations

import json
import threading
from time import perf_counter
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import anyio

if TYPE_CHECKING:
    from backend.core.config import Settings
    from backend.providers.inference import InferenceScheduler

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
        timing: dict[str, float | bool | None] | None = None,
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
        if timing is not None:
            entry["timing"] = timing
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(entry, ensure_ascii=True, default=str) + "\n")


class LoggingTransport(httpx.AsyncBaseTransport):
    """Logs OpenAI-compatible LLM endpoint traffic."""

    def __init__(self, logger: LLMCallLogger, provider: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._logger = logger
        self._provider = provider
        self._transport = transport or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not _is_llm_endpoint(request.url.path):
            return await self._transport.handle_async_request(request)

        request_body = _decode_body(request.content)
        model = request_body.get("model") if isinstance(request_body, dict) else None
        started = perf_counter()
        queue_wait = request.extensions.get("scholarweave_queue_wait_seconds")
        queue_wait_seconds = float(queue_wait) if queue_wait is not None else None
        timings: dict[str, float | bool | None] = {
            "queue_wait_seconds": queue_wait_seconds,
            "response_headers_seconds": None,
            "first_byte_seconds": None,
            "response_complete": False,
        }
        try:
            response = await self._transport.handle_async_request(request)
            timings["response_headers_seconds"] = perf_counter() - started
            if response.is_closed:
                timings["response_complete"] = True
                self._logger.write(
                    provider=self._provider,
                    model=str(model) if model else None,
                    operation=request.url.path,
                    request=request_body,
                    response=_decode_response_body(response.content),
                    timing=_completed_timings(timings, started, queue_wait_seconds),
                )
                return response
            response.stream = _LoggingStream(
                response.stream,
                logger=self._logger,
                provider=self._provider,
                model=str(model) if model else None,
                operation=request.url.path,
                request_body=request_body,
                started=started,
                timings=timings,
                queue_wait_seconds=queue_wait_seconds,
            )
            return response
        except BaseException as exc:
            self._logger.write(
                provider=self._provider,
                model=str(model) if model else None,
                operation=request.url.path,
                request=request_body,
                error=exc,
                timing=_completed_timings(timings, started, queue_wait_seconds),
            )
            raise

    async def aclose(self) -> None:
        await self._transport.aclose()


class _LoggingStream(httpx.AsyncByteStream):
    """Copies response bytes for audit logging without delaying the consumer."""

    def __init__(
        self,
        stream: httpx.AsyncByteStream,
        *,
        logger: LLMCallLogger,
        provider: str,
        model: str | None,
        operation: str,
        request_body: Any,
        started: float,
        timings: dict[str, float | bool | None],
        queue_wait_seconds: float | None,
    ) -> None:
        self._stream = stream
        self._logger = logger
        self._provider = provider
        self._model = model
        self._operation = operation
        self._request_body = request_body
        self._chunks: list[bytes] = []
        self._logged = False
        self._started = started
        self._timings = timings
        self._queue_wait_seconds = queue_wait_seconds

    async def __aiter__(self):
        try:
            async for chunk in self._stream:
                # A body byte may be an SSE heartbeat or metadata, not a model token.
                if chunk and self._timings["first_byte_seconds"] is None:
                    self._timings["first_byte_seconds"] = perf_counter() - self._started
                self._chunks.append(chunk)
                yield chunk
        except BaseException as exc:
            self._log(error=exc)
            raise
        else:
            self._timings["response_complete"] = True
            self._log()

    async def aclose(self) -> None:
        try:
            await self._stream.aclose()
        finally:
            self._log()

    def _log(self, error: BaseException | None = None) -> None:
        if self._logged:
            return
        self._logged = True
        self._logger.write(
            provider=self._provider,
            model=self._model,
            operation=self._operation,
            request=self._request_body,
            response=_decode_response_body(b"".join(self._chunks)),
            error=error,
            timing=_completed_timings(self._timings, self._started, self._queue_wait_seconds),
        )


def _completed_timings(
    timings: dict[str, float | bool | None], started: float, queue_wait_seconds: float | None,
) -> dict[str, float | bool | None]:
    duration = perf_counter() - started
    return {
        **timings, "duration_seconds": duration,
        "total_duration_seconds": (queue_wait_seconds or 0.0) + duration,
    }


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


class ScheduledTransport(httpx.AsyncBaseTransport):
    """Holds the inference lane until a buffered or streamed response is consumed."""

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport,
        scheduler: InferenceScheduler,
        profile_id: str | None = None,
    ) -> None:
        self._transport = transport
        self._scheduler = scheduler
        self._profile_id = profile_id

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not _is_llm_endpoint(request.url.path):
            return await self._transport.handle_async_request(request)

        body = _decode_body(request.content)
        model = body.get("model") if isinstance(body, dict) else None
        endpoint = next(suffix for suffix in ("/chat/completions", "/embeddings", "/responses")
                        if request.url.path.endswith(suffix))
        server_url = str(request.url.copy_with(
            path=request.url.path.removesuffix(endpoint), query=None, fragment=None,
        )).rstrip("/")
        lease = self._scheduler.request(
            profile_id=self._profile_id, model=model, server_url=server_url,
        )
        queued = perf_counter()
        await lease.__aenter__()
        request.extensions["scholarweave_queue_wait_seconds"] = perf_counter() - queued
        try:
            response = await self._transport.handle_async_request(request)
        except BaseException:
            await lease.release()
            raise
        if response.is_closed:
            await lease.release()
        else:
            response.stream = _ScheduledStream(response.stream, lease)
        return response

    async def aclose(self) -> None:
        await self._transport.aclose()


class _ScheduledStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, lease: Any) -> None:
        self._stream = stream
        self._lease = lease
        self._closed = False

    async def __aiter__(self):
        try:
            async for chunk in self._stream:
                yield chunk
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._stream.aclose()
        finally:
            await self._lease.release()


def _is_llm_endpoint(path: str) -> bool:
    return path.endswith(("/chat/completions", "/embeddings", "/responses"))


def logged_http_client(
    settings: Settings,
    provider: str,
    request_lock: anyio.Lock | None = None,
    inference_scheduler: InferenceScheduler | None = None,
    profile_id: str | None = None,
) -> httpx.AsyncClient:
    transport: httpx.AsyncBaseTransport = LoggingTransport(
        LLMCallLogger(settings.llm_log_path),
        provider,
    )
    if request_lock is not None:
        transport = LockedTransport(transport, request_lock)
    if inference_scheduler is not None:
        transport = ScheduledTransport(transport, inference_scheduler, profile_id)
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
