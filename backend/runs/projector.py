from __future__ import annotations

from typing import Any

from agents import (
    AgentUpdatedStreamEvent,
    CompactionItem,
    HandoffCallItem,
    HandoffOutputItem,
    ItemHelpers,
    MessageOutputItem,
    RawResponsesStreamEvent,
    ReasoningItem,
    RunItem,
    RunItemStreamEvent,
    ToolApprovalItem,
    ToolCallItem,
    ToolCallOutputItem,
)

from backend.runtime.serialization import to_jsonable


def project_run_item(item: RunItem) -> dict[str, Any]:
    projection: dict[str, Any] = {
        "type": item.type,
        "agent_name": item.agent.name,
        "raw_item": to_jsonable(item.raw_item),
    }
    if isinstance(item, MessageOutputItem):
        projection["content"] = ItemHelpers.text_message_output(item)
    elif isinstance(item, ToolCallItem):
        projection.update(
            {
                "title": item.title,
                "description": item.description,
                "tool_origin": to_jsonable(item.tool_origin),
            }
        )
    elif isinstance(item, ToolCallOutputItem):
        projection.update(
            {
                "output": to_jsonable(item.output),
                "custom_data": to_jsonable(item.custom_data),
                "tool_origin": to_jsonable(item.tool_origin),
            }
        )
    elif isinstance(item, HandoffOutputItem):
        projection.update(
            {
                "source_agent": item.source_agent.name,
                "target_agent": item.target_agent.name,
            }
        )
    elif isinstance(item, ToolApprovalItem):
        projection.update(
            {
                "tool_name": item.tool_name,
                "tool_namespace": item.tool_namespace,
                "item_key": run_item_key(item),
            }
        )
    elif isinstance(item, (HandoffCallItem, ReasoningItem, CompactionItem)):
        pass
    return projection


def project_stream_event(event: Any) -> tuple[str, dict[str, Any]] | None:
    if isinstance(event, AgentUpdatedStreamEvent):
        return "agent.updated", {"agent_name": event.new_agent.name}
    if isinstance(event, RunItemStreamEvent):
        return "run.item", {
            "name": event.name,
            "item": project_run_item(event.item),
        }
    if isinstance(event, RawResponsesStreamEvent):
        raw_type = getattr(event.data, "type", type(event.data).__name__)
        payload: dict[str, Any] = {"raw_type": raw_type}
        delta = getattr(event.data, "delta", None)
        if delta is not None:
            payload["delta"] = to_jsonable(delta)
        return "model.stream", payload
    return None


def run_item_key(item: ToolApprovalItem) -> str:
    raw = item.raw_item
    for field in ("id", "call_id", "approval_request_id"):
        value = getattr(raw, field, None)
        if value:
            return str(value)
        if isinstance(raw, dict) and raw.get(field):
            return str(raw[field])
    return f"{item.agent.name}:{item.tool_name or 'tool'}"
