from __future__ import annotations

from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
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
from backend.core.time import utcnow


class RunRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def create(
        self,
        *,
        agent_revision_id: str | None,
        conversation_id: str | None,
        agent_name: str,
        input_value: Any,
        blueprint: dict[str, Any],
    ) -> AgentRunRecord:
        with self._sessions() as session:
            record = AgentRunRecord(
                agent_revision_id=agent_revision_id,
                conversation_id=conversation_id,
                agent_name=agent_name,
                input_json=input_value,
                blueprint_json=blueprint,
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

    def claim(self, run_id: str, owner_id: str) -> bool:
        with self._sessions() as session:
            existing = session.get(AgentRunClaimRecord, run_id)
            if existing is not None:
                return False
            session.add(AgentRunClaimRecord(run_id=run_id, owner_id=owner_id))
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return False
            return True

    def release_claim(self, run_id: str, owner_id: str) -> None:
        with self._sessions() as session:
            session.execute(
                delete(AgentRunClaimRecord).where(
                    AgentRunClaimRecord.run_id == run_id,
                    AgentRunClaimRecord.owner_id == owner_id,
                )
            )
            session.commit()

    def clear_stale_claims(self, owner_id: str) -> None:
        with self._sessions() as session:
            session.execute(
                delete(AgentRunClaimRecord).where(
                    AgentRunClaimRecord.owner_id != owner_id
                )
            )
            session.commit()

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

    def abandon_incomplete_epochs(self, run_id: str) -> None:
        with self._sessions() as session:
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

    def pause(self, run_id: str, *, state: dict[str, Any]) -> None:
        self._update(run_id, status="paused", state_json=state)

    def fail(self, run_id: str, error: str) -> None:
        self._update(run_id, status="failed", error=error, finished_at=utcnow())

    def cancel(self, run_id: str) -> None:
        self._update(
            run_id,
            status="cancelled",
            cancel_requested=True,
            finished_at=utcnow(),
        )

    def request_cancel(self, run_id: str) -> None:
        self._update(run_id, cancel_requested=True)

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
