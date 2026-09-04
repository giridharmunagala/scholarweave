from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from backend.core.errors import NotFoundError
from backend.runs.models import (
    AgentRunEventRecord,
    AgentRunClaimRecord,
    AgentRunEpochRecord,
    AgentRunItemRecord,
    AgentRunRecord,
    AgentGoalStateRecord,
    AgentToolAttemptRecord,
    RunInterruptionRecord,
)
from backend.utils import utcnow


@dataclass(frozen=True)
class RunLease:
    run_id: str
    owner_id: str
    generation: int
    token: str
    claimed_at: datetime
    expires_at: datetime

    @property
    def duration_seconds(self) -> float:
        return max(0.0, (self.expires_at - self.claimed_at).total_seconds())


class LeaseOwnershipError(RuntimeError):
    pass


class RunRepository:
    DEFAULT_LEASE_SECONDS = 300.0

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def create(
        self,
        *,
        conversation_id: str | None,
        agent_name: str,
        input_value: Any,
        blueprint: dict[str, Any],
        context_window_tokens: int | None = None,
        runtime_metadata: dict[str, Any] | None = None,
        completion_policy_id: str | None = None,
    ) -> AgentRunRecord:
        with self._sessions() as session:
            record = AgentRunRecord(
                conversation_id=conversation_id,
                agent_name=agent_name,
                input_json=input_value,
                blueprint_json=blueprint,
                context_window_tokens=context_window_tokens,
                runtime_metadata_json=runtime_metadata or {},
                completion_policy_id=completion_policy_id,
            )
            session.add(record)
            session.commit()
            session.refresh(record)
            return record

    def list(self, *, conversation_id: str | None = None) -> list[AgentRunRecord]:
        with self._sessions() as session:
            statement = select(AgentRunRecord)
            if conversation_id is not None:
                statement = statement.where(
                    AgentRunRecord.conversation_id == conversation_id
                )
            records = list(
                session.scalars(statement.order_by(AgentRunRecord.created_at.desc()))
            )
            for record in records:
                self._load_relations(record)
            return records

    def get(self, run_id: str) -> AgentRunRecord:
        with self._sessions() as session:
            record = session.get(AgentRunRecord, run_id)
            if record is None:
                raise NotFoundError("Run was not found.")
            self._load_relations(record)
            return record

    def ids_created_before(self, cutoff: datetime | None = None) -> list[str]:
        with self._sessions() as session:
            statement = select(AgentRunRecord.id)
            if cutoff is not None:
                statement = statement.where(AgentRunRecord.created_at < cutoff)
            return list(session.scalars(statement))

    def mark_running(self, run_id: str) -> None:
        with self._sessions() as session:
            record = session.get(AgentRunRecord, run_id)
            if record is None:
                raise NotFoundError("Run was not found.")
            record.status = "running"
            record.error = None
            record.state_json = None
            if record.started_at is None:
                record.started_at = utcnow()
            session.commit()

    def mark_running_owned(self, lease: RunLease) -> None:
        with self._sessions() as session:
            self._begin_immediate(session)
            self._require_claim(session, lease)
            record = self._run(session, lease.run_id)
            if record.status not in {"pending", "running"}:
                raise LeaseOwnershipError(
                    f"Run {lease.run_id} is no longer executable."
                )
            record.status = "running"
            record.error = None
            record.state_json = None
            if record.started_at is None:
                record.started_at = utcnow()
            session.commit()

    def claim(
        self,
        run_id: str,
        owner_id: str,
        *,
        now: datetime | None = None,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> RunLease | None:
        claimed_at = now or utcnow()
        expires_at = claimed_at + timedelta(seconds=lease_seconds)
        token = str(uuid.uuid4())
        with self._sessions() as session:
            statement = sqlite_insert(AgentRunClaimRecord).values(
                run_id=run_id,
                owner_id=owner_id,
                token=token,
                claimed_at=claimed_at,
                expires_at=expires_at,
            )
            statement = statement.on_conflict_do_update(
                index_elements=[AgentRunClaimRecord.run_id],
                set_={
                    "owner_id": owner_id,
                    "generation": AgentRunClaimRecord.generation + 1,
                    "token": token,
                    "claimed_at": claimed_at,
                    "expires_at": expires_at,
                },
                where=AgentRunClaimRecord.expires_at <= claimed_at,
            )
            result = session.execute(statement)
            session.commit()
            if result.rowcount != 1:
                return None
            claim = session.get(AgentRunClaimRecord, run_id)
            return self._lease(claim) if claim is not None else None

    def renew_claim(
        self,
        run_id: str,
        owner_id: str,
        *,
        generation: int,
        token: str,
        now: datetime | None = None,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> RunLease | None:
        renewed_at = now or utcnow()
        with self._sessions() as session:
            conditions = [
                AgentRunClaimRecord.run_id == run_id,
                AgentRunClaimRecord.owner_id == owner_id,
                AgentRunClaimRecord.generation == generation,
                AgentRunClaimRecord.token == token,
                AgentRunClaimRecord.expires_at > renewed_at,
            ]
            result = session.execute(
                update(AgentRunClaimRecord)
                .where(*conditions)
                .values(
                    claimed_at=renewed_at,
                    expires_at=renewed_at + timedelta(seconds=lease_seconds),
                )
            )
            session.commit()
            if result.rowcount != 1:
                return None
            claim = session.get(AgentRunClaimRecord, run_id)
            return self._lease(claim) if claim is not None else None

    def release_claim(
        self,
        run_id: str,
        owner_id: str,
        *,
        generation: int,
        token: str,
    ) -> bool:
        with self._sessions() as session:
            conditions = [
                AgentRunClaimRecord.run_id == run_id,
                AgentRunClaimRecord.owner_id == owner_id,
                AgentRunClaimRecord.generation == generation,
                AgentRunClaimRecord.token == token,
            ]
            result = session.execute(delete(AgentRunClaimRecord).where(*conditions))
            session.commit()
            return result.rowcount == 1

    def validate_claim(self, lease: RunLease, *, now: datetime | None = None) -> bool:
        with self._sessions() as session:
            return self._owns_claim(session, lease, now=now or utcnow())

    def update_runtime_metadata(
        self,
        run_id: str,
        metadata: dict[str, Any],
    ) -> None:
        self._update(run_id, runtime_metadata_json=metadata)

    def incomplete(self) -> list[AgentRunRecord]:
        with self._sessions() as session:
            return list(
                session.scalars(
                    select(AgentRunRecord).where(
                        AgentRunRecord.status.in_(("pending", "running"))
                    )
                )
            )

    def begin_epoch(self, run_id: str, input_value: Any) -> AgentRunEpochRecord:
        with self._sessions() as session:
            maximum = session.scalar(
                select(func.max(AgentRunEpochRecord.epoch_index)).where(
                    AgentRunEpochRecord.run_id == run_id
                )
            )
            epoch = AgentRunEpochRecord(
                run_id=run_id,
                epoch_index=(maximum if maximum is not None else -1) + 1,
                input_json=input_value,
            )
            session.add(epoch)
            session.commit()
            session.refresh(epoch)
            return epoch

    def begin_epoch_owned(
        self,
        lease: RunLease,
        input_value: Any,
    ) -> AgentRunEpochRecord:
        with self._sessions() as session:
            self._begin_immediate(session)
            self._require_claim(session, lease)
            maximum = session.scalar(
                select(func.max(AgentRunEpochRecord.epoch_index)).where(
                    AgentRunEpochRecord.run_id == lease.run_id
                )
            )
            epoch = AgentRunEpochRecord(
                run_id=lease.run_id,
                epoch_index=(maximum if maximum is not None else -1) + 1,
                input_json=input_value,
            )
            session.add(epoch)
            session.commit()
            session.refresh(epoch)
            return epoch

    def finish_epoch(
        self,
        epoch_id: str,
        *,
        status: str,
        terminal_reason: str,
        usage: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self._sessions() as session:
            epoch = session.get(AgentRunEpochRecord, epoch_id)
            if epoch is None:
                raise NotFoundError("Run epoch was not found.")
            epoch.status = status
            epoch.terminal_reason = terminal_reason
            epoch.usage_json = usage or {}
            epoch.error = error
            epoch.finished_at = utcnow()
            session.commit()

    def finish_epoch_owned(
        self,
        lease: RunLease,
        epoch_id: str,
        *,
        status: str,
        terminal_reason: str,
        usage: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self._sessions() as session:
            self._begin_immediate(session)
            self._require_claim(session, lease)
            epoch = session.get(AgentRunEpochRecord, epoch_id)
            if epoch is None or epoch.run_id != lease.run_id:
                raise NotFoundError("Run epoch was not found.")
            epoch.status = status
            epoch.terminal_reason = terminal_reason
            epoch.usage_json = usage or {}
            epoch.error = error
            epoch.finished_at = utcnow()
            session.commit()

    def aggregate_epoch_usage(self, run_id: str) -> dict[str, Any]:
        with self._sessions() as session:
            usages = session.scalars(
                select(AgentRunEpochRecord.usage_json).where(
                    AgentRunEpochRecord.run_id == run_id
                )
            )
            aggregate: dict[str, Any] = {}
            for usage in usages:
                if not isinstance(usage, dict):
                    continue
                aggregate = _merge_usage(aggregate, usage)
            return aggregate

    def consumed_model_turns(self, run_id: str) -> int:
        with self._sessions() as session:
            usages = session.scalars(
                select(AgentRunEpochRecord.usage_json).where(
                    AgentRunEpochRecord.run_id == run_id
                )
            )
            total = 0
            for usage in usages:
                if not isinstance(usage, dict):
                    continue
                value = usage.get("model_turns", usage.get("requests", 0))
                if isinstance(value, int) and not isinstance(value, bool):
                    total += value
            return total

    def update_usage(self, run_id: str, usage: dict[str, Any]) -> None:
        self._update(run_id, usage_json=usage)

    def update_usage_owned(self, lease: RunLease, usage: dict[str, Any]) -> None:
        self._update_owned(lease, usage_json=usage)

    def abandon_incomplete_epochs(self, run_id: str) -> None:
        with self._sessions() as session:
            self._abandon_incomplete_epochs(session, run_id)
            session.commit()

    def abandon_incomplete_epochs_owned(self, lease: RunLease) -> None:
        with self._sessions() as session:
            self._begin_immediate(session)
            self._require_claim(session, lease)
            self._abandon_incomplete_epochs(session, lease.run_id)
            session.commit()

    def begin_tool_attempt(
        self,
        *,
        run_id: str,
        epoch_id: str | None,
        tool_call_id: str,
        catalog_id: str,
        attempt: int,
        arguments: dict[str, Any],
    ) -> AgentToolAttemptRecord:
        with self._sessions() as session:
            record = AgentToolAttemptRecord(
                run_id=run_id,
                epoch_id=epoch_id,
                tool_call_id=tool_call_id,
                catalog_id=catalog_id,
                attempt=attempt,
                arguments_json=arguments,
            )
            session.add(record)
            session.commit()
            session.refresh(record)
            return record

    def begin_tool_attempt_owned(
        self,
        lease: RunLease,
        *,
        epoch_id: str | None,
        tool_call_id: str,
        catalog_id: str,
        attempt: int,
        arguments: dict[str, Any],
    ) -> AgentToolAttemptRecord:
        with self._sessions() as session:
            self._begin_immediate(session)
            self._require_claim(session, lease)
            record = AgentToolAttemptRecord(
                run_id=lease.run_id,
                epoch_id=epoch_id,
                tool_call_id=tool_call_id,
                catalog_id=catalog_id,
                attempt=attempt,
                arguments_json=arguments,
            )
            session.add(record)
            session.commit()
            session.refresh(record)
            return record

    def finish_tool_attempt(
        self,
        attempt_id: str,
        *,
        status: str,
        result: Any = None,
        result_ref: str | None = None,
        failure_category: str | None = None,
        retryable: bool = False,
        error: str | None = None,
    ) -> None:
        with self._sessions() as session:
            record = session.get(AgentToolAttemptRecord, attempt_id)
            if record is None:
                raise NotFoundError("Tool attempt was not found.")
            record.status = status
            record.result_json = result
            record.result_ref = result_ref
            record.failure_category = failure_category
            record.retryable = retryable
            record.error = error
            record.finished_at = utcnow()
            session.commit()

    def finish_tool_attempt_owned(
        self,
        lease: RunLease,
        attempt_id: str,
        *,
        status: str,
        result: Any = None,
        result_ref: str | None = None,
        failure_category: str | None = None,
        retryable: bool = False,
        error: str | None = None,
    ) -> None:
        with self._sessions() as session:
            self._begin_immediate(session)
            self._require_claim(session, lease)
            record = session.get(AgentToolAttemptRecord, attempt_id)
            if record is None or record.run_id != lease.run_id:
                raise NotFoundError("Tool attempt was not found.")
            record.status = status
            record.result_json = result
            record.result_ref = result_ref
            record.failure_category = failure_category
            record.retryable = retryable
            record.error = error
            record.finished_at = utcnow()
            session.commit()

    def cancel_requested(self, run_id: str) -> bool:
        with self._sessions() as session:
            record = session.get(AgentRunRecord, run_id)
            if record is None:
                return False
            return record.cancel_requested

    def exists(self, run_id: str) -> bool:
        with self._sessions() as session:
            return session.get(AgentRunRecord, run_id) is not None

    def get_goal_state(self, run_id: str) -> dict[str, Any]:
        with self._sessions() as session:
            record = session.get(AgentGoalStateRecord, run_id)
            return dict(record.state_json) if record is not None else {}

    def save_goal_state(
        self,
        run_id: str,
        *,
        status: str,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        with self._sessions() as session:
            record = session.get(AgentGoalStateRecord, run_id)
            if record is None:
                record = AgentGoalStateRecord(
                    run_id=run_id,
                    status=status,
                    state_json=state,
                )
                session.add(record)
            else:
                record.version += 1
                record.status = status
                record.state_json = state
                record.updated_at = utcnow()
            session.commit()
            session.refresh(record)
            return {
                "version": record.version,
                "status": record.status,
                **dict(record.state_json),
            }

    def complete(
        self,
        run_id: str,
        *,
        final_output: Any,
        last_agent_name: str,
        usage: dict[str, Any],
    ) -> None:
        self._update(
            run_id,
            status="completed",
            final_output_json=final_output,
            last_agent_name=last_agent_name,
            usage_json=usage,
            state_json=None,
            finished_at=utcnow(),
        )

    def complete_owned(
        self,
        lease: RunLease,
        *,
        final_output: Any,
        last_agent_name: str,
        usage: dict[str, Any],
    ) -> bool:
        return self._terminal_update_owned(
            lease,
            status="completed",
            require_no_cancel=True,
            final_output_json=final_output,
            last_agent_name=last_agent_name,
            usage_json=usage,
            state_json=None,
        )

    def pause(self, run_id: str, *, state: dict[str, Any]) -> None:
        self._update(run_id, status="paused", state_json=state)

    def pause_owned(self, lease: RunLease, *, state: dict[str, Any]) -> bool:
        return self._terminal_update_owned(
            lease,
            status="paused",
            require_no_cancel=True,
            state_json=state,
            finished=False,
        )

    def fail(self, run_id: str, error: str) -> None:
        self._update(run_id, status="failed", error=error, finished_at=utcnow())

    def fail_owned(self, lease: RunLease, error: str) -> bool:
        return self._terminal_update_owned(
            lease,
            status="failed",
            require_no_cancel=True,
            error=error,
        )

    def cancel(self, run_id: str) -> None:
        self._update(
            run_id,
            status="cancelled",
            cancel_requested=True,
            finished_at=utcnow(),
        )

    def cancel_owned(self, lease: RunLease) -> bool:
        return self._terminal_update_owned(
            lease,
            status="cancelled",
            require_cancel=True,
            cancel_requested=True,
        )

    def request_cancel(self, run_id: str) -> bool:
        with self._sessions() as session:
            result = session.execute(
                update(AgentRunRecord)
                .where(
                    AgentRunRecord.id == run_id,
                    AgentRunRecord.status.in_(("pending", "running")),
                )
                .values(cancel_requested=True)
            )
            session.commit()
            return result.rowcount == 1

    def add_items(self, run_id: str, items: list[dict[str, Any]]) -> None:
        if not items:
            return
        with self._sessions() as session:
            maximum = session.scalar(
                select(func.max(AgentRunItemRecord.item_index)).where(
                    AgentRunItemRecord.run_id == run_id
                )
            )
            start = (maximum if maximum is not None else -1) + 1
            for offset, item in enumerate(items):
                session.add(
                    AgentRunItemRecord(
                        run_id=run_id,
                        item_index=start + offset,
                        item_type=item["type"],
                        agent_name=item["agent_name"],
                        item_json=item,
                    )
                )
            session.commit()

    def add_items_owned(
        self,
        lease: RunLease,
        items: list[dict[str, Any]],
    ) -> None:
        if not items:
            return
        with self._sessions() as session:
            self._begin_immediate(session)
            self._require_claim(session, lease)
            maximum = session.scalar(
                select(func.max(AgentRunItemRecord.item_index)).where(
                    AgentRunItemRecord.run_id == lease.run_id
                )
            )
            start = (maximum if maximum is not None else -1) + 1
            session.add_all(
                AgentRunItemRecord(
                    run_id=lease.run_id,
                    item_index=start + offset,
                    item_type=item["type"],
                    agent_name=item["agent_name"],
                    item_json=item,
                )
                for offset, item in enumerate(items)
            )
            session.commit()

    def next_event_sequence(self, run_id: str) -> int:
        with self._sessions() as session:
            maximum = session.scalar(
                select(func.max(AgentRunEventRecord.sequence)).where(
                    AgentRunEventRecord.run_id == run_id
                )
            )
            return (maximum if maximum is not None else -1) + 1

    def add_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        sequence: int | None = None,
    ) -> AgentRunEventRecord:
        return self.add_events(
            run_id,
            [(event_type, payload)],
            start_sequence=sequence,
        )[0]

    def add_events(
        self,
        run_id: str,
        events: list[tuple[str, dict[str, Any]]],
        *,
        start_sequence: int | None = None,
    ) -> list[AgentRunEventRecord]:
        if not events:
            return []
        with self._sessions() as session:
            if start_sequence is None:
                maximum = session.scalar(
                    select(func.max(AgentRunEventRecord.sequence)).where(
                        AgentRunEventRecord.run_id == run_id
                    )
                )
                start_sequence = (maximum if maximum is not None else -1) + 1
            records = [
                AgentRunEventRecord(
                    run_id=run_id,
                    sequence=start_sequence + offset,
                    event_type=event_type,
                    payload_json=payload,
                )
                for offset, (event_type, payload) in enumerate(events)
            ]
            session.add_all(records)
            session.commit()
            return records

    def add_events_owned(
        self,
        lease: RunLease,
        events: list[tuple[str, dict[str, Any]]],
        *,
        start_sequence: int | None = None,
    ) -> list[AgentRunEventRecord]:
        if not events:
            return []
        with self._sessions() as session:
            self._begin_immediate(session)
            self._require_claim(session, lease)
            if start_sequence is None:
                maximum = session.scalar(
                    select(func.max(AgentRunEventRecord.sequence)).where(
                        AgentRunEventRecord.run_id == lease.run_id
                    )
                )
                start_sequence = (maximum if maximum is not None else -1) + 1
            records = [
                AgentRunEventRecord(
                    run_id=lease.run_id,
                    sequence=start_sequence + offset,
                    event_type=event_type,
                    payload_json=payload,
                )
                for offset, (event_type, payload) in enumerate(events)
            ]
            session.add_all(records)
            session.commit()
            return records

    def events_after(self, run_id: str, sequence: int = -1) -> list[AgentRunEventRecord]:
        with self._sessions() as session:
            return list(
                session.scalars(
                    select(AgentRunEventRecord)
                    .where(
                        AgentRunEventRecord.run_id == run_id,
                        AgentRunEventRecord.sequence > sequence,
                    )
                    .order_by(AgentRunEventRecord.sequence)
                )
            )

    def add_interruption(
        self,
        run_id: str,
        *,
        item_key: str,
        tool_name: str | None,
        item: dict[str, Any],
    ) -> RunInterruptionRecord:
        with self._sessions() as session:
            record = RunInterruptionRecord(
                run_id=run_id,
                item_key=item_key,
                tool_name=tool_name,
                item_json=item,
            )
            session.add(record)
            session.commit()
            session.refresh(record)
            return record

    def resolve_interruption(
        self,
        interruption_id: str,
        *,
        run_id: str,
        status: str,
        response: dict[str, Any],
        state: dict[str, Any],
    ) -> RunInterruptionRecord:
        with self._sessions() as session:
            result = session.execute(
                update(RunInterruptionRecord)
                .where(
                    RunInterruptionRecord.id == interruption_id,
                    RunInterruptionRecord.status == "pending",
                )
                .values(
                    status=status,
                    response_json=response,
                    resolved_at=utcnow(),
                )
            )
            if result.rowcount != 1:
                session.rollback()
                raise ValueError("The interruption is no longer pending.")
            run = session.get(AgentRunRecord, run_id)
            if run is None:
                session.rollback()
                raise NotFoundError("Run was not found.")
            run.status = "pending"
            run.state_json = state
            session.commit()
            record = session.get(RunInterruptionRecord, interruption_id)
            if record is None:
                raise NotFoundError("Run interruption was not found.")
            session.refresh(record)
            return record

    def get_interruption(self, interruption_id: str) -> RunInterruptionRecord:
        with self._sessions() as session:
            record = session.get(RunInterruptionRecord, interruption_id)
            if record is None:
                raise NotFoundError("Run interruption was not found.")
            return record

    def delete(self, run_id: str) -> None:
        with self._sessions() as session:
            record = session.get(AgentRunRecord, run_id)
            if record is None:
                raise NotFoundError("Run was not found.")
            session.delete(record)
            session.commit()

    def delete_many(self, run_ids: list[str]) -> None:
        if not run_ids:
            return
        with self._sessions() as session:
            session.execute(
                delete(RunInterruptionRecord).where(
                    RunInterruptionRecord.run_id.in_(run_ids)
                )
            )
            session.execute(
                delete(AgentRunEventRecord).where(
                    AgentRunEventRecord.run_id.in_(run_ids)
                )
            )
            session.execute(
                delete(AgentRunItemRecord).where(
                    AgentRunItemRecord.run_id.in_(run_ids)
                )
            )
            session.execute(
                delete(AgentRunRecord).where(AgentRunRecord.id.in_(run_ids))
            )
            session.commit()

    def _update(self, run_id: str, **values: Any) -> None:
        with self._sessions() as session:
            record = session.get(AgentRunRecord, run_id)
            if record is None:
                raise NotFoundError("Run was not found.")
            for key, value in values.items():
                setattr(record, key, value)
            session.commit()

    def _update_owned(self, lease: RunLease, **values: Any) -> None:
        with self._sessions() as session:
            self._begin_immediate(session)
            self._require_claim(session, lease)
            record = self._run(session, lease.run_id)
            for key, value in values.items():
                setattr(record, key, value)
            session.commit()

    def _terminal_update_owned(
        self,
        lease: RunLease,
        *,
        status: str,
        require_no_cancel: bool = False,
        require_cancel: bool = False,
        finished: bool = True,
        **values: Any,
    ) -> bool:
        with self._sessions() as session:
            self._begin_immediate(session)
            self._require_claim(session, lease)
            record = self._run(session, lease.run_id)
            if record.status not in {"pending", "running"}:
                return False
            if require_no_cancel and record.cancel_requested:
                return False
            if require_cancel and not record.cancel_requested:
                return False
            record.status = status
            if finished:
                record.finished_at = utcnow()
            for key, value in values.items():
                setattr(record, key, value)
            session.commit()
            return True

    @staticmethod
    def _lease(record: AgentRunClaimRecord) -> RunLease:
        return RunLease(
            run_id=record.run_id,
            owner_id=record.owner_id,
            generation=record.generation,
            token=record.token,
            claimed_at=record.claimed_at,
            expires_at=record.expires_at,
        )

    @staticmethod
    def _run(session: Session, run_id: str) -> AgentRunRecord:
        record = session.get(AgentRunRecord, run_id)
        if record is None:
            raise NotFoundError("Run was not found.")
        return record

    @staticmethod
    def _owns_claim(
        session: Session,
        lease: RunLease,
        *,
        now: datetime,
    ) -> bool:
        return (
            session.scalar(
                select(func.count())
                .select_from(AgentRunClaimRecord)
                .where(
                    AgentRunClaimRecord.run_id == lease.run_id,
                    AgentRunClaimRecord.owner_id == lease.owner_id,
                    AgentRunClaimRecord.generation == lease.generation,
                    AgentRunClaimRecord.token == lease.token,
                    AgentRunClaimRecord.expires_at > now,
                )
            )
            == 1
        )

    def _require_claim(self, session: Session, lease: RunLease) -> None:
        if not self._owns_claim(session, lease, now=utcnow()):
            raise LeaseOwnershipError(
                f"Run {lease.run_id} claim generation {lease.generation} is no longer owned."
            )

    @staticmethod
    def _begin_immediate(session: Session) -> None:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")

    @staticmethod
    def _abandon_incomplete_epochs(session: Session, run_id: str) -> None:
        epochs = session.scalars(
            select(AgentRunEpochRecord).where(
                AgentRunEpochRecord.run_id == run_id,
                AgentRunEpochRecord.status == "running",
            )
        )
        for epoch in epochs:
            epoch.status = "abandoned"
            epoch.terminal_reason = "process_interrupted"
            epoch.finished_at = utcnow()
        attempts = session.scalars(
            select(AgentToolAttemptRecord).where(
                AgentToolAttemptRecord.run_id == run_id,
                AgentToolAttemptRecord.status == "started",
            )
        )
        for attempt in attempts:
            attempt.status = "unknown_outcome"
            attempt.failure_category = "process_interrupted"
            attempt.error = "Process stopped before the tool outcome was persisted."
            attempt.finished_at = utcnow()

    @staticmethod
    def _load_relations(record: AgentRunRecord) -> None:
        _ = record.items
        _ = record.events
        _ = record.interruptions
        _ = record.epochs
        _ = record.tool_attempts
        _ = record.goal_state


def _merge_usage(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged = dict(left)
    for key, value in right.items():
        current = merged.get(key)
        if (
            isinstance(current, (int, float))
            and not isinstance(current, bool)
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        ):
            merged[key] = current + value
        elif isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _merge_usage(current, value)
        elif isinstance(current, list) and isinstance(value, list):
            merged[key] = [*current, *value][-100:]
        else:
            merged[key] = value
    return merged
