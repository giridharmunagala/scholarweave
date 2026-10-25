"""A tiny OpenAI-compatible server for deterministic harness integration tests.

Implements ``/v1/chat/completions`` and stateless ``/v1/responses`` for replies,
streaming, tool calls, reasoning replay, model discovery, and embeddings.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from typing import Any
from contextlib import contextmanager

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


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
        self.request_started = threading.Event()
        self.reply = "Stub answer."
        self.call_tool: str | None = None
        self.tool_arguments: dict[str, Any] = {}
        self.tool_plans: list[tuple[str, str, dict[str, Any]]] = []
        self.stream_delay_seconds = 0.0
        self.completion_delay_seconds = 0.0
        self.stream_gates: dict[str, threading.Event] = {}
        self.tool_plan_cursor = 0
        self.base_url = ""
        self.reject_reasoning_with_tools = False
        self.response_api_requests: list[dict[str, Any]] = []
        self.decision_requests: list[dict[str, Any]] = []
        self.request_order: list[str] = []
        self.decision_delay_seconds = 0.0
        self.active_inference = 0
        self.max_active_inference = 0
        self.tokenizer_enabled = False
        self.tokenizer_requests: list[dict[str, Any]] = []
        self.embedding_vectors: dict[str, list[float]] = {}

    def tokenize(self, payload: dict) -> dict:
        import tiktoken

        self.tokenizer_requests.append(payload)
        return {"tokens": tiktoken.get_encoding("cl100k_base").encode(payload["content"])}

    @contextmanager
    def inference(self):
        self.active_inference += 1
        self.max_active_inference = max(self.max_active_inference, self.active_inference)
        try:
            yield
        finally:
            self.active_inference -= 1

    def decisions(self, payload: dict) -> dict:
        self.decision_requests.append(payload)
        self.request_order.append(payload["model"])
        answers = {}
        for name, question in payload["questions"].items():
            kind = question["type"]
            if kind == "noul":
                answers[name] = {"type": kind, "noul": 0.8}
                continue
            keys = list(question["criteria"]) if kind == "choice" else [str(index) for index in range(len(question["criteria"]))]
            answer = {"type": kind, "probabilities": {key: 1.0 if index == 0 else 0.0 for index, key in enumerate(keys)}, "confidence": 1.0}
            answer.update({"choice": keys[0]} if kind == "choice" else {"score": 0.0})
            answers[name] = answer
        return {"answers": answers, "usage": {"input_tokens": 100, "output_tokens": 0}}

    def _reply_for(self, _payload: dict[str, Any]) -> str:
        return self.reply

    async def wait_for_stream(self, payload: dict) -> None:
        gate = self.stream_gates.get(payload.get("model"))
        if gate is not None and not await asyncio.to_thread(gate.wait, 10):
            raise TimeoutError("The test did not release the provider response stream.")
        if self.stream_delay_seconds:
            await asyncio.sleep(self.stream_delay_seconds)

    def responses(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(payload)
        self.request_order.append(payload.get("model", "stub-model"))
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
        """Re-render a scripted reply as Chat Completions SSE chunks."""
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

    def response_api(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.response_api_requests.append(payload)
        messages: list[dict[str, Any]] = []
        for item in payload.get("input") or []:
            if item.get("type") == "function_call":
                messages.append({
                    "role": "assistant", "content": None,
                    "tool_calls": [{
                        "id": item["call_id"], "type": "function",
                        "function": {"name": item["name"], "arguments": item["arguments"]},
                    }],
                })
            elif item.get("type") == "function_call_output":
                messages.append({"role": "tool", "tool_call_id": item["call_id"], "content": item["output"]})
            elif "role" in item:
                messages.append({"role": item["role"], "content": item.get("content")})
        completion = self.responses({
            **payload, "messages": messages,
            "tools": [{"type": "function", "function": tool} for tool in payload.get("tools") or []],
        })
        message = completion["choices"][0]["message"]
        output: list[dict[str, Any]] = [{
            "type": "reasoning", "id": "rs_stub",
            "summary": [{"type": "summary_text", "text": "Checked the supplied evidence."}],
            "encrypted_content": "opaque-stub-reasoning",
        }]
        if message.get("tool_calls"):
            output.extend({
                "type": "function_call", "id": f"fc_{index}",
                "call_id": call["id"], "name": call["function"]["name"],
                "arguments": call["function"]["arguments"], "status": "completed",
            } for index, call in enumerate(message["tool_calls"]))
        else:
            output.append({
                "type": "message", "id": "msg_stub", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": message.get("content") or "", "annotations": []}],
            })
        return {
            "id": "resp_stub", "object": "response", "created_at": int(time.time()),
            "status": "completed", "model": payload.get("model", "stub-model"),
            "output": output, "error": None, "incomplete_details": None,
            "usage": {
                "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                "input_tokens_details": {"cached_tokens": 4},
                "output_tokens_details": {"reasoning_tokens": 2},
            },
        }

    def response_api_stream(self, payload: dict[str, Any]) -> str:
        response = self.response_api(payload)
        lines: list[str] = []

        def event(kind: str, **data: Any) -> None:
            body = {"type": kind, "sequence_number": len(lines), **data}
            lines.append(f"event: {kind}\ndata: {json.dumps(body)}\n\n")

        event("response.created", response={**response, "status": "in_progress", "output": [], "usage": None})
        for index, item in enumerate(response["output"]):
            event("response.output_item.added", output_index=index, item={
                **item, **({"arguments": ""} if item["type"] == "function_call" else {}),
            })
            if item["type"] == "reasoning":
                event("response.reasoning_summary_text.delta", item_id=item["id"], output_index=index,
                      summary_index=0, delta=item["summary"][0]["text"])
            elif item["type"] == "function_call":
                for piece in _split(item["arguments"]):
                    event("response.function_call_arguments.delta", item_id=item["id"], output_index=index, delta=piece)
                event("response.function_call_arguments.done", item_id=item["id"], output_index=index, arguments=item["arguments"])
            elif item["type"] == "message":
                for piece in _split(item["content"][0]["text"]):
                    event("response.output_text.delta", item_id=item["id"], output_index=index,
                          content_index=0, delta=piece, logprobs=[])
                event("response.output_text.done", item_id=item["id"], output_index=index, content_index=0,
                      text=item["content"][0]["text"], logprobs=[])
            event("response.output_item.done", output_index=index, item=item)
        event("response.completed", response=response)
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


def _provider_app(provider: StubProvider) -> FastAPI:
    app = FastAPI()

    @app.post("/tokenize")
    async def tokenize(request: Request):
        if not provider.tokenizer_enabled:
            return JSONResponse(status_code=404, content={"error": {"message": "Tokenizer unavailable"}})
        return provider.tokenize(await request.json())

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        payload = await request.json()
        provider.request_started.set()
        if (
            provider.reject_reasoning_with_tools
            and payload.get("tools")
            and payload.get("reasoning_effort") != "none"
        ):
            return JSONResponse(status_code=400, content={"error": {
                "message": "Function tools with reasoning-effort are not supported",
                "type": "invalid_request_error", "param": "reasoning_effort", "code": None,
            }})
        if payload.get("stream"):
            async def chunks():
                with provider.inference():
                    await provider.wait_for_stream(payload)
                    yield provider.stream(payload)

            return StreamingResponse(chunks(), media_type="text/event-stream")
        with provider.inference():
            if provider.completion_delay_seconds:
                await asyncio.sleep(provider.completion_delay_seconds)
            return provider.responses(payload)

    @app.post("/v1/systemone")
    async def decisions(request: Request):
        payload = await request.json()
        provider.request_started.set()
        with provider.inference():
            if provider.decision_delay_seconds:
                await asyncio.sleep(provider.decision_delay_seconds)
            return provider.decisions(payload)

    @app.get("/v1/models")
    async def models() -> dict[str, Any]:
        return {"object": "list", "data": [{"id": "stub-model", "object": "model"}]}

    @app.post("/v1/responses")
    async def response_api(request: Request):
        payload = await request.json()
        provider.request_started.set()
        if payload.get("stream"):
            async def chunks():
                with provider.inference():
                    await provider.wait_for_stream(payload)
                    yield provider.response_api_stream(payload)

            return StreamingResponse(chunks(), media_type="text/event-stream")
        with provider.inference():
            return provider.response_api(payload)

    @app.post("/v1/embeddings")
    async def embeddings(request: Request) -> dict[str, Any]:
        payload = await request.json()
        provider.requests.append(payload)
        return {
            "object": "list",
            "model": payload.get("model", "stub-model"),
            "data": [{"object": "embedding", "index": index,
                      "embedding": provider.embedding_vectors.get(text, [0.1, 0.2, 0.3])}
                     for index, text in enumerate(payload["input"] if isinstance(payload["input"], list) else [payload["input"]])],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        }

    return app


class _ProviderServer:
    """Reuse an event-loop thread while keeping each test's HTTP server isolated."""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stop: asyncio.Event | None = None
        self.ready = threading.Event()

    async def serve(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.stop = asyncio.Event()
        self.ready.set()
        await self.stop.wait()

    def run(self) -> None:
        # Windows Proactor reset errors can skip listener detachment indefinitely.
        # This socket-only thread needs neither Proactor subprocesses nor pipes.
        loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
        with asyncio.Runner(loop_factory=loop_factory) as runner:
            runner.run(self.serve())

    async def _start(self, provider: StubProvider) -> uvicorn.Server:
        config = uvicorn.Config(
            _provider_app(provider), host="127.0.0.1", port=0, log_level="error",
            timeout_graceful_shutdown=1,
        )
        config.load()
        server = uvicorn.Server(config)
        server.lifespan = config.lifespan_class(config)
        await server.startup()
        if not server.started:
            raise RuntimeError("Stub provider did not start")
        await server.on_tick(0)
        port = server.servers[0].sockets[0].getsockname()[1]
        provider.base_url = f"http://127.0.0.1:{port}"
        return server

    async def _close(self, server: uvicorn.Server) -> None:
        # Uvicorn's CLI shutdown polls and always sleeps 100ms, even with no work.
        # Await the actual listeners/tasks instead; retain its one-second grace.
        for listener in server.servers:
            listener.close()
        for connection in list(server.server_state.connections):
            connection.shutdown()
        tasks = set(server.server_state.tasks)
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=server.config.timeout_graceful_shutdown)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.gather(*(listener.wait_closed() for listener in server.servers))
        await server.lifespan.shutdown()

    @contextmanager
    def provider(self):
        provider = StubProvider()
        server = asyncio.run_coroutine_threadsafe(self._start(provider), self.loop).result(timeout=15)
        try:
            yield provider
        finally:
            asyncio.run_coroutine_threadsafe(self._close(server), self.loop).result(timeout=10)


@pytest.fixture(scope="session")
def _stub_server():
    runner = _ProviderServer()
    thread = threading.Thread(target=runner.run, daemon=True)
    thread.start()
    try:
        if not runner.ready.wait(timeout=15):
            pytest.fail("Stub provider event loop did not start in time")
        yield runner
    finally:
        if runner.loop is not None:
            runner.loop.call_soon_threadsafe(runner.stop.set)
        thread.join(timeout=10)
        if thread.is_alive():
            pytest.fail("Stub provider event loop did not stop in time")


@pytest.fixture()
def stub_provider(_stub_server):
    """Use real HTTP with a fresh socket, app and script for every test."""
    with _stub_server.provider() as provider:
        yield provider
