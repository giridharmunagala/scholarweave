from __future__ import annotations

from pathlib import Path

import pytest
from agents import Model, ModelResponse, ModelSettings, SQLiteSession, TResponseInputItem, Usage
from openai import AsyncOpenAI

from backend.agents.blueprint import SessionPolicySpec
from backend.providers.types import ResolvedAgentModel
from backend.runtime.compaction import COMPACTION_MARKER, LocalCompactionSession
from backend.runtime.compaction_events import observe_compaction
from backend.runtime.sessions import SdkSessionFactory


class NoopModel(Model):
    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools,
        output_schema,
        handoffs,
        tracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt,
    ) -> ModelResponse:
        return ModelResponse(output=[], usage=Usage(), response_id=None)

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


class StaticCompactor:
    async def compact(self, items):
        return f"Summary of {len(items)} items."


class FailingCompactor:
    async def compact(self, items):
        raise RuntimeError("compaction failed")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def message(role: str, text: str) -> TResponseInputItem:
    content_type = "input_text" if role == "user" else "output_text"
    return {"role": role, "content": [{"type": content_type, "text": text}]}


@pytest.mark.anyio
async def test_local_compaction_replaces_old_history(tmp_path: Path) -> None:
    base = SQLiteSession("conversation", tmp_path / "sessions.sqlite3")
    session = LocalCompactionSession(
        base,
        StaticCompactor(),
        threshold_items=6,
        recent_items_to_keep=2,
    )

    await session.add_items(
        [
            message("user", "first"),
            message("assistant", "one"),
            message("user", "second"),
            message("assistant", "two"),
            message("user", "latest"),
            message("assistant", "three"),
        ]
    )

    items = await session.get_items()
    assert len(items) == 3
    assert COMPACTION_MARKER in items[0]["content"][0]["text"]
    assert items[1:] == [message("user", "latest"), message("assistant", "three")]


@pytest.mark.anyio
async def test_local_compaction_reports_lifecycle_events(tmp_path: Path) -> None:
    base = SQLiteSession("conversation", tmp_path / "sessions.sqlite3")
    session = LocalCompactionSession(
        base,
        StaticCompactor(),
        threshold_items=4,
        recent_items_to_keep=2,
    )
    events: list[tuple[str, dict]] = []

    async def capture(event_type: str, payload: dict) -> None:
        events.append((event_type, payload))

    async with observe_compaction(capture):
        await session.add_items(
            [
                message("user", "first"),
                message("assistant", "one"),
                message("user", "latest"),
                message("assistant", "two"),
            ]
        )

    assert [event_type for event_type, _payload in events] == [
        "compaction.started",
        "compaction.completed",
    ]
    assert events[-1][1] == {
        "strategy": "local",
        "removed_items": 2,
        "remaining_items": 3,
    }


@pytest.mark.anyio
async def test_failed_local_compaction_keeps_original_history(tmp_path: Path) -> None:
    base = SQLiteSession("conversation", tmp_path / "sessions.sqlite3")
    session = LocalCompactionSession(
        base,
        FailingCompactor(),
        threshold_items=4,
        recent_items_to_keep=2,
    )
    original = [
        message("user", "first"),
        message("assistant", "one"),
        message("user", "latest"),
        message("assistant", "two"),
    ]

    with pytest.raises(RuntimeError, match="compaction failed"):
        await session.add_items(original)

    assert await session.get_items() == original


@pytest.mark.anyio
async def test_session_factory_selects_provider_strategy(tmp_path: Path) -> None:
    factory = SdkSessionFactory(tmp_path / "sessions.sqlite3")
    client = AsyncOpenAI(api_key="test")
    responses_model = ResolvedAgentModel(
        NoopModel(),
        "openai",
        True,
        True,
        True,
        model_name="gpt-4.1",
        responses_client=client,
    )
    local_model = ResolvedAgentModel(
        NoopModel(),
        "ollama",
        False,
        False,
        False,
        model_name="qwen",
    )

    responses_session = factory.get(
        "openai-conversation",
        SessionPolicySpec(
            compaction_threshold_items=10,
            recent_items_to_keep=4,
        ),
        responses_model,
    )
    local_session = factory.get(
        "local-conversation",
        SessionPolicySpec(
            compaction_threshold_items=10,
            recent_items_to_keep=4,
        ),
        local_model,
    )

    from agents import OpenAIResponsesCompactionSession

    assert isinstance(responses_session, OpenAIResponsesCompactionSession)
    assert isinstance(local_session, LocalCompactionSession)
    assert factory.get(
        "local-conversation",
        SessionPolicySpec(
            compaction_threshold_items=10,
            recent_items_to_keep=4,
        ),
        local_model,
    ) is local_session
    await client.close()


def test_session_factory_rejects_forced_responses_on_local_model(tmp_path: Path) -> None:
    factory = SdkSessionFactory(tmp_path / "sessions.sqlite3")
    local_model = ResolvedAgentModel(NoopModel(), "ollama", False, False, False)

    with pytest.raises(ValueError, match="does not support Responses"):
        factory.get(
            "conversation",
            SessionPolicySpec(
                strategy="openai_responses",
                compaction_threshold_items=10,
                recent_items_to_keep=4,
            ),
            local_model,
        )
