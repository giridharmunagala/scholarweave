from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.agents.blueprint import (
    ModelReferenceSpec,
    ReasoningEffort,
    SessionPolicySpec,
)
from backend.runs.schemas import RunResponse

ResearchMode = Literal["research", "learn", "understand", "review"]


class ConversationSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionItemResponse(ConversationSchema):
    type: str
    role: str | None
    text: str | None
    raw: Any


class ResearchConversationCreateRequest(ConversationSchema):
    title: str = Field(default="New research", min_length=1, max_length=120)
    model_reference: ModelReferenceSpec = Field(default_factory=ModelReferenceSpec)


class ConversationResponse(ConversationSchema):
    id: str
    title: str
    kind: Literal["autonomous", "deep_work"]
    model_reference: ModelReferenceSpec
    session_policy: SessionPolicySpec
    status: str
    last_message_preview: str
    created_at: datetime
    updated_at: datetime


class ConversationDetailResponse(ConversationResponse):
    items: list[SessionItemResponse]


class ConversationMessageRequest(ConversationSchema):
    content: str = Field(min_length=1)
    reasoning_effort: ReasoningEffort | None = None
    web_enabled: bool = True
    deep_work: bool = Field(
        default=False,
        description="Permanently enable Deep Work for this conversation.",
    )
    fast_answer: bool = False
    research_mode: ResearchMode | None = Field(
        default=None,
        description=(
            "research: follow the user's intent without automatic summaries or notes; "
            "learn: narrow sourced Q&A; understand: explain a paper and prerequisites; "
            "review: require cited summaries and durable paper notes. "
            "Omitted selects research, or learn for legacy Fast Answer. "
            "All modes are available per turn in Deep Work."
        ),
    )
    web_search_limit: int = Field(default=1, ge=1, le=100)
    context_window_tokens: int | None = Field(default=None, ge=4_096, le=2_000_000)

    @model_validator(mode="after")
    def validate_modes(self) -> "ConversationMessageRequest":
        if self.deep_work and self.fast_answer:
            raise ValueError("Fast Answer and Deep Work cannot be enabled together.")
        if self.fast_answer and self.research_mode not in {None, "learn"}:
            raise ValueError("Fast Answer requires learn mode.")
        return self


class ConversationMessageResponse(ConversationSchema):
    conversation: ConversationResponse
    run: RunResponse
