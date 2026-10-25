from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class WorkspaceSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


WorkspaceKind = Literal["note", "paper_summary", "paper_notes", "paper_file", "track_section", "file"]


class WorkspaceFileResponse(WorkspaceSchema):
    path: str
    name: str
    media_type: str
    size_bytes: int
    modified_at: datetime
    tags: list[str]
    paper_id: str | None = None
    paper_name: str | None = None
    note_id: str | None = None
    note_name: str | None = None
    kind: str


class WorkspaceFileContentResponse(WorkspaceFileResponse):
    content: Any
    sha256: str | None = None


class WorkspaceSearchResponse(WorkspaceFileResponse):
    score: float | None = None
    excerpt: str | None = None


class ReaderNoteResponse(WorkspaceSchema):
    id: str
    paper_id: str
    paper_name: str
    path: str
    page: int
    text: str
    sha256: str
    connection_ids: list[str]
    match_type: Literal["direct", "connected"]
    connected_via: list[str]


class ReaderNotePage(WorkspaceSchema):
    items: list[ReaderNoteResponse]
    total: int
    next_offset: int | None


class ReaderNoteConnectionRequest(WorkspaceSchema):
    source_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class WorkspaceIndexResponse(WorkspaceSchema):
    engine: Literal["sqlite-fts5-bm25"]
    indexed_files: int
    removed_files: int | None = None


class WorkspaceFileWriteRequest(WorkspaceSchema):
    path: str = Field(min_length=1, max_length=512)
    content: Any
    tags: list[str] | None = None
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class WorkspaceNoteEditRequest(WorkspaceSchema):
    path: str = Field(min_length=1, max_length=512)
    operation: Literal["append", "replace", "insert_after", "overwrite"] = "append"
    content: str
    selection: str | None = None
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class WorkspaceNoteCreateRequest(WorkspaceSchema):
    name: str = Field(min_length=1, max_length=300)
    content: str = ""
    tags: list[str] = Field(default_factory=list, max_length=32)


class WorkspaceNoteCleanupCandidate(WorkspaceSchema):
    path: str = Field(min_length=1, max_length=512)
    size_bytes: int = Field(ge=0, lt=1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class WorkspaceNoteCleanupIssue(WorkspaceSchema):
    path: str
    reason: str


class WorkspaceNoteCleanupPreview(WorkspaceSchema):
    threshold_bytes: Literal[1024] = 1024
    candidates: list[WorkspaceNoteCleanupCandidate]
    failed: list[WorkspaceNoteCleanupIssue]


class WorkspaceNoteCleanupRequest(WorkspaceSchema):
    candidates: list[WorkspaceNoteCleanupCandidate]


class WorkspaceNoteCleanupResponse(WorkspaceSchema):
    deleted_paths: list[str]
    skipped: list[WorkspaceNoteCleanupIssue]
    failed: list[WorkspaceNoteCleanupIssue]
