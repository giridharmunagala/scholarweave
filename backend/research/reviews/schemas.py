from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.agents.blueprint import ModelReferenceSpec

ReviewStage = Literal["scope", "discovery", "screening", "reading_plan", "reading", "references", "reference_screening", "reference_collection", "synthesis", "publication"]
ReviewStatus = Literal["running", "awaiting_approval", "paused", "blocked", "completed"]
STAGES: tuple[ReviewStage, ...] = (
    "scope", "discovery", "screening", "reading_plan", "reading", "references", "reference_screening", "reference_collection", "synthesis", "publication",
)


class ReviewSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkBudget(ReviewSchema):
    screened_papers: int = Field(default=100, ge=1, le=5000)
    read_papers: int = Field(default=20, ge=1, le=5000)
    comparison_passes: int = Field(default=1, ge=1, le=100)


class WorkUsage(ReviewSchema):
    screened_papers: int = 0
    read_papers: int = 0
    comparison_passes: int = 0


class DecisionRole(ReviewSchema):
    model_reference: ModelReferenceSpec
    protocol: Literal["systemone", "generation"] = "systemone"
    input_tokens: int = Field(default=8192, ge=128, le=131_072)


class ReviewCreateRequest(ReviewSchema):
    title: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=20_000)
    main_model: ModelReferenceSpec
    decision_model: DecisionRole | None = None
    conversation_id: str | None = None
    track_id: str | None = None
    web_enabled: bool = True
    openalex_enabled: bool = False

    @model_validator(mode="after")
    def explicit_models(self) -> ReviewCreateRequest:
        references = [self.main_model]
        if self.decision_model:
            references.append(self.decision_model.model_reference)
        if any(not ref.provider_profile_id or not ref.model for ref in references):
            raise ValueError("Select an explicit provider and model for each review role.")
        if not self.title.strip() or not self.objective.strip():
            raise ValueError("The review needs a title and a research objective.")
        return self


class ReviewControlRequest(ReviewSchema):
    expected_revision: int = Field(ge=1)
    action: Literal["approve_plan", "approve_papers", "select_references", "approve_reference_pdfs", "pause", "stop_stage", "resume", "extend_budget", "link_paper"]
    candidate_ids: list[str] | None = None
    reference_matches: dict[str, str] | None = None
    document_id: str | None = None
    allowance_tokens: int | None = Field(default=None, ge=1)
    work_budget: WorkBudget | None = None
    stage: ReviewStage | None = None


class SearchQuery(ReviewSchema):
    provider: Literal["library", "arxiv", "web", "openalex"]
    query: str = Field(min_length=1, max_length=1000)
    pages: int = Field(ge=1)


class ReviewClearRequest(ReviewSchema):
    expected_revision: int = Field(ge=1)


class ReviewClearResponse(ReviewSchema):
    deleted_document_ids: list[str]
    preserved_document_ids: list[str]
    preserved_paths: list[str]


class ReviewScope(ReviewSchema):
    questions: list[str] = Field(min_length=1)
    inclusion_criteria: list[str]
    exclusion_criteria: list[str]
    queries: list[SearchQuery] = Field(min_length=1)
    suggested_allowance_tokens: int = Field(default=1_000_000, ge=1)
    suggested_work_budget: WorkBudget = Field(default_factory=WorkBudget)
    rationale: str


class Screening(ReviewSchema):
    kind: Literal["research", "review", "other", "unknown"]
    relevance: Literal["HIGHLY_RELEVANT", "RELEVANT", "SOMEWHAT_RELEVANT", "IRRELEVANT"]
    reason: str
    matched_questions: list[str]

    @field_validator("relevance", mode="before")
    @classmethod
    def legacy_relevance(cls, value: str) -> str:
        return {
            "core": "HIGHLY_RELEVANT", "relevant": "RELEVANT",
            "background": "SOMEWHAT_RELEVANT", "uncertain": "SOMEWHAT_RELEVANT",
            "unrelated": "IRRELEVANT",
        }.get(value, value)


class CandidateScreening(Screening):
    candidate_id: str


class ScreeningBatch(ReviewSchema):
    candidates: list[CandidateScreening]


class ReferenceSeeds(ReviewSchema):
    candidate_ids: list[str]
    reason: str


class Candidate(ReviewSchema):
    id: str
    title: str
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    openalex_id: str | None = None
    url: str | None = None
    pdf_url: str | None = None
    abstract: str | None = None
    document_id: str | None = None
    source_kind: str | None = None
    references: list[str] = Field(default_factory=list)
    discovery: list[str] = Field(default_factory=list)
    screening: Screening | None = None
    decision: dict[str, Any] | None = None
    selected: bool = False
    error: str | None = None


class CandidatePage(ReviewSchema):
    items: list[Candidate]
    total: int
    next_offset: int | None
    selected_ids: list[str]


class PageChoice(ReviewSchema):
    page: int = Field(ge=1)
    action: Literal["read", "defer"]
    reason: str
    reference_page: bool = False


class ExtractedReference(ReviewSchema):
    title: str = Field(min_length=1)
    authors: list[str]
    year: int | None
    doi: str | None
    arxiv_id: str | None
    quote: str = Field(min_length=1)
    page: int = Field(ge=1)


class ReferenceExtraction(ReviewSchema):
    references: list[ExtractedReference]


class ReviewReference(ReviewSchema):
    id: str
    title: str
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    citing_paper_ids: list[str]
    citing_count: int
    citations: list[dict[str, Any]]
    selected: bool = False
    matches: list[Candidate] = Field(default_factory=list)
    metadata: Candidate | None = None
    screening: Screening | None = None
    decision: dict[str, Any] | None = None
    document_id: str | None = None
    error: str | None = None


class ReadingPlan(ReviewSchema):
    pages: list[PageChoice]


class ReadingGroups(ReviewSchema):
    groups: list[list[int]]


class PartialSummarySection(ReviewSchema):
    title: Literal["Motivation, contribution, and novelty", "Method", "Results", "Open questions"]
    content: str = Field(min_length=1)
    claim_indexes: list[int]


class SelectiveSummary(ReviewSchema):
    sections: list["SynthesisSection"]
    limitations: list[str]


class PaperReview(ReviewSchema):
    candidate_id: str
    title: str = ""
    source_version: str | None
    pages: list[PageChoice]
    read_pages: list[int]
    partial_pages: list[int]
    evidence_count: int
    operation: str | None = None
    findings: list[dict] = Field(default_factory=list)
    partial_summaries: list[dict] = Field(default_factory=list)
    selective_summary: dict | None = None


class EvidenceClaim(ReviewSchema):
    finding: str
    category: Literal["method", "result", "limitation", "contradiction", "context"]
    quote: str = Field(min_length=1)
    page: int = Field(ge=1)


class EvidencePacket(ReviewSchema):
    claims: list[EvidenceClaim]
    limitations: list[str]
    follow_up_pages: list[int]
    partial_summary: list[PartialSummarySection]


class SynthesisSection(ReviewSchema):
    title: str
    content: str
    evidence_ids: list[str]


class GapHypothesis(ReviewSchema):
    hypothesis: str
    basis: Literal["author_stated", "inferred", "coverage_gap"]
    evidence_ids: list[str]
    counter_evidence_ids: list[str]
    caveat: str
    next_check: str


class Synthesis(ReviewSchema):
    sections: list[SynthesisSection]
    gaps: list[GapHypothesis]
    limitations: list[str]


class ReviewResponse(ReviewSchema):
    id: str
    revision: int
    title: str
    objective: str
    status: ReviewStatus
    stage: ReviewStage
    approval: Literal["plan", "papers", "budget", "references", "reference_pdfs"] | None
    main_model: ModelReferenceSpec
    decision_model: DecisionRole | None
    conversation_id: str | None
    track_id: str | None
    scope: ReviewScope | None
    allowance_tokens: int | None
    used_tokens: int
    usage_estimated: bool
    suggested_allowance_tokens: int | None
    work_budget: WorkBudget | None = None
    work_used: WorkUsage = Field(default_factory=WorkUsage)
    suggested_work_budget: WorkBudget | None = None
    candidate_count: int
    screened_count: int = 0
    excluded_count: int = 0
    selected_count: int
    evidence_count: int
    active_run_id: str | None
    stage_history: dict[str, str]
    notices: list[str]
    error: str | None
    output_path: str | None
    created_at: datetime
    updated_at: datetime
