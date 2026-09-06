from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.agents.blueprint import ModelReferenceSpec, ReasoningEffort
from backend.runs.schemas import RunResponse


class ResearchSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PaperSummaryRunRequest(ResearchSchema):
    model_reference: ModelReferenceSpec = Field(default_factory=ModelReferenceSpec)
    reasoning_effort: ReasoningEffort | None = None
    mode: Literal["overview", "reviewed"] = "reviewed"


class PaperSummaryBatchRequest(PaperSummaryRunRequest):
    document_ids: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1, max_length=50)

    @field_validator("document_ids")
    @classmethod
    def distinct_documents(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or item != item.strip() for item in value):
            raise ValueError("Paper IDs must be nonempty and have no surrounding whitespace.")
        if len(value) != len(set(value)):
            raise ValueError("Each paper may appear only once in a summary batch.")
        return value

    @model_validator(mode="after")
    def explicit_model(self) -> "PaperSummaryBatchRequest":
        if not self.model_reference.provider_profile_id or not self.model_reference.model:
            raise ValueError("Select an explicit provider and model for the whole batch.")
        return self


class PaperSummaryRunResponse(ResearchSchema):
    run: RunResponse
    prompt_revision: str
    document_id: str | None = None


class PaperSummaryBatchResponse(ResearchSchema):
    runs: list[PaperSummaryRunResponse]


class PaperSummaryCoverageResponse(ResearchSchema):
    kind: Literal["pages", "chunks"] | None = None
    checkpointed_batches: int = 0
    exact_spans_path: str | None = None


class PaperSummaryModelResponse(ResearchSchema):
    model: str
    provider_kind: str | None = None
    provider_profile_id: str | None = None


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
    mode: Literal["overview", "reviewed"] | None = None
    review_complete: bool | None = None
    coverage_complete: bool | None = None
    coverage: PaperSummaryCoverageResponse | None = None
    next_start: int | None = None
    next_offset: int = 0
    evidence_path: str | None = None
    model: PaperSummaryModelResponse | str | None = None
    content_hash: str | None = None
    source_hash: str | None = None
    extraction_hash: str | None = None
    source_version: str | None = None
    canonical_path: str | None = None
    canonical_updated: bool | None = None


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
