from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from backend.core.time import utcnow
from backend.direct_agents.models import (
    DirectAgentConversationRecord,
    PaperPageDecisionRecord,
    PaperSummaryRecord,
)
from backend.documents.models import Document


class DirectAgentRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def create_conversation_scope(
        self,
        conversation_id: str,
        *,
        agent_key: str,
        document_ids: list[str],
    ) -> DirectAgentConversationRecord:
        with self._sessions() as session:
            record = DirectAgentConversationRecord(
                conversation_id=conversation_id,
                agent_key=agent_key,
                document_ids_json=document_ids,
            )
            session.add(record)
            session.commit()
            session.refresh(record)
            return record

    def get_conversation_scope(self, conversation_id: str) -> DirectAgentConversationRecord:
        with self._sessions() as session:
            record = session.get(DirectAgentConversationRecord, conversation_id)
            if record is None:
                raise ValueError("Direct-agent conversation was not found.")
            return record

    def list_conversation_scopes(self) -> list[DirectAgentConversationRecord]:
        with self._sessions() as session:
            return list(session.scalars(select(DirectAgentConversationRecord)))

    def delete_conversation_scope(self, conversation_id: str) -> None:
        with self._sessions() as session:
            session.execute(
                delete(DirectAgentConversationRecord).where(
                    DirectAgentConversationRecord.conversation_id == conversation_id
                )
            )
            session.commit()

    def clear_document_analysis(self, document_id: str) -> None:
        with self._sessions() as session:
            session.execute(
                delete(PaperPageDecisionRecord).where(
                    PaperPageDecisionRecord.document_id == document_id
                )
            )
            session.execute(
                delete(PaperSummaryRecord).where(
                    PaperSummaryRecord.document_id == document_id
                )
            )
            session.commit()

    def page_decisions(self, document_id: str) -> dict[int, str]:
        with self._sessions() as session:
            rows = session.scalars(
                select(PaperPageDecisionRecord).where(
                    PaperPageDecisionRecord.document_id == document_id
                )
            )
            return {row.page_number: row.decision for row in rows}

    def save_page_decisions(
        self,
        document_id: str,
        decisions: list[dict[str, Any]],
    ) -> int:
        now = utcnow()
        with self._sessions() as session:
            for decision in decisions:
                statement = sqlite_insert(PaperPageDecisionRecord).values(
                    id=str(uuid.uuid4()),
                    document_id=document_id,
                    page_number=int(decision["page_number"]),
                    decision=str(decision["decision"]),
                    reason=str(decision["reason"]),
                    updated_at=now,
                )
                statement = statement.on_conflict_do_update(
                    index_elements=["document_id", "page_number"],
                    set_={
                        "decision": statement.excluded.decision,
                        "reason": statement.excluded.reason,
                        "updated_at": now,
                    },
                )
                session.execute(statement)
            session.commit()
        return len(decisions)

    def save_summary(
        self,
        document_id: str,
        *,
        contribution: str,
        contributions_detail: str,
        experimentation_results: str,
        open_areas: list[dict[str, str]],
    ) -> None:
        now = utcnow()
        with self._sessions() as session:
            statement = sqlite_insert(PaperSummaryRecord).values(
                document_id=document_id,
                contribution=contribution,
                contributions_detail=contributions_detail,
                experimentation_results=experimentation_results,
                open_areas_json=open_areas,
                updated_at=now,
            )
            statement = statement.on_conflict_do_update(
                index_elements=["document_id"],
                set_={
                    "contribution": statement.excluded.contribution,
                    "contributions_detail": statement.excluded.contributions_detail,
                    "experimentation_results": statement.excluded.experimentation_results,
                    "open_areas_json": statement.excluded.open_areas_json,
                    "updated_at": now,
                },
            )
            session.execute(statement)
            session.commit()

    def list_summaries(self) -> list[dict[str, Any]]:
        with self._sessions() as session:
            rows = session.execute(
                select(PaperSummaryRecord, Document.title)
                .join(Document, Document.id == PaperSummaryRecord.document_id)
                .order_by(PaperSummaryRecord.updated_at.desc())
            )
            return [
                {
                    "document_id": summary.document_id,
                    "title": title,
                    "contribution": summary.contribution,
                    "contributions_detail": summary.contributions_detail,
                    "experimentation_results": summary.experimentation_results,
                    "open_areas": summary.open_areas_json or [],
                }
                for summary, title in rows
            ]
