from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.persistence import Base, JSONText
from backend.core.time import utcnow


class AgentRecord(Base):
    __tablename__ = "agent_definitions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_template: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    revisions: Mapped[list["AgentRevision"]] = relationship(
        back_populates="agent",
        order_by="AgentRevision.revision",
        cascade="all, delete-orphan",
    )


class AgentRevision(Base):
    __tablename__ = "agent_revisions"
    __table_args__ = (
        UniqueConstraint("agent_id", "revision", name="uq_agent_revision_number"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_id: Mapped[str] = mapped_column(ForeignKey("agent_definitions.id", ondelete="CASCADE"))
    revision: Mapped[int] = mapped_column(Integer)
    blueprint_json: Mapped[Any] = mapped_column(JSONText)
    presentation_json: Mapped[Any] = mapped_column(JSONText, default=dict)
    sdk_version: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    agent: Mapped[AgentRecord] = relationship(back_populates="revisions")
