from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.persistence.database import Base, JSONText
from backend.utils import utcnow


class LiteratureReview(Base):
    __tablename__ = "literature_reviews"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    specification: Mapped[Any] = mapped_column(JSONText)
    state: Mapped[Any] = mapped_column(JSONText, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReviewCandidate(Base):
    __tablename__ = "review_candidates"
    __table_args__ = (UniqueConstraint("review_id", "candidate_key"),)

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    review_id: Mapped[str] = mapped_column(ForeignKey("literature_reviews.id"), index=True)
    candidate_key: Mapped[str] = mapped_column(String(512))
    content: Mapped[Any] = mapped_column(JSONText)
    rank: Mapped[int] = mapped_column(Integer, default=0)
    selected: Mapped[int] = mapped_column(Integer, default=0)


class ReviewJob(Base):
    __tablename__ = "review_jobs"
    __table_args__ = (UniqueConstraint("review_id", "job_key"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    review_id: Mapped[str] = mapped_column(ForeignKey("literature_reviews.id"), index=True)
    job_key: Mapped[str] = mapped_column(String(512))
    stage: Mapped[str] = mapped_column(String(32))
    content: Mapped[Any] = mapped_column(JSONText)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ScholarlyCache(Base):
    __tablename__ = "scholarly_query_cache"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    content: Mapped[Any] = mapped_column(JSONText)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
