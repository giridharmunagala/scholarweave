from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.agents.blueprint import AgentBlueprint, ReasoningEffort


class RunSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MessageRunItem(RunSchema):
    type: Literal["message_output_item"]
    agent_name: str
    raw_item: Any
    content: str


class ToolCallRunItem(RunSchema):
    type: Literal["tool_call_item"]
    agent_name: str
    raw_item: Any
    title: str | None = None
    description: str | None = None
    tool_origin: Any = None


class ToolOutputRunItem(RunSchema):
    type: Literal["tool_call_output_item"]
    agent_name: str
    raw_item: Any
    output: Any
    custom_data: Any = None
    tool_origin: Any = None


class HandoffCallRunItem(RunSchema):
    type: Literal["handoff_call_item"]
    agent_name: str
    raw_item: Any


class HandoffOutputRunItem(RunSchema):
    type: Literal["handoff_output_item"]
    agent_name: str
    raw_item: Any
    source_agent: str
    target_agent: str


class ReasoningRunItem(RunSchema):
    type: Literal["reasoning_item"]
    agent_name: str
    raw_item: Any


class ToolApprovalRunItem(RunSchema):
    type: Literal["tool_approval_item"]
    agent_name: str
    raw_item: Any
    tool_name: str | None = None
    tool_namespace: str | None = None
    item_key: str


class ToolSearchCallRunItem(RunSchema):
    type: Literal["tool_search_call_item"]
    agent_name: str
    raw_item: Any


class ToolSearchOutputRunItem(RunSchema):
    type: Literal["tool_search_output_item"]
    agent_name: str
    raw_item: Any


class McpListToolsRunItem(RunSchema):
    type: Literal["mcp_list_tools_item"]
    agent_name: str
    raw_item: Any


class McpApprovalRequestRunItem(RunSchema):
    type: Literal["mcp_approval_request_item"]
    agent_name: str
    raw_item: Any


class McpApprovalResponseRunItem(RunSchema):
    type: Literal["mcp_approval_response_item"]
    agent_name: str
    raw_item: Any


RunItemResponse = Annotated[
    MessageRunItem
    | ToolCallRunItem
    | ToolOutputRunItem
    | HandoffCallRunItem
    | HandoffOutputRunItem
    | ReasoningRunItem
    | ToolApprovalRunItem
    | ToolSearchCallRunItem
    | ToolSearchOutputRunItem
    | McpListToolsRunItem
    | McpApprovalRequestRunItem
    | McpApprovalResponseRunItem,
    Field(discriminator="type"),
]


class RunEventResponse(RunSchema):
    sequence: int
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


class RunInterruptionResponse(RunSchema):
    id: str
    item_key: str
    tool_name: str | None
    status: str
    item: ToolApprovalRunItem | dict[str, Any]
    response: dict[str, Any] | None
    created_at: datetime
    resolved_at: datetime | None


class RunResponse(RunSchema):
    id: str
    agent_revision_id: str | None
    conversation_id: str | None
    agent_name: str
    status: Literal["pending", "running", "paused", "completed", "failed", "cancelled"]
    input: Any
    final_output: Any | None
    last_agent_name: str | None
    usage: dict[str, Any]
    error: str | None
    cancel_requested: bool
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    items: list[RunItemResponse]
    events: list[RunEventResponse]
    interruptions: list[RunInterruptionResponse]


class RunCreateRequest(RunSchema):
    agent_revision_id: str | None = None
    blueprint: AgentBlueprint | None = None
    input: str | list[dict[str, Any]]
    conversation_id: str | None = None
    reasoning_effort: ReasoningEffort | None = None

    @model_validator(mode="after")
    def require_one_agent_source(self) -> "RunCreateRequest":
        if bool(self.agent_revision_id) == bool(self.blueprint):
            raise ValueError("Provide exactly one of agent_revision_id or blueprint.")
        return self


class InterruptionResolutionRequest(RunSchema):
    approved: bool
    rejection_message: str | None = Field(default=None, max_length=2_000)


def run_response(record) -> RunResponse:
    return RunResponse(
        id=record.id,
        agent_revision_id=record.agent_revision_id,
        conversation_id=record.conversation_id,
        agent_name=record.agent_name,
        status=record.status,
        input=record.input_json,
        final_output=record.final_output_json,
        last_agent_name=record.last_agent_name,
        usage=record.usage_json or {},
        error=record.error,
        cancel_requested=record.cancel_requested,
        created_at=record.created_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
        items=[item.item_json for item in record.items],
        events=[
            RunEventResponse(
                sequence=event.sequence,
                event_type=event.event_type,
                payload=event.payload_json,
                created_at=event.created_at,
            )
            for event in record.events
        ],
        interruptions=[
            RunInterruptionResponse(
                id=interruption.id,
                item_key=interruption.item_key,
                tool_name=interruption.tool_name,
                status=interruption.status,
                item=interruption.item_json,
                response=interruption.response_json,
                created_at=interruption.created_at,
                resolved_at=interruption.resolved_at,
            )
            for interruption in record.interruptions
        ],
    )
