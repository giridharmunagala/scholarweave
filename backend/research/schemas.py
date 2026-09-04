from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.agents.blueprint import ModelReferenceSpec, ReasoningEffort
from backend.runs.schemas import RunResponse


class ResearchSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PaperSummaryRunRequest(ResearchSchema):
    model_reference: ModelReferenceSpec = Field(default_factory=ModelReferenceSpec)
    reasoning_effort: ReasoningEffort | None = None


class PaperSummaryRunResponse(ResearchSchema):
    run: RunResponse
    prompt_revision: str


class PaperSummaryVersionResponse(ResearchSchema):
    id: str
    document_id: str
    run_id: str
    path: str
    created_at: datetime
    prompt_revision: str | None
    review_summary: str
    citation_count: int
    status: str


class PaperSummaryContentResponse(ResearchSchema):
    version: PaperSummaryVersionResponse
    content: str


class PaperSummaryPromotionResponse(ResearchSchema):
    version: PaperSummaryVersionResponse
    summary_path: str
    content: str


class ArtifactResponse(ResearchSchema):
    id: str
    document_id: str | None
    owner_type: str
    kind: str
    relative_path: str
    media_type: str
    size_bytes: int
    sha256: str
    metadata: dict[str, Any]
    created_at: datetime


class DocumentChunkResponse(ResearchSchema):
    id: str
    chunk_index: int
    section_title: str | None
    page_start: int
    page_end: int
    citation: str
    text: str
    metadata: dict[str, Any]


class DocumentSummaryResponse(ResearchSchema):
    id: str
    title: str
    source_filename: str
    content_type: str
    status: str
    page_count: int | None
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class DocumentResponse(DocumentSummaryResponse):
    artifacts: list[ArtifactResponse]
    chunks: list[DocumentChunkResponse]


class PaperFolderCreateRequest(ResearchSchema):
    name: str = Field(min_length=1, max_length=100)


class PaperFolderAssignmentRequest(ResearchSchema):
    folder_id: str | None


class PaperFolderResponse(ResearchSchema):
    id: str
    name: str
    created_at: datetime
    updated_at: datetime


class IngestionOptionsResponse(ResearchSchema):
    total_pages: int
    embedded_text_pages: int
    embedded_text_ratio: float
    recommended_mode: Literal["embedded", "ocr"]
    ocr_available: bool
    ocr_engine: Literal["tesseract"]


class ArtifactContentResponse(ResearchSchema):
    artifact: ArtifactResponse
    content: Any


class RemotePdfDownloadRequest(ResearchSchema):
    url: str = Field(min_length=1, max_length=2048)
    title: str | None = Field(default=None, max_length=300)


class WebSourceCreateRequest(ResearchSchema):
    url: str = Field(min_length=1, max_length=2048)


class WebSourceNoteRequest(ResearchSchema):
    name: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=200_000)
    tags: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(
        default_factory=list,
        max_length=32,
    )


class WebSourceResponse(ResearchSchema):
    id: str
    url: str
    title: str
    text: str | None = None
    chunk_count: int
    created_at: datetime
    expires_at: datetime


class SavedWebSourceNoteResponse(ResearchSchema):
    path: str
    note_id: str | None
    name: str | None
