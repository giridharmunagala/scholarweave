from __future__ import annotations

from pathlib import Path

import pytest
from agents import SQLiteSession, TResponseInputItem

from backend.agents.blueprint import SessionPolicySpec
from backend.conversations.sessions import SdkSessionFactory


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_session_factory_uses_unmodified_sqlite_history(tmp_path: Path) -> None:
    factory = SdkSessionFactory(tmp_path / "sessions.sqlite3")
    policy = SessionPolicySpec()

    session = factory.get("conversation", policy)
    assert isinstance(session, SQLiteSession)

    items: list[TResponseInputItem] = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "one"},
    ]
    await session.add_items(items)

    assert await session.get_items() == items
    assert factory.get("conversation", policy) is session
    await factory.close()


@pytest.mark.anyio
async def test_session_factory_evicts_wrapped_session_and_closes_all(
    tmp_path: Path,
    monkeypatch,
) -> None:
    closed: list[str] = []
    original_close = SQLiteSession.close

    def recording_close(self) -> None:
        closed.append(self.session_id)
        original_close(self)

    monkeypatch.setattr(SQLiteSession, "close", recording_close)
    factory = SdkSessionFactory(tmp_path / "sessions.sqlite3")
    policy = SessionPolicySpec(messages_only=True)
    wrapped = factory.get("conversation", policy)
    other = factory.get("other", SessionPolicySpec())

    await wrapped.add_items([{"role": "user", "content": "delete me"}])
    await factory.evict("conversation", clear=True)
    assert "conversation" in closed
    assert factory.get("conversation", SessionPolicySpec()) is not wrapped

    await factory.close()
    assert "other" in closed
    assert other is not None


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
