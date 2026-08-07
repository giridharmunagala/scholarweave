from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.agents.blueprint import ModelReferenceSpec, SessionPolicySpec
from backend.runs.schemas import RunResponse


class ConversationSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionItemResponse(ConversationSchema):
    type: str
    role: str | None
    text: str | None
    raw: Any


class ConversationCreateRequest(ConversationSchema):
    title: str = Field(default="New conversation", min_length=1, max_length=120)
    agent_revision_id: str


class BuilderConversationCreateRequest(ConversationSchema):
    title: str = Field(default="New builder chat", min_length=1, max_length=120)
    model_reference: ModelReferenceSpec = Field(default_factory=ModelReferenceSpec)


class ConversationResponse(ConversationSchema):
    id: str
    title: str
    kind: Literal["agent", "autonomous", "builder", "direct_agent"]
    agent_revision_id: str | None
    model_reference: ModelReferenceSpec
    session_policy: SessionPolicySpec
    status: str
    last_message_preview: str
    created_at: datetime
    updated_at: datetime


class ConversationDetailResponse(ConversationResponse):
    items: list[SessionItemResponse]


class ConversationMessageRequest(ConversationSchema):
    content: str = Field(min_length=1, max_length=100_000)


class ConversationMessageResponse(ConversationSchema):
    conversation: ConversationResponse
    run: RunResponse
