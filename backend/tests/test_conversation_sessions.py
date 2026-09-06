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


@pytest.mark.anyio
async def test_working_snapshot_reopens_with_only_new_audit_delta(tmp_path: Path) -> None:
    database = tmp_path / "sessions.sqlite3"
    session = ConversationSession("conversation", database)
    original = [
        {"role": "user", "content": "Keep exact constraints — never upload."},
        {"role": "assistant", "content": "long evidence " * 1000},
    ]
    await session.add_items(original)
    cursor = await session.checkpoint()
    final = [{"role": "assistant", "content": "Result."}]
    working = [original[0], {"role": "assistant", "content": "Compact findings."}, *final]
    await session.commit_working_items(final, working, base_cursor=cursor, commit_id="epoch-1")
    # Replaying a persisted epoch must not duplicate audit items or overwrite the snapshot.
    await session.commit_working_items(final, working, base_cursor=cursor, commit_id="epoch-1")
    delta = [{"role": "user", "content": "Follow up."}]
    await session.add_items(delta)
    reopened = ConversationSession("conversation", database)
    assert await reopened.get_items() == [*original, *final, *delta]
    assert await reopened.get_working_items() == [*working, *delta]


@pytest.mark.anyio
async def test_rollback_and_pop_invalidate_snapshots_without_resurrecting_items(tmp_path: Path) -> None:
    session = ConversationSession("conversation", tmp_path / "sessions.sqlite3")
    original = [{"role": "user", "content": "Original."}]
    await session.add_items(original)
    cursor = await session.checkpoint()
    invalid = [{"role": "assistant", "content": "Rejected answer."}]
    await session.commit_working_items(
        invalid, [*original, *invalid], base_cursor=cursor, commit_id="rejected"
    )
    await session.rollback_to(cursor)
    assert await session.get_working_items() == original
    accepted = [{"role": "assistant", "content": "Accepted answer."}]
    await session.commit_working_items(
        accepted, [*original, *accepted], base_cursor=cursor, commit_id="rejected"
    )
    assert await session.pop_item() == accepted[0]
    assert await session.get_working_items() == original
    await session.clear_session()
    assert await session.get_working_items() == []


@pytest.mark.anyio
async def test_snapshot_retains_steering_persisted_during_model_execution(tmp_path: Path) -> None:
    from backend.conversations.steering import SteeringMessage

    session = ConversationSession("conversation", tmp_path / "sessions.sqlite3")
    original = [{"role": "user", "content": "Research."}]
    await session.add_items(original)
    cursor = await session.checkpoint()
    steering = SteeringMessage("steering-1", "No more downloads.").session_item()
    await session.add_items([steering])
    final = [{"role": "assistant", "content": "Done."}]
    await session.commit_working_items(
        final, [*original, *final], base_cursor=cursor, commit_id="epoch-1"
    )
    assert (await session.get_working_items()).count(steering) == 1
    assert await session.get_items() == [*original, steering, *final]


@pytest.mark.anyio
async def test_legacy_compaction_rehydrates_canonical_history_once(tmp_path: Path) -> None:
    session = ConversationSession("conversation", tmp_path / "sessions.sqlite3")
    original = [{"role": "user", "content": "Keep the source details."}]
    await session.add_items(original)
    cursor = await session.checkpoint()
    previous = {"role": "assistant", "content": "Detailed findings before harsh compaction."}
    legacy = {
        "role": "user",
        "_scholarweave_context_checkpoint": True,
        "content": "Old lossy checkpoint",
    }
    await session.commit_working_items(
        [previous], [legacy], base_cursor=cursor, commit_id="legacy",
    )
    assert await session.get_working_items() == [*original, previous]
    current = {
        **legacy,
        "_scholarweave_context_policy_version": 2,
        "content": "Recoverable cached context",
    }
    await session.commit_working_items(
        [], [current], base_cursor=await session.checkpoint(), commit_id="current",
    )
    assert await session.get_working_items() == [current]
    assert await session.get_items() == [*original, previous]


@pytest.mark.anyio
async def test_bounded_snapshot_leaves_unprepared_last_round_as_audit_delta(tmp_path: Path) -> None:
    import json
    import sqlite3

    database = tmp_path / "sessions.sqlite3"
    session = ConversationSession("conversation", database)
    original = [{"role": "user", "content": "Research."}]
    await session.add_items(original)
    cursor = await session.checkpoint()
    previous = {"role": "assistant", "content": "Previous raw history " * 1000}
    tail = {"role": "assistant", "content": "Final newly generated content " * 1000}
    compact = [*original, {"role": "assistant", "content": "Compact prior findings."}]
    await session.commit_working_items(
        [previous, tail], compact, base_cursor=cursor, commit_id="epoch-1",
        covered_item_count=1,
    )
    with sqlite3.connect(database) as connection:
        payload, snapshot_cursor = connection.execute(
            "SELECT items, cursor FROM conversation_working_context WHERE session_id = ?",
            ("conversation",),
        ).fetchone()
    assert json.loads(payload) == compact
    assert await session.get_working_items() == [*compact, tail]
    assert await session.get_items() == [*original, previous, tail]
    await session.rollback_to(snapshot_cursor)
    assert await session.get_working_items() == compact


@pytest.mark.anyio
async def test_policy_snapshot_maps_filtered_audit_cursor_without_losing_constraints(tmp_path: Path) -> None:
    session = PolicySession(
        ConversationSession("conversation", tmp_path / "sessions.sqlite3"),
        SessionPolicySpec(messages_only=True, history_max_items=1),
    )
    user = {"role": "user", "content": "Keep this exact constraint."}
    await session.add_items([user])
    cursor = await session.checkpoint()
    call = {"type": "function_call", "call_id": "read", "name": "read", "arguments": "{}"}
    output = {"type": "function_call_output", "call_id": "read", "output": "Evidence."}
    answer = {"role": "assistant", "content": "Answer."}
    await session.commit_working_items(
        [call, output, answer], [user, call, output],
        base_cursor=cursor, commit_id="epoch", covered_item_count=2,
    )
    assert await session.get_items() == [answer]
    assert await session.get_working_items() == [user, answer]
