from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.conversations.models import ConversationRecord
from backend.core.errors import NotFoundError
from backend.runs.models import AgentRunRecord

_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{2,}")


class ConversationMemoryService:
    """Conservative lexical lookup over completed local conversation turns."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def search(
        self,
        query: str,
        *,
        exclude_conversation_id: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        phrase = " ".join(query.casefold().split())
        terms = set(_WORD.findall(phrase))
        if not phrase or not terms:
            return []
        with self._sessions() as session:
            statement = (
                select(AgentRunRecord, ConversationRecord)
                .join(
                    ConversationRecord,
                    AgentRunRecord.conversation_id == ConversationRecord.id,
                )
                .where(
                    AgentRunRecord.status == "completed",
                    AgentRunRecord.final_output_json.is_not(None),
                )
                .order_by(AgentRunRecord.finished_at.desc())
                .limit(250)
            )
            if exclude_conversation_id is not None:
                statement = statement.where(
                    AgentRunRecord.conversation_id != exclude_conversation_id
                )
            rows = list(session.execute(statement))

        matches: list[dict[str, Any]] = []
        for run, conversation in rows:
            input_text = _text(run.input_json)
            output_text = _text(run.final_output_json)
            searchable = f"{conversation.title}\n{input_text}\n{output_text}".casefold()
            matched_terms = sorted(term for term in terms if term in searchable)
            coverage = len(matched_terms) / len(terms)
            exact = phrase in searchable
            if not exact and coverage < 0.6:
                continue
            matches.append(
                {
                    "run_id": run.id,
                    "conversation_id": conversation.id,
                    "conversation_title": conversation.title,
                    "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                    "match_score": round(coverage + (1.0 if exact else 0.0), 3),
                    "matched_terms": matched_terms,
                    "request": _snippet(input_text, 280),
                    "answer_preview": _snippet(output_text, 500),
                }
            )
        matches.sort(
            key=lambda item: (item["match_score"], item["finished_at"] or ""),
            reverse=True,
        )
        return matches[:limit]

    def read(self, run_id: str) -> dict[str, Any]:
        with self._sessions() as session:
            row = session.execute(
                select(AgentRunRecord, ConversationRecord)
                .join(
                    ConversationRecord,
                    AgentRunRecord.conversation_id == ConversationRecord.id,
                )
                .where(AgentRunRecord.id == run_id)
            ).first()
            if row is None:
                raise NotFoundError("Conversation memory was not found.")
            run, conversation = row
            if run.status != "completed":
                raise ValueError("Only completed conversation turns can be reused.")
            return {
                "run_id": run.id,
                "conversation_id": conversation.id,
                "conversation_title": conversation.title,
                "request": run.input_json,
                "answer": run.final_output_json,
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            }


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _snippet(value: str, limit: int) -> str:
    collapsed = " ".join(value.split())
    return collapsed if len(collapsed) <= limit else f"{collapsed[: limit - 1]}…"
