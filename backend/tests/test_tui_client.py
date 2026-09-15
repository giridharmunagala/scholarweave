from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import httpx
import pytest

from backend.agents.blueprint import ModelReferenceSpec
from backend.conversations.schemas import (
    ConversationDetailResponse,
    ConversationMessageResponse,
    ConversationResponse,
)
from backend.research.schemas import DocumentResponse, DocumentSummaryResponse
from backend.prompting.schemas import SkillResponse
from backend.runs.schemas import RunResponse, SteeringMessageResponse
from backend.tests.test_tui_app import settings_payload
from backend.workspace.schemas import (
    WorkspaceFileContentResponse,
    WorkspaceFileResponse,
    WorkspaceSearchResponse,
)
from scholarweave_tui.client import ApiError, ScholarWeaveClient, StreamEvent

STAMP = "2026-09-12T12:00:00Z"


def run_data() -> dict[str, Any]:
    return {
        "id": "run-1", "conversation_id": "chat-1", "agent_name": "Main",
        "status": "running", "input": "Question", "final_output": None,
        "last_agent_name": None, "usage": {}, "error": None, "cancel_requested": False,
        "created_at": STAMP, "started_at": STAMP, "finished_at": None,
        "items": [], "events": [], "epochs": [], "tool_attempts": [], "goal_state": None,
    }


def note_data(index: int = 0) -> dict[str, Any]:
    return {
        "path": f"notes/note-{index}/notes.md", "name": f"Note {index}",
        "media_type": "text/markdown", "size_bytes": 6, "modified_at": STAMP,
        "tags": ["keep"], "kind": "note",
    }


def test_client_contracts_use_existing_routes_models_and_explicit_message_effort() -> None:
    conversation = {
        "id": "chat-1", "title": "New chat", "kind": "autonomous",
        "model_reference": {}, "session_policy": {}, "status": "active",
        "last_message_preview": "", "created_at": STAMP, "updated_at": STAMP,
    }
    document = {
        "id": "paper-1", "title": "Paper", "source_filename": "paper.pdf",
        "content_type": "application/pdf", "status": "ready", "page_count": 1,
        "metadata": {}, "created_at": STAMP, "updated_at": STAMP,
    }
    note = {**note_data(), "content": "Saved"}
    skill = {
        "name": "web-synthesis", "content": "# Web synthesis", "source": "local", "revision": "a" * 64,
    }
    provider = {
        "id": "provider-1", "name": "Local runtime", "kind": "openai_compatible",
        "base_url": "http://127.0.0.1:11434/v1", "api_key_set": False, "state": "active",
        "models": [{
            "name": "weave-deep", "capabilities": ["chat"], "reasoning_efforts": ["low", "high"],
            "preserve_thinking": False, "context_window_tokens": None, "enabled": True,
        }],
        "serialize_model_switches": False, "created_at": STAMP, "updated_at": STAMP,
    }
    settings = settings_payload({"provider_profile_id": "provider-1", "model": "weave-deep"})
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        route = (request.method, request.url.path)
        responses = {
            ("GET", "/api/agent/conversations"): [conversation],
            ("POST", "/api/agent/conversations"): conversation,
            ("GET", "/api/agent/conversations/chat-1"): {**conversation, "items": []},
            ("POST", "/api/agent/conversations/chat-1/messages"): {
                "conversation": conversation, "run": run_data(),
            },
            ("GET", "/api/runs"): [run_data()],
            ("GET", "/api/runs/run-1"): run_data(),
            ("POST", "/api/runs/run-1/cancel"): {**run_data(), "status": "cancelled"},
            ("POST", "/api/runs/run-1/steering"): {
                "id": "steer-1", "content": "Narrow the question", "status": "queued",
            },
            ("GET", "/api/documents"): [document],
            ("GET", "/api/documents/paper-1"): {**document, "artifacts": [], "chunks": []},
            ("GET", "/api/providers"): [provider],
            ("POST", "/api/providers"): provider,
            ("PUT", "/api/providers/provider-1"): provider,
            ("GET", "/api/providers/provider-1/models"): {
                "models": provider["models"], "discovery_error": None,
            },
            ("GET", "/api/settings"): settings,
            ("PUT", "/api/settings"): settings,
            ("GET", "/api/workspace/files/content"): note,
            ("PUT", "/api/workspace/files/content"): note,
            ("POST", "/api/workspace/files/notes"): note,
            ("GET", "/api/skills"): [skill],
            ("GET", "/api/skills/web-synthesis"): skill,
            ("PUT", "/api/skills/web-synthesis"): skill,
        }
        assert route in responses
        return httpx.Response(200, json=responses[route])

    async def scenario() -> None:
        client = ScholarWeaveClient(transport=httpx.MockTransport(respond))
        try:
            assert client.base_url == "http://127.0.0.1:8000"
            assert isinstance((await client.conversations())[0], ConversationResponse)
            assert isinstance(await client.conversation("chat-1"), ConversationDetailResponse)
            assert isinstance(await client.create_conversation(), ConversationResponse)
            assert json.loads(requests[-1].content) == {}
            reference = ModelReferenceSpec(provider_profile_id="provider-1", model="weave-deep")
            await client.create_conversation(reference)
            assert json.loads(requests[-1].content) == {
                "model_reference": {"provider_profile_id": "provider-1", "model": "weave-deep"},
            }
            assert (await client.providers())[0].models[0].reasoning_efforts == ["low", "high"]
            from backend.providers.schemas import ProviderCreate, ProviderUpdate
            assert (
                await client.create_provider(ProviderCreate(
                    name="Local runtime",
                    kind="openai_compatible",
                    base_url="http://127.0.0.1:11434/v1",
                ))
            ).id == "provider-1"
            assert json.loads(requests[-1].content) == {
                "name": "Local runtime",
                "kind": "openai_compatible",
                "base_url": "http://127.0.0.1:11434/v1",
                "api_key": None,
                "models": [],
                "serialize_model_switches": None,
            }
            assert (
                await client.update_provider(
                    "provider-1", ProviderUpdate(name="Renamed runtime"),
                )
            ).id == "provider-1"
            assert json.loads(requests[-1].content) == {"name": "Renamed runtime"}
            assert (
                await client.discover_provider_models("provider-1")
            ).models[0].name == "weave-deep"
            assert (await client.settings()).last_chat_model_reference == reference
            await client.save_chat_model(reference)
            assert json.loads(requests[-1].content) == {
                "last_chat_model_reference": {
                    "provider_profile_id": "provider-1", "model": "weave-deep",
                },
            }
            assert isinstance(
                await client.send_message("chat-1", "Hello"), ConversationMessageResponse,
            )
            assert json.loads(requests[-1].content) == {
                "content": "Hello", "response_effort": "auto", "web_enabled": True,
            }
            await client.send_message("chat-1", "Local only", effort="thorough", web_enabled=False)
            assert json.loads(requests[-1].content) == {
                "content": "Local only", "response_effort": "thorough", "web_enabled": False,
            }
            await client.send_message("chat-1", "Think hard", reasoning_effort="high")
            assert json.loads(requests[-1].content) == {
                "content": "Think hard", "response_effort": "auto", "web_enabled": True,
                "reasoning_effort": "high",
            }
            await client.send_message(
                "chat-1", "Use more context", context_window_tokens=65_536,
            )
            assert json.loads(requests[-1].content) == {
                "content": "Use more context",
                "response_effort": "auto",
                "web_enabled": True,
                "context_window_tokens": 65_536,
            }
            assert isinstance((await client.runs())[0], RunResponse)
            assert not requests[-1].url.query
            await client.runs("chat-1")
            assert dict(requests[-1].url.params) == {"conversation_id": "chat-1"}
            assert isinstance(await client.run("run-1"), RunResponse)
            assert (await client.cancel_run("run-1")).status == "cancelled"
            assert isinstance(
                await client.steer_run("run-1", "Narrow the question"), SteeringMessageResponse,
            )
            assert json.loads(requests[-1].content) == {"content": "Narrow the question"}
            assert isinstance((await client.papers())[0], DocumentSummaryResponse)
            assert isinstance(await client.paper("paper-1"), DocumentResponse)
            path = "notes/space & #unicode-é/notes.md"
            assert isinstance(await client.read_note(path), WorkspaceFileContentResponse)
            assert dict(requests[-1].url.params) == {"path": path}
            await client.save_note(path, "Saved", expected_sha256="a" * 64)
            assert json.loads(requests[-1].content) == {
                "path": path, "content": "Saved", "expected_sha256": "a" * 64,
            }
            assert (await client.create_note("Ideas")).tags == ["keep"]
            assert json.loads(requests[-1].content) == {"name": "Ideas", "content": ""}
            await client.create_note("Ideas", "Opening")
            assert json.loads(requests[-1].content) == {"name": "Ideas", "content": "Opening"}
            assert isinstance((await client.skills())[0], SkillResponse)
            assert isinstance(await client.read_skill("web-synthesis"), SkillResponse)
            assert isinstance(
                await client.save_skill("web-synthesis", "# Web synthesis", "a" * 64), SkillResponse,
            )
            assert json.loads(requests[-1].content) == {
                "content": "# Web synthesis", "expected_revision": "a" * 64,
            }
            await client.save_skill("web-synthesis", "# New skill", None)
            assert json.loads(requests[-1].content)["expected_revision"] is None
        finally:
            await client.close()
        assert client._http.is_closed

    asyncio.run(scenario())


def test_notes_and_search_paginate_and_preserve_search_kinds() -> None:
    pages: list[tuple[str, int]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        offset = int(params["offset"])
        assert params["limit"] == "100"
        pages.append((request.url.path, offset))
        searching = request.url.path.endswith("/search")
        if searching:
            assert params["query"] == "quantum & resonance"
            assert params.get_list("kinds") == ["note", "paper_notes"]
        total = 101 if searching else 200
        return httpx.Response(200, json=[
            {**note_data(index), **({"score": 1.2, "excerpt": "Found"} if searching else {})}
            for index in range(offset, min(offset + 100, total))
        ])

    async def scenario() -> None:
        client = ScholarWeaveClient(transport=httpx.MockTransport(respond))
        try:
            notes = await client.notes()
            assert len(notes) == 200
            assert all(isinstance(note, WorkspaceFileResponse) for note in notes)
            found = await client.search_notes("quantum & resonance")
            assert len(found) == 101
            assert all(isinstance(note, WorkspaceSearchResponse) for note in found)
            assert len({note.path for note in found}) == 101
        finally:
            await client.close()

    asyncio.run(scenario())
    assert pages == [
        ("/api/workspace/notes", 0), ("/api/workspace/notes", 100),
        ("/api/workspace/notes", 200), ("/api/workspace/search", 0),
        ("/api/workspace/search", 100),
    ]


@pytest.mark.parametrize("url,normalized", [
    ("http://127.0.0.1:8000/", "http://127.0.0.1:8000"),
    ("http://localhost", "http://localhost"),
    ("http://LOCALHOST:80", "http://localhost:80"),
    ("http://[::1]:65535", "http://[::1]:65535"),
    ("http://[::1]/", "http://[::1]"),
])
def test_only_loopback_origins_are_accepted(url: str, normalized: str) -> None:
    async def scenario() -> None:
        client = ScholarWeaveClient(url)
        try:
            assert client.base_url == normalized
            assert not client._http.trust_env
            assert not client._http.follow_redirects
        finally:
            await client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("url", [
    "https://localhost:8000", "http://example.com", "http://0.0.0.0:8000",
    "http://127.0.0.2", "http://localhost.example.com", "http://localhost.",
    "http://127.1", "http://2130706433", "http://[::ffff:127.0.0.1]",
    "http://name:password@localhost:8000", "http://@localhost", "http://localhost/api",
    "http://localhost/api/", "http://localhost//", "http://localhost/../",
    "http://localhost/%2fapi", "http://localhost?x=1", "http://localhost?",
    "http://localhost#anchor", "http://localhost#", "http://localhost:0",
    "http://localhost:65536", "http://localhost:-1", "http://localhost:+80",
    "http://localhost:", "http://localhost:abc", "http://[::1", "//localhost",
    "localhost:8000", " http://localhost", "http://local\nhost", "http://localhost\\api",
])
def test_nonlocal_or_nonroot_urls_are_rejected(url: str) -> None:
    with pytest.raises(ValueError, match="API URL"):
        ScholarWeaveClient(url)


@pytest.mark.parametrize("status,body,expected", [
    (404, {"detail": "Conversation missing"}, "Conversation missing"),
    (422, {"detail": [
        {"loc": ["body", "content"], "msg": "Field required", "type": "missing"},
    ]}, '["body", "content"]'),
    (500, "Backend failed", "Backend failed"),
    (307, "", "HTTP 307"),
])
def test_http_failures_include_backend_details_and_never_retry(
    status: int, body: Any, expected: str,
) -> None:
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        kwargs = {"json": body} if isinstance(body, dict) else {"text": body}
        return httpx.Response(status, headers={"Location": "http://example.com"}, **kwargs)

    async def scenario() -> None:
        client = ScholarWeaveClient(transport=httpx.MockTransport(respond))
        try:
            with pytest.raises(ApiError) as caught:
                await client.create_conversation()
            assert expected in str(caught.value)
            assert caught.value.status_code == status
        finally:
            await client.close()

    asyncio.run(scenario())
    assert len(requests) == 1


@pytest.mark.parametrize("exception,message", [
    (httpx.ConnectError, "Could not reach the local backend"),
    (httpx.ConnectTimeout, "timed out"),
    (httpx.ReadTimeout, "timed out"),
    (httpx.RemoteProtocolError, "Could not reach the local backend"),
])
def test_transport_errors_are_actionable_and_mutations_not_retried(exception, message: str) -> None:
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise exception("offline", request=request)

    async def scenario() -> None:
        client = ScholarWeaveClient(transport=httpx.MockTransport(respond))
        try:
            with pytest.raises(ApiError, match=message):
                await client.send_message("chat-1", "Question")
        finally:
            await client.close()

    asyncio.run(scenario())
    assert len(calls) == 1


@pytest.mark.parametrize("body,message", [
    (b"not json", "Invalid JSON response"),
    (b'{"unexpected":"object"}', "Invalid response"),
    (b'[{"id":"incomplete"}]', "Invalid response"),
    (b"null", "Invalid response"),
])
def test_malformed_json_and_response_schemas_become_api_errors(body: bytes, message: str) -> None:
    async def scenario() -> None:
        client = ScholarWeaveClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)),
        )
        try:
            with pytest.raises(ApiError, match=message):
                await client.conversations()
        finally:
            await client.close()

    asyncio.run(scenario())


class Chunks(httpx.AsyncByteStream):
    def __init__(self, content: bytes, *, failure: Exception | None = None) -> None:
        self.content = content
        self.failure = failure
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for start in range(0, len(self.content), 3):
            yield self.content[start:start + 3]
        if self.failure:
            raise self.failure

    async def aclose(self) -> None:
        self.closed = True


def frame(sequence: int, kind: str = "model.stream", **payload: Any) -> str:
    envelope = json.dumps(
        {"sequence": sequence, "event_type": kind, "payload": payload, "created_at": None},
        ensure_ascii=False,
    )
    return f"id: {sequence}\nevent: {kind}\ndata: {envelope}\n\n"


def test_sse_decodes_chunks_crlf_multiline_heartbeats_replay_and_buffered_eof() -> None:
    multiline = (
        'event: unknown.future.event\r\nid: 999\r\n'
        'data: {"sequence":3,\r\n'
        'data: "event_type":"future.event",\r\n'
        'data: "payload":{"text":"héllo 📚"}, "created_at":null}\r\n\r\n'
    )
    content = (
        "\ufeff: heartbeat\r\n\r\nretry: 15000\r\nid: 2\r\n\r\n"
        + frame(1) + frame(2) + multiline + frame(3) + frame(1)
        + ': heartbeat\n\n'
        + 'data: {"sequence":4,"event_type":"run.completed","payload":{}}'
    ).encode("utf-8")
    stream = Chunks(content)
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.path == "/api/runs/run-1/events"
        assert dict(request.url.params) == {"after": "2"}
        assert request.headers["accept"] == "text/event-stream"
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream)

    async def scenario() -> None:
        client = ScholarWeaveClient(transport=httpx.MockTransport(respond))
        try:
            events = [event async for event in client.events("run-1", after=2)]
            assert [event.sequence for event in events] == [3, 4]
            assert events[0].event_type == "future.event"
            assert events[0].payload == {"text": "héllo 📚"}
            assert all(event.created_at is None for event in events)
        finally:
            await client.close()

    asyncio.run(scenario())
    assert stream.closed
    assert len(calls) == 1


def test_sse_handles_cr_only_lines_and_timestamp() -> None:
    body = (
        'data:{"sequence":0,"event_type":"run.started","payload":{},'
        f'"created_at":"{STAMP}"}}\r\r: heartbeat\r\r'
    ).encode()
    stream = Chunks(body)

    async def scenario() -> None:
        client = ScholarWeaveClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"Content-Type": "text/event-stream; charset=utf-8"}, stream=stream,
            ),
        ))
        try:
            events = [event async for event in client.events("run-1")]
            assert len(events) == 1
            assert isinstance(events[0].created_at, datetime)
        finally:
            await client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("data", [
    "bad json", "[]", '{"sequence":1,"event_type":"run.started","payload":[]}',
    '{"sequence":true,"event_type":"run.started","payload":{}}',
    '{"sequence":1,"event_type":"run.started"}',
])
def test_sse_invalid_envelopes_raise_clear_errors(data: str) -> None:
    async def scenario() -> None:
        client = ScholarWeaveClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"Content-Type": "text/event-stream"}, text=f"data: {data}\n\n",
            ),
        ))
        try:
            with pytest.raises(ApiError, match="Invalid SSE event"):
                _ = [event async for event in client.events("run-1")]
        finally:
            await client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("status,headers,body,message", [
    (404, {}, '{"detail":"Run missing"}', "Run missing"),
    (200, {"Content-Type": "application/json"}, "[]", "Expected an SSE"),
    (307, {"Location": "http://example.com"}, "", "HTTP 307"),
])
def test_sse_checks_http_errors_and_content_type(status, headers, body, message) -> None:
    async def scenario() -> None:
        client = ScholarWeaveClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(status, headers=headers, text=body),
        ))
        try:
            with pytest.raises(ApiError, match=message):
                _ = [event async for event in client.events("run-1")]
        finally:
            await client.close()

    asyncio.run(scenario())


def test_sse_read_failure_preserves_delivered_cursor_and_closes_response() -> None:
    stream = Chunks(frame(7).encode())

    def respond(request: httpx.Request) -> httpx.Response:
        stream.failure = httpx.ReadTimeout("disconnected", request=request)
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream)

    async def scenario() -> None:
        client = ScholarWeaveClient(transport=httpx.MockTransport(respond))
        received = []
        try:
            with pytest.raises(ApiError, match="timed out"):
                async for event in client.events("run-1"):
                    received.append(event.sequence)
            assert received == [7]
            assert stream.closed
        finally:
            await client.close()

    asyncio.run(scenario())


def test_sse_can_be_closed_without_cancelling_the_backend_run() -> None:
    stream = Chunks((frame(1) + frame(2)).encode())
    methods = []

    def respond(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream)

    async def scenario() -> None:
        client = ScholarWeaveClient(transport=httpx.MockTransport(respond))
        try:
            events = client.events("run-1")
            assert (await anext(events)).sequence == 1
            await events.aclose()
            assert stream.closed
        finally:
            await client.close()

    asyncio.run(scenario())
    assert methods == ["GET"]


def test_stream_event_allows_nullable_or_omitted_creation_timestamp() -> None:
    assert StreamEvent(sequence=0, event_type="x", payload={}).created_at is None
    assert StreamEvent(sequence=0, event_type="x", payload={}, created_at=None).created_at is None
