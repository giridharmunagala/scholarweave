from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.core.config import RUNTIME_VERSION
from backend.persistence import Base, JSONText
from backend.utils import utcnow


class AgentRunRecord(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
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
    runtime_version: Mapped[str] = mapped_column(String(32), default=RUNTIME_VERSION)
    context_window_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    runtime_metadata_json: Mapped[Any] = mapped_column(JSONText, default=dict)
    completion_policy_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
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
    epochs: Mapped[list["AgentRunEpochRecord"]] = relationship(
        order_by="AgentRunEpochRecord.epoch_index",
        cascade="all, delete-orphan",
    )
    tool_attempts: Mapped[list["AgentToolAttemptRecord"]] = relationship(
        order_by="AgentToolAttemptRecord.started_at",
        cascade="all, delete-orphan",
    )
    goal_state: Mapped["AgentGoalStateRecord | None"] = relationship(
        cascade="all, delete-orphan",
        uselist=False,
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


class AgentRunClaimRecord(Base):
    __tablename__ = "agent_run_claims"

    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), primary_key=True
    )
    owner_id: Mapped[str] = mapped_column(String(36), index=True)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    token: Mapped[str] = mapped_column(String(36), unique=True)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class AgentRunEpochRecord(Base):
    __tablename__ = "agent_run_epochs"
    __table_args__ = (
        UniqueConstraint("run_id", "epoch_index", name="uq_run_epoch_index"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True
    )
    epoch_index: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="running")
    input_json: Mapped[Any] = mapped_column(JSONText)
    usage_json: Mapped[Any] = mapped_column(JSONText, default=dict)
    terminal_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentToolAttemptRecord(Base):
    __tablename__ = "agent_tool_attempts"
    __table_args__ = (
        UniqueConstraint(
            "epoch_id",
            "tool_call_id",
            "attempt",
            name="uq_epoch_tool_attempt",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True
    )
    epoch_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_run_epochs.id", ondelete="SET NULL"), nullable=True
    )
    tool_call_id: Mapped[str] = mapped_column(String(255))
    catalog_id: Mapped[str] = mapped_column(String(255))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), default="started")
    arguments_json: Mapped[Any] = mapped_column(JSONText)
    result_json: Mapped[Any | None] = mapped_column(JSONText, nullable=True)
    result_ref: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    failure_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentGoalStateRecord(Base):
    __tablename__ = "agent_goal_states"

    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), primary_key=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), default="active")
    state_json: Mapped[Any] = mapped_column(JSONText, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
