"""A tiny OpenAI-compatible server for deterministic SDK integration tests.

Implements enough of ``/v1/chat/completions`` to exercise plain replies, streaming,
tool calls, model discovery, and embeddings without a live provider.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _split(text: str, size: int = 12) -> list[str]:
    """Chops a reply into a few deltas so token streaming is genuinely exercised."""
    return [text[start : start + size] for start in range(0, len(text), size)] or [""]


def _message(content: str | None = None, tool_calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "id": "chatcmpl-stub",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "stub-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content, "tool_calls": tool_calls},
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class StubProvider:
    """Records requests and replies according to a scripted plan."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.reply = "Stub answer."
        self.call_tool: str | None = None
        self.tool_arguments: dict[str, Any] = {}
        self.tool_plans: list[tuple[str, str, dict[str, Any]]] = []
        self.tool_plan_cursor = 0
        self.base_url = ""

    def _reply_for(self, _payload: dict[str, Any]) -> str:
        return self.reply

    def responses(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(payload)
        prompt = json.dumps(payload.get("messages") or [])
        called_tools = [
            call.get("function", {}).get("name")
            for message in payload.get("messages", [])
            for call in message.get("tool_calls") or []
        ]
        already_called = bool(called_tools)
        offered_tools = {
            (tool.get("function") or {}).get("name")
            for tool in payload.get("tools") or []
        }
        matching_plans = [
            (tool_name, arguments)
            for needle, tool_name, arguments in self.tool_plans
            if needle in prompt and tool_name in offered_tools
        ]
        planned_tool = (
            matching_plans[self.tool_plan_cursor]
            if self.tool_plan_cursor < len(matching_plans)
            else None
        )
        configured_tool = self.call_tool if self.call_tool in offered_tools else None
        tool_name = configured_tool or (planned_tool[0] if planned_tool else None)
        tool_arguments = self.tool_arguments if self.call_tool else (planned_tool[1] if planned_tool else {})
        if tool_name and (planned_tool is not None or not already_called):
            if planned_tool is not None:
                self.tool_plan_cursor += 1
            return _message(
                tool_calls=[
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": tool_name, "arguments": json.dumps(tool_arguments)},
                    }
                ]
            )
        return _message(self._reply_for(payload))

    def stream(self, payload: dict[str, Any]) -> str:
        """Re-renders a scripted reply as the SSE chunks the SDK's streaming path expects."""
        completion = self.responses(payload)
        message = completion["choices"][0]["message"]
        head = {
            "id": completion["id"],
            "object": "chat.completion.chunk",
            "created": completion["created"],
            "model": completion["model"],
        }
        lines: list[str] = []

        def chunk(delta: dict[str, Any], finish: str | None = None) -> None:
            body = {**head, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            lines.append(f"data: {json.dumps(body)}\n\n")

        chunk({"role": "assistant"})
        if message.get("tool_calls"):
            for index, call in enumerate(message["tool_calls"]):
                chunk({"tool_calls": [{"index": index, **call}]})
            chunk({}, "tool_calls")
        else:
            for piece in _split(message.get("content") or ""):
                chunk({"content": piece})
            chunk({}, "stop")
        lines.append("data: [DONE]\n\n")
        return "".join(lines)

    @property
    def tools_offered(self) -> list[str]:
        names: list[str] = []
        for request in self.requests:
            for tool in request.get("tools") or []:
                name = (tool.get("function") or {}).get("name")
                if name and name not in names:
                    names.append(name)
        return names


@pytest.fixture()
def stub_provider():
    """Runs the stub on a real socket, because the SDK builds its own HTTP client."""
    provider = StubProvider()
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        payload = await request.json()
        if payload.get("stream"):
            return StreamingResponse(iter([provider.stream(payload)]), media_type="text/event-stream")
        return provider.responses(payload)

    @app.get("/v1/models")
    async def models() -> dict[str, Any]:
        return {"object": "list", "data": [{"id": "stub-model", "object": "model"}]}

    @app.post("/v1/embeddings")
    async def embeddings(request: Request) -> dict[str, Any]:
        payload = await request.json()
        provider.requests.append(payload)
        return {
            "object": "list",
            "model": payload.get("model", "stub-model"),
            "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        }

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        server.should_exit = True
        pytest.skip("Stub provider did not start in time")
    provider.base_url = f"http://127.0.0.1:{port}"
    try:
        yield provider
    finally:
        server.should_exit = True
        thread.join(timeout=10)
