from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.persistence import Base, JSONText
from backend.core.time import utcnow


class FunctionToolRecord(Base):
    __tablename__ = "function_tool_definitions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    revisions: Mapped[list["FunctionToolRevision"]] = relationship(
        back_populates="definition",
        order_by="FunctionToolRevision.revision",
        cascade="all, delete-orphan",
    )


class FunctionToolRevision(Base):
    __tablename__ = "function_tool_revisions"
    __table_args__ = (
        UniqueConstraint("definition_id", "revision", name="uq_function_tool_revision_number"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    definition_id: Mapped[str] = mapped_column(
        ForeignKey("function_tool_definitions.id", ondelete="CASCADE")
    )
    revision: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text, default="")
    parameters_schema_json: Mapped[Any] = mapped_column(JSONText)
    output_schema_json: Mapped[Any | None] = mapped_column(JSONText, nullable=True)
    code: Mapped[str] = mapped_column(Text)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    definition: Mapped[FunctionToolRecord] = relationship(back_populates="revisions")
