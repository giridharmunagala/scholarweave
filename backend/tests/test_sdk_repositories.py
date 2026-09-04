from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from sqlalchemy import event

from backend.agents.blueprint import SessionPolicySpec
from backend.core.config import Settings
from backend.utils import utcnow
from backend.conversations.repository import ConversationRepository
from backend.persistence import create_session_factory
from backend.runs.repository import LeaseOwnershipError, RunRepository


def repositories(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        database_path=tmp_path / "metadata.sqlite3",
    )
    settings.ensure_directories()
    sessions = create_session_factory(settings)
    return ConversationRepository(sessions), RunRepository(sessions)


def test_conversation_and_run_repositories(tmp_path) -> None:
    conversations, runs = repositories(tmp_path)
    conversation = conversations.create(
        title="Paper chat",
        kind="autonomous",
        model_reference={},
        session_policy=SessionPolicySpec().model_dump(mode="json"),
    )
    run = runs.create(
        conversation_id=conversation.id,
        agent_name="ScholarWeave research",
        input_value="Summarize this paper.",
        blueprint={},
    )
    runs.mark_running(run.id)
    runs.add_event(run.id, "run.started", {"agent": "ScholarWeave research"})
    runs.add_items(
        run.id,
        [{"type": "message_output_item", "agent_name": "Researcher", "content": "Done"}],
    )
    runs.complete(
        run.id,
        final_output="Done",
        last_agent_name="Researcher",
        usage={"total_tokens": 2},
    )

    saved = runs.get(run.id)
    assert saved.status == "completed"
    assert saved.final_output_json == "Done"
    assert saved.items[0].item_type == "message_output_item"
    assert saved.events[0].event_type == "run.started"
    assert [record.id for record in runs.list(conversation_id=conversation.id)] == [
        run.id
    ]
    assert runs.list(conversation_id="another-conversation") == []


def test_run_repository_bulk_deletes_run_relations(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        database_path=tmp_path / "metadata.sqlite3",
    )
    settings.ensure_directories()
    sessions = create_session_factory(settings)
    runs = RunRepository(sessions)
    old_run = runs.create(
        conversation_id=None,
        agent_name="Old run",
        input_value="old",
        blueprint={},
    )
    current_run = runs.create(
        conversation_id=None,
        agent_name="Current run",
        input_value="current",
        blueprint={},
    )
    runs.add_event(old_run.id, "run.completed", {})
    runs.add_items(
        old_run.id,
        [{"type": "message_output_item", "agent_name": "Old run"}],
    )
    with sessions() as session:
        stored = session.get(type(old_run), old_run.id)
        assert stored is not None
        stored.created_at = utcnow() - timedelta(days=3)
        session.commit()

    expired = runs.ids_created_before(utcnow() - timedelta(days=2))
    runs.delete_many(expired)

    assert expired == [old_run.id]
    assert [record.id for record in runs.list()] == [current_run.id]


def test_run_claim_lease_protects_live_owner_and_allows_expired_takeover(
    tmp_path,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        database_path=tmp_path / "metadata.sqlite3",
    )
    settings.ensure_directories()
    runs = RunRepository(create_session_factory(settings))
    run = runs.create(
        conversation_id=None,
        agent_name="Lease test",
        input_value="work",
        blueprint={},
    )
    now = utcnow()

    owner_a = runs.claim(run.id, "owner-a", now=now, lease_seconds=120)
    assert owner_a is not None
    assert not runs.claim(
        run.id,
        "owner-b",
        now=now + timedelta(seconds=119),
        lease_seconds=120,
    )
    renewed_a = runs.renew_claim(
        run.id,
        "owner-a",
        generation=owner_a.generation,
        token=owner_a.token,
        now=now + timedelta(seconds=60),
        lease_seconds=120,
    )
    assert renewed_a is not None
    assert not runs.claim(
        run.id,
        "owner-b",
        now=now + timedelta(seconds=121),
        lease_seconds=120,
    )
    assert not runs.renew_claim(
        run.id,
        "owner-a",
        generation=renewed_a.generation,
        token=renewed_a.token,
        now=now + timedelta(seconds=181),
        lease_seconds=120,
    )
    owner_b = runs.claim(
        run.id,
        "owner-b",
        now=now + timedelta(seconds=181),
        lease_seconds=120,
    )
    assert owner_b is not None
    assert owner_b.generation == owner_a.generation + 1
    with pytest.raises(LeaseOwnershipError):
        runs.begin_epoch_owned(owner_a, "stale work")
    with pytest.raises(LeaseOwnershipError):
        runs.add_events_owned(owner_a, [("run.completed", {})])
    with pytest.raises(LeaseOwnershipError):
        runs.complete_owned(
            owner_a,
            final_output="stale",
            last_agent_name="old owner",
            usage={},
        )
    assert not runs.release_claim(
        run.id,
        "owner-a",
        generation=owner_a.generation,
        token=owner_a.token,
    )
    assert runs.release_claim(
        run.id,
        "owner-b",
        generation=owner_b.generation,
        token=owner_b.token,
    )


def test_cancel_intent_wins_atomically_over_owner_completion(tmp_path) -> None:
    _, runs = repositories(tmp_path)
    run = runs.create(
        conversation_id=None,
        agent_name="Cancellation race",
        input_value="work",
        blueprint={},
    )
    lease = runs.claim(run.id, "owner")
    assert lease is not None
    runs.mark_running_owned(lease)

    assert runs.request_cancel(run.id)
    assert not runs.complete_owned(
        lease,
        final_output="too late",
        last_agent_name="owner",
        usage={},
    )
    assert runs.cancel_owned(lease)
    assert not runs.request_cancel(run.id)
    assert runs.get(run.id).status == "cancelled"


def test_released_claim_token_fences_same_owner_reacquisition(tmp_path) -> None:
    _, runs = repositories(tmp_path)
    run = runs.create(
        conversation_id=None,
        agent_name="Claim token",
        input_value="work",
        blueprint={},
    )
    first = runs.claim(run.id, "same-owner")
    assert first is not None
    assert runs.release_claim(
        run.id,
        first.owner_id,
        generation=first.generation,
        token=first.token,
    )

    second = runs.claim(run.id, "same-owner")
    assert second is not None
    assert second.token != first.token
    assert not runs.validate_claim(first)
    with pytest.raises(LeaseOwnershipError):
        runs.add_events_owned(first, [("stale.event", {})])
    assert not runs.release_claim(
        run.id,
        first.owner_id,
        generation=first.generation,
        token=first.token,
    )
    assert runs.release_claim(
        run.id,
        second.owner_id,
        generation=second.generation,
        token=second.token,
    )


def test_owned_write_holds_claim_fence_until_commit(tmp_path, monkeypatch) -> None:
    _, runs = repositories(tmp_path)
    run = runs.create(
        conversation_id=None,
        agent_name="Atomic owner fence",
        input_value="work",
        blueprint={},
    )
    lease = runs.claim(run.id, "old-owner", lease_seconds=60)
    assert lease is not None
    validated = threading.Event()
    continue_write = threading.Event()
    takeover_started = threading.Event()
    takeover_finished = threading.Event()
    writer_errors: list[BaseException] = []
    takeover: list[object] = []
    original_require = runs._require_claim
    engine = runs._sessions.kw["bind"]

    def observe_takeover(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        if (
            threading.current_thread().name == "claim-takeover"
            and "INSERT INTO agent_run_claims" in statement
        ):
            takeover_started.set()

    event.listen(engine, "before_cursor_execute", observe_takeover)

    def pause_after_validation(session, candidate) -> None:
        original_require(session, candidate)
        validated.set()
        assert continue_write.wait(timeout=2)

    monkeypatch.setattr(runs, "_require_claim", pause_after_validation)

    def write_as_old_owner() -> None:
        try:
            runs.add_events_owned(lease, [("old-owner.event", {})])
        except BaseException as exc:
            writer_errors.append(exc)

    def take_over() -> None:
        takeover.append(
            runs.claim(
                run.id,
                "new-owner",
                now=lease.expires_at,
                lease_seconds=60,
            )
        )
        takeover_finished.set()

    writer = threading.Thread(target=write_as_old_owner)
    writer.start()
    assert validated.wait(timeout=2)
    replacement = threading.Thread(target=take_over, name="claim-takeover")
    replacement.start()
    assert takeover_started.wait(timeout=2)
    assert not takeover_finished.wait(timeout=0.1)
    continue_write.set()
    writer.join(timeout=2)
    replacement.join(timeout=2)
    event.remove(engine, "before_cursor_execute", observe_takeover)

    assert not writer_errors
    assert takeover_finished.is_set()
    assert takeover and takeover[0] is not None
    with pytest.raises(LeaseOwnershipError):
        runs.add_events_owned(lease, [("stale-after-takeover", {})])
    assert [event.event_type for event in runs.events_after(run.id)] == [
        "old-owner.event"
    ]
