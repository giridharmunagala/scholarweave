from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.core.time import utcnow
from backend.persistence import Base, JSONText


class DirectAgentConversationRecord(Base):
    __tablename__ = "direct_agent_conversations"

    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    agent_key: Mapped[str] = mapped_column(String(64))
    document_ids_json: Mapped[Any] = mapped_column(JSONText, default=list)


class PaperPageDecisionRecord(Base):
    __tablename__ = "paper_page_decisions"
    __table_args__ = (
        UniqueConstraint("document_id", "page_number", name="uq_paper_page_decision"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"),
        index=True,
    )
    page_number: Mapped[int] = mapped_column(Integer)
    decision: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class PaperSummaryRecord(Base):
    __tablename__ = "paper_summaries"

    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"),
        primary_key=True,
    )
    contribution: Mapped[str] = mapped_column(Text)
    contributions_detail: Mapped[str] = mapped_column(Text)
    experimentation_results: Mapped[str] = mapped_column(Text)
    open_areas_json: Mapped[Any] = mapped_column(JSONText, default=list)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )
