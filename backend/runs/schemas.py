from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


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


class ReasoningRunItem(RunSchema):
    type: Literal["reasoning_item"]
    agent_name: str
    raw_item: Any


RunItemResponse = Annotated[
    MessageRunItem | ToolCallRunItem | ToolOutputRunItem | ReasoningRunItem,
    Field(discriminator="type"),
]


class RunEventResponse(RunSchema):
    sequence: int
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


class RunEpochResponse(RunSchema):
    id: str
    epoch_index: int
    status: str
    terminal_reason: str | None
    usage: dict[str, Any]
    error: str | None
    started_at: datetime
    finished_at: datetime | None


class ToolAttemptResponse(RunSchema):
    id: str
    epoch_id: str | None
    tool_call_id: str
    catalog_id: str
    attempt: int
    status: str
    failure_category: str | None
    retryable: bool
    result_ref: str | None
    error: str | None
    started_at: datetime
    finished_at: datetime | None


class RunResponse(RunSchema):
    id: str
    conversation_id: str | None
    agent_name: str
    status: Literal["pending", "running", "completed", "failed", "cancelled"]
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
    epochs: list[RunEpochResponse]
    tool_attempts: list[ToolAttemptResponse]
    goal_state: dict[str, Any] | None


class PromptSnapshotResponse(RunSchema):
    run_id: str
    prompt_revision: str | None
    agents: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    activated_skills: list[dict[str, Any]]


class StopAndAnswerResponse(RunSchema):
    stopped_run: RunResponse
    answer_run: RunResponse


class SteeringMessageRequest(RunSchema):
    content: str = Field(min_length=1, max_length=100_000)


class SteeringMessageResponse(RunSchema):
    id: str
    content: str
    status: Literal["queued"]


def run_response(record) -> RunResponse:
    return RunResponse(
        id=record.id,
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
        epochs=[
            RunEpochResponse(
                id=epoch.id,
                epoch_index=epoch.epoch_index,
                status=epoch.status,
                terminal_reason=epoch.terminal_reason,
                usage=epoch.usage_json or {},
                error=epoch.error,
                started_at=epoch.started_at,
                finished_at=epoch.finished_at,
            )
            for epoch in record.epochs
        ],
        tool_attempts=[
            ToolAttemptResponse(
                id=attempt.id,
                epoch_id=attempt.epoch_id,
                tool_call_id=attempt.tool_call_id,
                catalog_id=attempt.catalog_id,
                attempt=attempt.attempt,
                status=attempt.status,
                failure_category=attempt.failure_category,
                retryable=attempt.retryable,
                result_ref=attempt.result_ref,
                error=attempt.error,
                started_at=attempt.started_at,
                finished_at=attempt.finished_at,
            )
            for attempt in record.tool_attempts
        ],
        goal_state=(
            {
                "version": record.goal_state.version,
                "status": record.goal_state.status,
                **dict(record.goal_state.state_json),
            }
            if record.goal_state is not None
            else None
        ),
    )
