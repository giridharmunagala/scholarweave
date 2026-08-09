from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.core.errors import NotFoundError
from backend.runs.models import (
    AgentRunEventRecord,
    AgentRunItemRecord,
    AgentRunRecord,
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
        status: str,
        response: dict[str, Any],
    ) -> RunInterruptionRecord:
        with self._sessions() as session:
            record = session.get(RunInterruptionRecord, interruption_id)
            if record is None:
                raise NotFoundError("Run interruption was not found.")
            record.status = status
            record.response_json = response
            record.resolved_at = utcnow()
            session.commit()
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
