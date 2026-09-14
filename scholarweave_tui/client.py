from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, TypeVar
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from backend.agents.blueprint import ModelReferenceSpec
from backend.conversations.schemas import (
    ConversationDetailResponse,
    ConversationMessageResponse,
    ConversationResponse,
    ResponseEffort,
)
from backend.core.settings_service import SettingsResponse
from backend.providers.reasoning import ReasoningEffort
from backend.providers.schemas import (
    ProviderCreate,
    ProviderModelsResponse,
    ProviderResponse,
    ProviderUpdate,
)
from backend.research.schemas import DocumentResponse, DocumentSummaryResponse
from backend.runs.schemas import RunResponse, SteeringMessageResponse
from backend.workspace.schemas import (
    WorkspaceFileContentResponse,
    WorkspaceFileResponse,
    WorkspaceSearchResponse,
)

T = TypeVar("T")


class ApiError(RuntimeError):
    """A failed request or an invalid response from the local backend."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class StreamEvent(BaseModel):
    sequence: int = Field(strict=True, ge=0)
    event_type: str
    payload: dict[str, Any]
    created_at: datetime | None = None


def _local_origin(value: str) -> str:
    error = (
        "API URL must be an http loopback server root, such as "
        "http://127.0.0.1:8000 (no credentials, /api, query, or fragment)."
    )
    if not isinstance(value, str) or any(char.isspace() for char in value):
        raise ValueError(error)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(error)
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ValueError(error) from exc
    authority = re.fullmatch(
        r"(127\.0\.0\.1|localhost|\[::1\])(?::([0-9]+))?",
        parsed.netloc,
        flags=re.IGNORECASE,
    )
    if (
        parsed.scheme != "http"
        or authority is None
        or parsed.path not in {"", "/"}
        or "?" in value
        or "#" in value
    ):
        raise ValueError(error)
    port = authority.group(2)
    if port is not None and (len(port) > 5 or not 1 <= int(port) <= 65535):
        raise ValueError("API URL port must be between 1 and 65535.")
    host = authority.group(1).lower()
    return f"http://{host}" + (f":{int(port)}" if port is not None else "")


def _segment(value: str) -> str:
    if not value or value in {".", ".."}:
        raise ApiError("An API resource ID must be nonempty and cannot be '.' or '..'.")
    return quote(value, safe="")


def _check_status(response: httpx.Response) -> None:
    if response.is_success:
        return
    try:
        body = response.json()
        detail = body.get("detail", body) if isinstance(body, dict) else body
        message = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
    except (ValueError, UnicodeError):
        message = response.text.strip()[:1000]
    raise ApiError(
        f"HTTP {response.status_code} {response.request.method} "
        f"{response.request.url.path}: {message or response.reason_phrase}",
        status_code=response.status_code,
    )


def _transport_error(exc: httpx.RequestError) -> ApiError:
    target = str(exc.request.url)
    if isinstance(exc, httpx.TimeoutException):
        return ApiError(f"Request timed out connecting to or reading from {target}.")
    return ApiError(
        f"Could not reach the local backend at {target}: {exc or type(exc).__name__}. "
        "Check that the ScholarWeave server is running."
    )


class ScholarWeaveClient:
    """Typed HTTP access to the existing local API; mutations are never retried."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = _local_origin(base_url)
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(30, connect=5),
        )

    @property
    def base_url(self) -> str:
        return self._base_url

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(
        self, method: str, path: str, response_type: type[T], **kwargs: Any,
    ) -> T:
        try:
            response = await self._http.request(method, f"/api{path}", **kwargs)
        except httpx.RequestError as exc:
            raise _transport_error(exc) from exc
        _check_status(response)
        try:
            data = response.json()
        except (ValueError, UnicodeError) as exc:
            raise ApiError(f"Invalid JSON response from {method} /api{path}.") from exc
        try:
            return TypeAdapter(response_type).validate_python(data)
        except ValidationError as exc:
            raise ApiError(f"Invalid response from {method} /api{path}: {exc}") from exc

    async def conversations(self) -> list[ConversationResponse]:
        return await self._request("GET", "/agent/conversations", list[ConversationResponse])

    async def conversation(self, id: str) -> ConversationDetailResponse:
        return await self._request(
            "GET", f"/agent/conversations/{_segment(id)}", ConversationDetailResponse,
        )

    async def create_conversation(
        self, model_reference: ModelReferenceSpec | None = None,
    ) -> ConversationResponse:
        # An empty reference stays out of the payload so the backend picks the configured default.
        payload: dict[str, Any] = {}
        if model_reference and (model_reference.provider_profile_id or model_reference.model):
            payload["model_reference"] = model_reference.model_dump(mode="json")
        return await self._request(
            "POST", "/agent/conversations", ConversationResponse, json=payload,
        )

    async def send_message(
        self, id: str, content: str, *, effort: ResponseEffort = "auto",
        web_enabled: bool = True, reasoning_effort: ReasoningEffort | None = None,
        context_window_tokens: int | None = None,
    ) -> ConversationMessageResponse:
        payload: dict[str, Any] = {
            "content": content, "response_effort": effort, "web_enabled": web_enabled,
        }
        if reasoning_effort is not None:
            payload["reasoning_effort"] = reasoning_effort
        if context_window_tokens is not None:
            payload["context_window_tokens"] = context_window_tokens
        return await self._request(
            "POST", f"/agent/conversations/{_segment(id)}/messages",
            ConversationMessageResponse, json=payload,
        )

    async def providers(self) -> list[ProviderResponse]:
        return await self._request("GET", "/providers", list[ProviderResponse])

    async def create_provider(self, payload: ProviderCreate) -> ProviderResponse:
        return await self._request(
            "POST", "/providers", ProviderResponse,
            json=payload.model_dump(mode="json"),
        )

    async def update_provider(
        self, profile_id: str, payload: ProviderUpdate,
    ) -> ProviderResponse:
        return await self._request(
            "PUT", f"/providers/{_segment(profile_id)}", ProviderResponse,
            json=payload.model_dump(mode="json", exclude_unset=True),
        )

    async def discover_provider_models(self, profile_id: str) -> ProviderModelsResponse:
        return await self._request(
            "GET", f"/providers/{_segment(profile_id)}/models", ProviderModelsResponse,
        )

    async def settings(self) -> SettingsResponse:
        return await self._request("GET", "/settings", SettingsResponse)

    async def save_chat_model(self, model_reference: ModelReferenceSpec) -> SettingsResponse:
        return await self._request(
            "PUT", "/settings", SettingsResponse,
            json={"last_chat_model_reference": model_reference.model_dump(mode="json")},
        )

    async def runs(self, conversation_id: str | None = None) -> list[RunResponse]:
        params = {"conversation_id": conversation_id} if conversation_id is not None else {}
        return await self._request("GET", "/runs", list[RunResponse], params=params)

    async def run(self, id: str) -> RunResponse:
        return await self._request("GET", f"/runs/{_segment(id)}", RunResponse)

    async def cancel_run(self, id: str) -> RunResponse:
        return await self._request("POST", f"/runs/{_segment(id)}/cancel", RunResponse)

    async def steer_run(self, id: str, content: str) -> SteeringMessageResponse:
        return await self._request(
            "POST", f"/runs/{_segment(id)}/steering", SteeringMessageResponse,
            json={"content": content},
        )

    async def papers(self) -> list[DocumentSummaryResponse]:
        return await self._request("GET", "/documents", list[DocumentSummaryResponse])

    async def paper(self, id: str) -> DocumentResponse:
        return await self._request("GET", f"/documents/{_segment(id)}", DocumentResponse)

    async def _pages(
        self, path: str, response_type: type[T], *,
        params: list[tuple[str, str]] | None = None,
    ) -> list[T]:
        result: list[T] = []
        while True:
            page = await self._request(
                "GET", path, list[response_type],
                params=[*(params or []), ("limit", "100"), ("offset", str(len(result)))],
            )
            result.extend(page)
            if len(page) < 100:
                return result

    async def notes(self) -> list[WorkspaceFileResponse]:
        return await self._pages("/workspace/notes", WorkspaceFileResponse)

    async def read_note(self, path: str) -> WorkspaceFileContentResponse:
        return await self._request(
            "GET", "/workspace/files/content", WorkspaceFileContentResponse,
            params={"path": path},
        )

    async def save_note(self, path: str, content: str) -> WorkspaceFileContentResponse:
        return await self._request(
            "PUT", "/workspace/files/content", WorkspaceFileContentResponse,
            json={"path": path, "content": content},
        )

    async def create_note(
        self, name: str, content: str = "",
    ) -> WorkspaceFileContentResponse:
        return await self._request(
            "POST", "/workspace/files/notes", WorkspaceFileContentResponse,
            json={"name": name, "content": content},
        )

    async def search_notes(self, query: str) -> list[WorkspaceSearchResponse]:
        return await self._pages(
            "/workspace/search", WorkspaceSearchResponse,
            params=[("query", query), ("kinds", "note"), ("kinds", "paper_notes")],
        )

    async def events(self, run_id: str, after: int = -1) -> AsyncIterator[StreamEvent]:
        path = f"/api/runs/{_segment(run_id)}/events"
        cursor = after
        try:
            async with self._http.stream(
                "GET", path, params={"after": after},
                headers={"Accept": "text/event-stream"},
                timeout=httpx.Timeout(30, connect=5, read=None),
            ) as response:
                if not response.is_success:
                    await response.aread()
                    _check_status(response)
                media_type = response.headers.get("content-type", "").split(";")[0].strip()
                if media_type.lower() != "text/event-stream":
                    raise ApiError(f"Expected an SSE event stream from {path}, got {media_type!r}.")
                async for event in _decode_events(response):
                    if event.sequence > cursor:
                        cursor = event.sequence
                        yield event
        except httpx.RequestError as exc:
            raise _transport_error(exc) from exc


async def _decode_events(response: httpx.Response) -> AsyncIterator[StreamEvent]:
    data: list[str] = []
    first_line = True
    # httpx handles incremental UTF-8 decoding and LF, CRLF, or CR line boundaries.
    async for line in response.aiter_lines():
        if first_line:
            line = line.removeprefix("\ufeff")
            first_line = False
        if not line:
            if data:
                yield _stream_event("\n".join(data))
                data.clear()
            continue
        field, separator, value = line.partition(":")
        if field == "data":
            data.append(value.removeprefix(" ") if separator else "")
    if data:
        yield _stream_event("\n".join(data))


def _stream_event(data: str) -> StreamEvent:
    try:
        return StreamEvent.model_validate_json(data)
    except ValidationError as exc:
        raise ApiError(f"Invalid SSE event JSON or envelope: {exc}") from exc
