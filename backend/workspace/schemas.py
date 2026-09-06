from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class WorkspaceSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


WorkspaceKind = Literal["note", "paper_summary", "paper_notes", "paper_file", "file"]


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


class WorkspaceSearchResponse(WorkspaceFileResponse):
    score: float | None = None
    excerpt: str | None = None


class WorkspaceIndexResponse(WorkspaceSchema):
    engine: Literal["sqlite-fts5-bm25"]
    indexed_files: int
    removed_files: int | None = None


class WorkspaceFileWriteRequest(WorkspaceSchema):
    path: str = Field(min_length=1, max_length=512)
    content: Any
    tags: list[str] | None = None


class WorkspaceNoteCreateRequest(WorkspaceSchema):
    name: str = Field(min_length=1, max_length=300)
    content: str = ""
    tags: list[str] = Field(default_factory=list, max_length=32)
