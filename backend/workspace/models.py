from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.persistence.database import Base, JSONText


class WorkspaceEntry(Base):
    __tablename__ = "workspace_entries"
    __table_args__ = (
        Index("ix_workspace_entries_kind_modified", "kind", "modified_at"),
        Index("ix_workspace_entries_paper_id", "paper_id"),
        Index("ix_workspace_entries_note_id", "note_id"),
    )

    path: Mapped[str] = mapped_column(String(512), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), default="file")
    media_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(Integer)
    modified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    tags_json: Mapped[Any] = mapped_column(JSONText, default=list)
    tags_text: Mapped[str] = mapped_column(Text, default="\n")
    paper_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    paper_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    note_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    note_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    search_content: Mapped[str] = mapped_column(Text, default="")
