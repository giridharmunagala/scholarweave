from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

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


class DocumentResponse(ResearchSchema):
    id: str
    title: str
    source_filename: str
    content_type: str
    status: str
    page_count: int | None
    metadata: dict[str, Any]
    artifacts: list[ArtifactResponse]
    chunks: list[DocumentChunkResponse]
    created_at: datetime
    updated_at: datetime


class IngestionOptionsResponse(ResearchSchema):
    total_pages: int
    embedded_text_pages: int
    embedded_text_ratio: float
    recommended_mode: Literal["embedded", "ocr"]
    ocr_available: bool
    ocr_engine: Literal["tesseract", "docling"]


class ArtifactContentResponse(ResearchSchema):
    artifact: ArtifactResponse
    content: Any
