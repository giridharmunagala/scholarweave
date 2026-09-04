from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.persistence import Base, JSONText
from backend.utils import utcnow


class ConversationRecord(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title: Mapped[str] = mapped_column(String(120), default="New conversation")
    kind: Mapped[str] = mapped_column(String(32), default="agent")
    model_reference_json: Mapped[Any] = mapped_column(JSONText, default=dict)
    session_policy_json: Mapped[Any] = mapped_column(JSONText, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="active")
    last_message_preview: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
