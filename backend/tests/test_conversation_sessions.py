from __future__ import annotations

from pathlib import Path

import pytest

from backend.agents.blueprint import SessionPolicySpec
from backend.agents.harness import ConversationItem
from backend.conversations.sessions import (
    ConversationSession,
    ConversationSessionFactory,
    PolicySession,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_session_factory_uses_unmodified_sqlite_history(tmp_path: Path) -> None:
    factory = ConversationSessionFactory(tmp_path / "sessions.sqlite3")
    policy = SessionPolicySpec()

    session = factory.get("conversation", policy)
    assert isinstance(session, ConversationSession)

    items: list[ConversationItem] = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "one"},
    ]
    await session.add_items(items)

    assert await session.get_items() == items
    assert factory.get("conversation", policy) is session
    await factory.close()


@pytest.mark.anyio
async def test_session_persists_tool_calls_and_outputs_across_instances(
    tmp_path: Path,
) -> None:
    database = tmp_path / "sessions.sqlite3"
    items: list[ConversationItem] = [
        {"role": "user", "content": "Find one paper."},
        {
            "type": "function_call",
            "call_id": "call-1",
            "name": "search_research_library",
            "arguments": '{"query":"graph"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call-1",
            "name": "search_research_library",
            "output": {"id": "paper-1"},
        },
        {"role": "assistant", "content": "Found it."},
    ]
    await ConversationSession("conversation", database).add_items(items)

    reopened = ConversationSession("conversation", database)

    assert await reopened.get_items() == items


@pytest.mark.anyio
async def test_policy_session_limits_history_and_filters_non_messages(
    tmp_path: Path,
) -> None:
    factory = ConversationSessionFactory(tmp_path / "sessions.sqlite3")
    policy = SessionPolicySpec(history_max_items=2, messages_only=True)
    session = factory.get("conversation", policy)
    assert isinstance(session, PolicySession)

    await session.add_items(
        [
            {"role": "user", "content": "one"},
            {"type": "function_call", "call_id": "c", "name": "n", "arguments": "{}"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ]
    )

    assert await session.get_items() == [
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
    ]
    await factory.close()


@pytest.mark.anyio
async def test_pop_item_removes_the_newest_entry(tmp_path: Path) -> None:
    session = ConversationSession("conversation", tmp_path / "sessions.sqlite3")
    await session.add_items(
        [{"role": "user", "content": "one"}, {"role": "user", "content": "two"}]
    )

    assert await session.pop_item() == {"role": "user", "content": "two"}
    assert await session.get_items() == [{"role": "user", "content": "one"}]
    await session.clear_session()
    assert await session.get_items() == []
    assert await session.pop_item() is None


@pytest.mark.anyio
async def test_session_rolls_back_only_items_after_checkpoint(tmp_path: Path) -> None:
    session = ConversationSession("conversation", tmp_path / "sessions.sqlite3")
    preserved = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "one"},
        {"role": "user", "content": "second"},
    ]
    await session.add_items(preserved)
    checkpoint = await session.checkpoint()
    await session.add_items(
        [
            {"role": "assistant", "content": "invalid answer"},
            {"role": "user", "content": "internal continuation"},
        ]
    )

    await session.rollback_to(checkpoint)

    assert await session.get_items() == preserved


@pytest.mark.anyio
async def test_session_factory_evicts_and_deletes_persisted_history(
    tmp_path: Path,
) -> None:
    database = tmp_path / "sessions.sqlite3"
    factory = ConversationSessionFactory(database)
    policy = SessionPolicySpec(messages_only=True)
    wrapped = factory.get("conversation", policy)
    other = factory.get("other", SessionPolicySpec())

    await wrapped.add_items([{"role": "user", "content": "delete me"}])
    await other.add_items([{"role": "user", "content": "keep me"}])
    await factory.evict("conversation", clear=True)

    assert factory.get("conversation", SessionPolicySpec()) is not wrapped
    assert await factory.get("conversation", SessionPolicySpec()).get_items() == []
    assert await other.get_items() == [{"role": "user", "content": "keep me"}]

    factory.delete_persisted("other")
    assert await ConversationSession("other", database).get_items() == []
    await factory.close()


def test_legacy_compaction_policy_is_ignored() -> None:
    policy = SessionPolicySpec.model_validate(
        {
            "strategy": "local",
            "compaction_enabled": True,
            "compaction_threshold_items": 20,
            "recent_items_to_keep": 8,
        }
    )

    assert policy == SessionPolicySpec()
