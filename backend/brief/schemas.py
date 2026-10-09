from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from backend.brief.catalog import SOURCES, TOPICS
from backend.agents.blueprint import ModelReferenceSpec


class BriefPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic_ids: list[str] = Field(default_factory=lambda: list(TOPICS), max_length=7)
    source_ids: list[str] = Field(default_factory=lambda: [source.id for source in SOURCES], max_length=20)
    extra_interests: str = Field(default="", max_length=1000)
    exclude_terms: list[str] = Field(default_factory=list, max_length=30)
    lookback_days: int = Field(default=7, ge=1, le=14)
    summary_model_reference: ModelReferenceSpec | None = None

    @field_validator("topic_ids", "source_ids")
    @classmethod
    def known_ids(cls, value: list[str], info: ValidationInfo) -> list[str]:
        known = TOPICS if info.field_name == "topic_ids" else {source.id for source in SOURCES}
        if not value or any(item not in known for item in value):
            raise ValueError("Select at least one known topic/source.")
        return list(dict.fromkeys(value))

    @field_validator("exclude_terms")
    @classmethod
    def clean_exclusions(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 100 for item in value):
            raise ValueError("Exclusions must contain 1 to 100 characters.")
        return list(dict.fromkeys(item.strip() for item in value))


class BriefSourceResponse(BaseModel):
    id: str
    name: str
    url: str
    kind: str


class BriefTopicResponse(BaseModel):
    id: str
    label: str


class BriefCatalogResponse(BaseModel):
    sources: list[BriefSourceResponse]
    topics: list[BriefTopicResponse]
    preferences: BriefPreferences
    summary_model_reference: ModelReferenceSpec = Field(default_factory=ModelReferenceSpec)


class BriefItemResponse(BaseModel):
    id: str
    title: str
    url: str
    source_id: str
    source_name: str
    kind: Literal["paper", "lab", "engineering", "reading"]
    published_at: datetime | None = None
    announcement: str | None = None
    arxiv_id: str | None = None
    authors: list[str] = Field(default_factory=list)
    abstract: str = ""
    topics: list[str] = Field(default_factory=list)
    downloaded: bool = False
    size_bytes: int = 0
    preview_note: str | None = None
    saved_document_id: str | None = None
    saved_path: str | None = None


class BriefSourceStatus(BaseModel):
    source_id: str
    name: str
    fetched_at: datetime
    count: int
    error: str | None = None
    warning: str | None = None


class BriefHighlightSummary(BaseModel):
    day: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    text: str = Field(min_length=1)
    created_at: datetime
    model_reference: ModelReferenceSpec
    source_hash: str
    item_count: int
    batch_count: int
    input_token_limit: int
    token_counting: str
    workspace_path: str


class BriefSummaryState(BaseModel):
    status: Literal["idle", "running", "ready", "failed", "interrupted"] = "idle"
    summary: BriefHighlightSummary | None = None
    completed_parts: int = 0
    total_parts: int = 0
    error: str | None = None


class BriefSummaryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_reference: ModelReferenceSpec | None = None


class BriefEditionResponse(BaseModel):
    day: str | None = None
    fetched_at: datetime | None = None
    cleared: bool = False
    items: list[BriefItemResponse] = Field(default_factory=list)
    sources: list[BriefSourceStatus] = Field(default_factory=list)
    temporary_bytes: int = 0
    highlights: BriefSummaryState = Field(default_factory=BriefSummaryState)


class BriefRefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    force: bool = False


class BriefPreviewResponse(BaseModel):
    item: BriefItemResponse
    media_type: str
    text: str | None = None
