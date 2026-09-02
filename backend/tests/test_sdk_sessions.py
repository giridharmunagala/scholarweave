from __future__ import annotations

from pathlib import Path

import pytest
from agents import SQLiteSession, TResponseInputItem

from backend.agents.blueprint import SessionPolicySpec
from backend.runtime.sessions import SdkSessionFactory


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
