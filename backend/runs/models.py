from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.persistence import Base, JSONText
from backend.runtime.sdk_compat import SUPPORTED_SDK_VERSION
from backend.core.time import utcnow


class AgentRunRecord(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_revisions.id", ondelete="SET NULL"), nullable=True
    )
    conversation_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    agent_name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    input_json: Mapped[Any] = mapped_column(JSONText)
    blueprint_json: Mapped[Any] = mapped_column(JSONText)
    final_output_json: Mapped[Any | None] = mapped_column(JSONText, nullable=True)
    last_agent_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    usage_json: Mapped[Any] = mapped_column(JSONText, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sdk_version: Mapped[str] = mapped_column(String(32), default=SUPPORTED_SDK_VERSION)
    state_json: Mapped[Any | None] = mapped_column(JSONText, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    items: Mapped[list["AgentRunItemRecord"]] = relationship(
        back_populates="run",
        order_by="AgentRunItemRecord.item_index",
        cascade="all, delete-orphan",
    )
    events: Mapped[list["AgentRunEventRecord"]] = relationship(
        back_populates="run",
        order_by="AgentRunEventRecord.sequence",
        cascade="all, delete-orphan",
    )
    interruptions: Mapped[list["RunInterruptionRecord"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
    )


class AgentRunItemRecord(Base):
    __tablename__ = "agent_run_items"
    __table_args__ = (UniqueConstraint("run_id", "item_index", name="uq_run_item_index"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"))
    item_index: Mapped[int] = mapped_column(Integer)
    item_type: Mapped[str] = mapped_column(String(64))
    agent_name: Mapped[str] = mapped_column(String(255))
    item_json: Mapped[Any] = mapped_column(JSONText)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped[AgentRunRecord] = relationship(back_populates="items")


class AgentRunEventRecord(Base):
    __tablename__ = "agent_run_events"
    __table_args__ = (UniqueConstraint("run_id", "sequence", name="uq_run_event_sequence"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"))
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[Any] = mapped_column(JSONText, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped[AgentRunRecord] = relationship(back_populates="events")


class RunInterruptionRecord(Base):
    __tablename__ = "agent_run_interruptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"))
    item_key: Mapped[str] = mapped_column(String(255))
    tool_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    item_json: Mapped[Any] = mapped_column(JSONText)
    response_json: Mapped[Any | None] = mapped_column(JSONText, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    run: Mapped[AgentRunRecord] = relationship(back_populates="interruptions")
