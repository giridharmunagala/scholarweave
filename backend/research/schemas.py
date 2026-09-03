from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ResearchSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


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
