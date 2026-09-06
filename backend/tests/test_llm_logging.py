from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from backend.providers.logging import (
    LLMCallLogger,
    LoggingTransport,
    ScheduledTransport,
    _decode_response_body,
)
from backend.providers.inference import InferenceScheduler


def test_decodes_streaming_chat_response_as_a_complete_json_response() -> None:
    chunks = [
        {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "test-model",
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hello"}, "finish_reason": None}],
        },
        {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "test-model",
            "choices": [{"index": 0, "delta": {"content": " world", "reasoning": "Brief thought"}, "finish_reason": None}],
        },
        {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "test-model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
    ]
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"

    response = _decode_response_body(body.encode())

    assert response == {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Hello world",
                    "reasoning": "Brief thought",
                    "parts": [
                        {"type": "content", "text": "Hello world"},
                        {"type": "reasoning", "text": "Brief thought"},
                    ],
                },
                "finish_reason": "stop",
            }
        ],
    }


def test_preserves_interleaved_reasoning_and_content_parts() -> None:
    chunks = [
        {"choices": [{"index": 0, "delta": {"reasoning": "First thought. "}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"content": "First answer. "}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"reasoning": "Second thought. "}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"content": "Second answer."}, "finish_reason": "stop"}]},
    ]
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"

    response = _decode_response_body(body.encode())

    assert response["choices"][0]["message"]["parts"] == [
        {"type": "reasoning", "text": "First thought. "},
        {"type": "content", "text": "First answer. "},
        {"type": "reasoning", "text": "Second thought. "},
        {"type": "content", "text": "Second answer."},
    ]


def test_combines_streamed_tool_call_arguments() -> None:
    chunks = [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "id": "call_1", "type": "function", "function": {"name": "search", "arguments": "{\"q\":"}}
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "\"paper\"}"}}]},
                    "finish_reason": "tool_calls",
                }
            ]
        },
    ]
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"

    response = _decode_response_body(body.encode())

    assert response["choices"][0] == {
        "index": 0,
        "message": {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "search", "arguments": '{"q":"paper"}'},
                }
            ]
        },
        "finish_reason": "tool_calls",
    }


@pytest.mark.anyio
async def test_logging_transport_persists_normalized_stream_response(tmp_path) -> None:
    stream_body = (
        'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","choices":'
        '[{"index":0,"delta":{"role":"assistant","content":"Hello"},"finish_reason":null}]}\n\n'
        'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","choices":'
        '[{"index":0,"delta":{"content":" world"},"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n"
    )

    async def provider(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=stream_body, headers={"content-type": "text/event-stream"})

    log_path = tmp_path / "llm_calls.jsonl"
    transport = LoggingTransport(LLMCallLogger(log_path), "test", httpx.MockTransport(provider))
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.post("https://provider.test/v1/chat/completions", json={"model": "test-model"})

    assert response.text == stream_body
    logged = json.loads(log_path.read_text())
    assert logged["response"]["object"] == "chat.completion"
    assert logged["response"]["choices"][0]["message"]["content"] == "Hello world"
    assert logged["timing"]["first_byte_seconds"] is None
    assert logged["timing"]["queue_wait_seconds"] is None
    assert logged["timing"]["response_complete"] is True


@pytest.mark.anyio
async def test_logging_transport_does_not_buffer_streaming_response(tmp_path) -> None:
    release_second_chunk = asyncio.Event()

    class ProviderStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"first"
            await release_second_chunk.wait()
            yield b"second"

    async def provider(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=ProviderStream(),
            headers={"content-type": "text/event-stream"},
        )

    log_path = tmp_path / "llm_calls.jsonl"
    transport = LoggingTransport(LLMCallLogger(log_path), "test", httpx.MockTransport(provider))
    async with httpx.AsyncClient(transport=transport) as client:
        async with client.stream(
            "POST",
            "https://provider.test/v1/chat/completions",
            json={"model": "test-model"},
        ) as response:
            chunks = response.aiter_bytes()
            assert await anext(chunks) == b"first"
            assert not log_path.exists()
            release_second_chunk.set()
            assert await anext(chunks) == b"second"
            with pytest.raises(StopAsyncIteration):
                await anext(chunks)

    assert json.loads(log_path.read_text())["response"] == "firstsecond"


@pytest.mark.anyio
async def test_logs_queue_request_and_observed_body_byte_timings_without_credentials(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("backend.providers.logging.perf_counter", lambda: clock[0])
    scheduler = InferenceScheduler()
    lease = scheduler.request()
    await lease.__aenter__()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            clock[0] = 0.09
            yield b": heartbeat\n\n"
            clock[0] = 0.12
            yield b"data: [DONE]\n\n"
            clock[0] = 0.15

    def provider(_request):
        clock[0] = 0.05
        return httpx.Response(200, stream=Stream())

    path = tmp_path / "metrics.jsonl"
    transport = ScheduledTransport(
        LoggingTransport(LLMCallLogger(path), "test", httpx.MockTransport(provider)), scheduler,
    )
    async with httpx.AsyncClient(transport=transport) as client:
        task = asyncio.create_task(client.post(
            "http://stub/v1/chat/completions", headers={"Authorization": "Bearer secret-header"},
            json={"model": "main", "api_key": "secret-body"},
        ))
        await asyncio.sleep(0)
        clock[0] = 0.02
        await lease.release()
        await task
    logged = json.loads(path.read_text())
    metrics = logged["timing"]
    assert metrics["queue_wait_seconds"] == pytest.approx(0.02)
    assert metrics["response_headers_seconds"] == pytest.approx(0.03)
    assert metrics["first_byte_seconds"] == pytest.approx(0.07)
    assert metrics["duration_seconds"] == pytest.approx(0.13)
    assert metrics["total_duration_seconds"] == pytest.approx(0.15)
    assert metrics["response_complete"] is True
    assert "token" not in " ".join(metrics)
    assert "secret-header" not in path.read_text()
    assert "secret-body" not in path.read_text()


@pytest.mark.anyio
async def test_interrupted_stream_logs_incomplete_timing_once(tmp_path):
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"first"
            raise asyncio.CancelledError()

    path = tmp_path / "metrics.jsonl"
    transport = LoggingTransport(
        LLMCallLogger(path), "test",
        httpx.MockTransport(lambda _request: httpx.Response(200, stream=Stream())),
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(asyncio.CancelledError):
            await client.post("http://stub/v1/chat/completions", json={"model": "main"})
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    logged = json.loads(lines[0])
    assert logged["timing"]["response_complete"] is False
    assert logged["timing"]["first_byte_seconds"] is not None
    assert logged["error"]["type"] == "CancelledError"
