from __future__ import annotations

import json

import httpx
import pytest

from backend.observability.llm_logging import (
    LLMCallLogger,
    LoggingTransport,
    _decode_response_body,
)


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
