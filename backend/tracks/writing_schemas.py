from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WritingSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceInput(WritingSchema):
    document_id: str
    page: int = Field(ge=1)
    quote: str = Field(min_length=1, max_length=10000)
    relation: Literal["supports", "contradicts", "context"] = "supports"


class ClaimInput(WritingSchema):
    statement: str = Field(min_length=1, max_length=5000)
    interpretation: str = Field(default="", max_length=10000)
    evidence: list[EvidenceInput] = Field(default_factory=list, max_length=30)
    expected_sha256: str | None = Field(pattern=r"^[0-9a-f]{64}$")


class ClaimEvidence(EvidenceInput):
    paper_title: str
    source_version: str
    page_sha256: str
    source_status: Literal["current", "changed", "missing"] = "current"


class ResearchClaim(WritingSchema):
    id: str
    statement: str
    interpretation: str
    evidence: list[ClaimEvidence]
    created_at: datetime
    updated_at: datetime


class ClaimCollection(WritingSchema):
    sha256: str | None
    claims: list[ResearchClaim]


class WritingRevisionRequest(WritingSchema):
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ManuscriptResponse(WritingSchema):
    content: str
    sha256: str | None
    path: str


class ManuscriptWriteRequest(WritingSchema):
    content: str = Field(max_length=1_000_000)
    expected_sha256: str | None = Field(pattern=r"^[0-9a-f]{64}$")


class ManuscriptAudit(WritingSchema):
    citation_keys: list[str]
    claim_ids: list[str]
    warnings: list[str]


class EvidencePageResponse(WritingSchema):
    document_id: str
    title: str
    page: int
    text: str
    source_version: str
