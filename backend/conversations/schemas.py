from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.agents.blueprint import (
    ModelReferenceSpec,
    ReasoningEffort,
    SessionPolicySpec,
)
from backend.runs.schemas import RunResponse

ResearchMode = Literal["research", "learn", "understand", "review"]
ResponseEffort = Literal["auto", "quick", "thorough"]


class ConversationSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionItemResponse(ConversationSchema):
    type: str
    role: str | None
    text: str | None
    raw: Any


class ResearchConversationCreateRequest(ConversationSchema):
    title: str = Field(default="New chat", min_length=1, max_length=120)
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


class ConversationAttachmentResponse(ConversationSchema):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    path: str
    name: str
    media_type: str
    size_bytes: int
    document_id: str | None


class ConversationMessageRequest(ConversationSchema):
    content: str = Field(min_length=1)
    attachment_paths: list[Annotated[str, Field(min_length=1, max_length=512)]] = Field(
        default_factory=list,
        max_length=10,
        description="Readable workspace paths returned by chat attachment uploads.",
    )
    reasoning_effort: ReasoningEffort | None = None
    web_enabled: bool = True
    response_effort: ResponseEffort | None = Field(
        default=None,
        description=(
            "Per-message effort: auto follows intent; quick uses the smallest sufficient "
            "local or web evidence; thorough enables focused delegation. Does not imply "
            "saved artifacts. Overrides legacy deep_work and fast_answer selectors. "
            "Omitted defaults to auto on the chat endpoint."
        ),
    )
    deep_work: bool = Field(
        default=False,
        description="Legacy per-message alias for thorough effort; never changes conversation kind.",
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
        if self.response_effort is not None:
            return self
        if self.deep_work and self.fast_answer:
            raise ValueError("Fast Answer and Deep Work cannot be enabled together.")
        if self.fast_answer and self.research_mode not in {None, "learn"}:
            raise ValueError("Fast Answer requires learn mode.")
        if self.fast_answer and self.attachment_paths:
            raise ValueError("Turn off Fast Answer to work with attached files.")
        return self


class ConversationMessageResponse(ConversationSchema):
    conversation: ConversationResponse
    run: RunResponse
