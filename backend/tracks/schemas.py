from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

TrackSection = Annotated[str, Field(pattern=r"^(problem|literature|experiments|results|exploration/[a-z0-9][a-z0-9_-]{0,79})$")]
ExperimentStatus = Literal["Suggested", "Planned", "Running", "Completed", "Blocked", "Cancelled"]


class TrackSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class SectionMetadata(TrackSchema):
    section: TrackSection
    path: str
    sha256: str
    modified_at: datetime


class TrackSectionResponse(SectionMetadata):
    content: str


class ExperimentResponse(TrackSchema):
    id: str
    experiment: str
    status: ExperimentStatus
    success_criterion: str
    evidence: str


class TrackDetailResponse(TrackSchema):
    id: str
    title: str
    folder_path: str
    created_at: datetime
    updated_at: datetime
    paper_ids: list[str]
    conversation_ids: list[str]
    sections: list[SectionMetadata]
    exploration: list[SectionMetadata] = Field(default_factory=list)
    experiments: list[ExperimentResponse]
    status_counts: dict[str, int]
    warnings: list[str]


class TrackCreateRequest(TrackSchema):
    title: str = Field(min_length=1, max_length=300)


class TrackSectionWriteRequest(TrackSchema):
    content: str
    expected_sha256: str | None = Field(pattern=r"^[0-9a-f]{64}$")


class RevisionResponse(TrackSchema):
    revision: str
    sha256: str
    created_at: datetime


TrackHistoryEntry = RevisionResponse


class ImportPreviewRequest(TrackSchema):
    section: TrackSection
    content: str


class ImportSectionPreview(TrackSchema):
    section: TrackSection
    current_content: str
    incoming_content: str
    expected_sha256: str | None
    diff: str


class ImportPreviewResponse(TrackSchema):
    preview_id: str
    sections: list[ImportSectionPreview]
    paper_ids: list[str]
    warnings: list[str]


class ImportApplyRequest(TrackSchema):
    preview_id: str
    mode: Literal["merge", "replace"]
    expected_revisions: dict[TrackSection, str | None]
    confirmed_replace: bool = False
    merged_content: dict[TrackSection, str] | None = None
