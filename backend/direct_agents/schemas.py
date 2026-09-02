from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.agents.blueprint import (
    ModelReferenceSpec,
    ReasoningEffort,
    SessionPolicySpec,
)
from backend.conversations.schemas import SessionItemResponse
from backend.runs.schemas import RunResponse

DirectAgentKey = Literal[
    "summary",
    "open_areas",
    "qa",
    "paper_cleaner",
]


class DirectAgentSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DirectAgentResponse(DirectAgentSchema):
    key: DirectAgentKey
    name: str
    description: str
    requires_document: bool


class DirectConversationCreateRequest(DirectAgentSchema):
    agent_key: DirectAgentKey
    document_ids: list[str] = Field(default_factory=list, max_length=1)
    title: str = Field(default="New agent chat", min_length=1, max_length=120)
    model_reference: ModelReferenceSpec = Field(default_factory=ModelReferenceSpec)


class DirectConversationResponse(DirectAgentSchema):
    id: str
    title: str
    kind: Literal["direct_agent"]
    agent_key: DirectAgentKey
    document_ids: list[str]
    model_reference: ModelReferenceSpec
    session_policy: SessionPolicySpec
    status: str
    last_message_preview: str
    created_at: datetime
    updated_at: datetime


class DirectConversationDetailResponse(DirectConversationResponse):
    items: list[SessionItemResponse]


class DirectConversationMessageRequest(DirectAgentSchema):
    content: str = Field(min_length=1, max_length=100_000)
    reasoning_effort: ReasoningEffort | None = None


class DirectConversationMessageResponse(DirectAgentSchema):
    conversation: DirectConversationResponse
    run: RunResponse
