from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RepositorySchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RepositoryLinkRequest(RepositorySchema):
    root: str = Field(min_length=1, max_length=4096)
    authorize_root: Literal[True]


class RepositoryEditRequest(RepositorySchema):
    path: str
    content: str = Field(max_length=262144)
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ArtifactLinkRequest(RepositorySchema):
    path: str
    experiment_id: str
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    producing_revision: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{40,64}$")
    note: str = Field(default="", max_length=2000)


class ArtifactPromotionRequest(RepositorySchema):
    expected_results_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation: str = Field(default="", max_length=4000)


class RepositoryEntry(RepositorySchema):
    path: str
    name: str
    directory: bool
    size_bytes: int


class RepositoryListing(RepositorySchema):
    path: str
    entries: list[RepositoryEntry]
    truncated: bool


class RepositoryPreview(RepositorySchema):
    path: str
    sha256: str
    size_bytes: int
    kind: Literal["text", "image", "pdf", "binary"]
    content: str | None = None
    editable: bool = False


class RepositoryArtifact(RepositorySchema):
    id: str
    path: str
    experiment_id: str
    sha256: str
    size_bytes: int
    linked_at: str
    observed_revision: str | None
    producing_revision: str | None
    provenance: str
    note: str
    status: Literal["unchecked", "unchanged", "changed", "unavailable"] = "unchecked"


class RepositoryLink(RepositorySchema):
    id: str
    root: str
    linked_at: str
    current_revision: str | None = None
    artifacts: list[RepositoryArtifact] = Field(default_factory=list)
