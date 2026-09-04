from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.utils import utcnow
from backend.persistence.database import Base, JSONText


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class PaperFolder(Base, TimestampMixin):
    __tablename__ = "paper_folders"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    name: Mapped[str] = mapped_column(String(100), unique=True)


class Document(Base, TimestampMixin):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    title: Mapped[str] = mapped_column(String(255))
    source_filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(
        String(128),
        default="application/pdf",
    )
    status: Mapped[str] = mapped_column(String(32), default="uploaded")
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_json: Mapped[Any] = mapped_column(JSONText, default=dict)

    artifacts: Mapped[list["Artifact"]] = relationship(back_populates="document")
    chunks: Mapped[list["DocumentChunk"]] = relationship(back_populates="document")


class Artifact(Base, TimestampMixin):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    document_id: Mapped[str | None] = mapped_column(
        ForeignKey("documents.id"),
        nullable=True,
    )
    # Historical run IDs are retained as data only. New SDK run ownership is recorded
    # in metadata_json["agent_run_id"], avoiding a second runtime relationship.
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    owner_type: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(64))
    relative_path: Mapped[str] = mapped_column(String(512), unique=True)
    media_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    metadata_json: Mapped[Any] = mapped_column(JSONText, default=dict)

    document: Mapped[Document | None] = relationship(back_populates="artifacts")


class DocumentChunk(Base, TimestampMixin):
    __tablename__ = "document_chunks"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"))
    artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id"),
        nullable=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer)
    section_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    page_start: Mapped[int] = mapped_column(Integer)
    page_end: Mapped[int] = mapped_column(Integer)
    citation: Mapped[str] = mapped_column(String(255))
    text: Mapped[str] = mapped_column(Text)
    embedding_json: Mapped[Any | None] = mapped_column(JSONText, nullable=True)
    metadata_json: Mapped[Any] = mapped_column(JSONText, default=dict)

    document: Mapped[Document] = relationship(back_populates="chunks")
