from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.core.time import utcnow
from backend.persistence.database import Base, JSONText


class ProviderProfile(Base):
    __tablename__ = "provider_profiles"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    name: Mapped[str] = mapped_column(String(120), unique=True)
    kind: Mapped[str] = mapped_column(String(32))
    base_url: Mapped[str] = mapped_column(String(1024))
    api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(16), default="active")
    models_json: Mapped[Any] = mapped_column(JSONText, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )
