from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.conversations.models import ConversationRecord
from backend.core.errors import NotFoundError
from backend.utils import utcnow


class ConversationRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def create(
        self,
        *,
        title: str,
        kind: str,
        model_reference: dict[str, Any],
        session_policy: dict[str, Any],
    ) -> ConversationRecord:
        with self._sessions() as session:
            record = ConversationRecord(
                title=title,
                kind=kind,
                model_reference_json=model_reference,
                session_policy_json=session_policy,
            )
            session.add(record)
            session.commit()
            session.refresh(record)
            return record

    def list(self, *, kind: str | None = None) -> list[ConversationRecord]:
        with self._sessions() as session:
            statement = select(ConversationRecord).order_by(ConversationRecord.updated_at.desc())
            if kind is not None:
                statement = statement.where(ConversationRecord.kind == kind)
            return list(session.scalars(statement))

    def get(self, conversation_id: str) -> ConversationRecord:
        with self._sessions() as session:
            record = session.get(ConversationRecord, conversation_id)
            if record is None:
                raise NotFoundError("Conversation was not found.")
            return record

    def touch(self, conversation_id: str, *, preview: str) -> ConversationRecord:
        with self._sessions() as session:
            record = session.get(ConversationRecord, conversation_id)
            if record is None:
                raise NotFoundError("Conversation was not found.")
            record.last_message_preview = preview[:500]
            record.updated_at = utcnow()
            session.commit()
            session.refresh(record)
            return record

    def set_title(self, conversation_id: str, *, title: str) -> ConversationRecord:
        with self._sessions() as session:
            record = session.get(ConversationRecord, conversation_id)
            if record is None:
                raise NotFoundError("Conversation was not found.")
            record.title = title
            record.updated_at = utcnow()
            session.commit()
            session.refresh(record)
            return record

    def promote_to_deep_work(self, conversation_id: str) -> ConversationRecord:
        with self._sessions() as session:
            record = session.get(ConversationRecord, conversation_id)
            if record is None:
                raise NotFoundError("Conversation was not found.")
            if record.kind not in {"autonomous", "deep_work"}:
                raise ValueError("Conversation cannot be promoted to Deep Work.")
            if record.kind == "autonomous":
                record.kind = "deep_work"
                record.updated_at = utcnow()
                session.commit()
                session.refresh(record)
            return record

    def delete(self, conversation_id: str) -> None:
        with self._sessions() as session:
            record = session.get(ConversationRecord, conversation_id)
            if record is None:
                raise NotFoundError("Conversation was not found.")
            session.delete(record)
            session.commit()
