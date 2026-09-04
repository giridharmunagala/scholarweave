from __future__ import annotations

import json
import uuid
from typing import Any, Literal

from backend.agents.context import ScholarWeaveContext
from backend.utils import to_jsonable

AgentTerminalStatus = Literal["completed", "failed", "superseded"]
_ACTIVE_INVOCATIONS_KEY = "_active_agent_invocations"


def active_agent_invocation_id(
    context: ScholarWeaveContext,
    agent_name: str,
) -> str | None:
    active = context.metadata.get(_ACTIVE_INVOCATIONS_KEY)
    if not isinstance(active, dict):
        return None
    invocations = active.get(agent_name)
    if not isinstance(invocations, list) or not invocations:
        return None
    invocation_id = invocations[-1]
    return invocation_id if isinstance(invocation_id, str) else None


async def start_agent_invocation(
    context: ScholarWeaveContext,
    agent_name: str,
    *,
    reason: str | None = None,
) -> str:
    active = context.metadata.setdefault(_ACTIVE_INVOCATIONS_KEY, {})
    if not isinstance(active, dict):
        active = {}
        context.metadata[_ACTIVE_INVOCATIONS_KEY] = active
    invocations = active.setdefault(agent_name, [])
    if not isinstance(invocations, list):
        invocations = []
        active[agent_name] = invocations
    invocation_id = str(uuid.uuid4())
    invocations.append(invocation_id)
    payload: dict[str, Any] = {
        "agent_name": agent_name,
        "invocation_id": invocation_id,
    }
    if reason:
        payload["reason"] = reason
    await context.emit("agent.started", payload)
    return invocation_id


async def finish_agent_invocation(
    context: ScholarWeaveContext,
    agent_name: str,
    status: AgentTerminalStatus,
    *,
    output: Any = None,
    error: str | None = None,
    reason: str | None = None,
) -> str | None:
    active = context.metadata.get(_ACTIVE_INVOCATIONS_KEY)
    invocations = active.get(agent_name) if isinstance(active, dict) else None
    invocation_id = invocations.pop() if isinstance(invocations, list) and invocations else None
    if isinstance(active, dict) and isinstance(invocations, list) and not invocations:
        active.pop(agent_name, None)
    if isinstance(active, dict) and not active:
        context.metadata.pop(_ACTIVE_INVOCATIONS_KEY, None)
    if not isinstance(invocation_id, str):
        invocation_id = str(uuid.uuid4())

    payload: dict[str, Any] = {
        "agent_name": agent_name,
        "invocation_id": invocation_id,
    }
    if output is not None:
        payload["output"] = output
    if error:
        payload["error"] = error
    if reason:
        payload["reason"] = reason
    await context.emit(f"agent.{status}", payload)
    return invocation_id


async def finish_all_agent_invocations(
    context: ScholarWeaveContext,
    status: Literal["failed", "superseded"],
    *,
    error: str | None = None,
    reason: str | None = None,
) -> None:
    active = context.metadata.get(_ACTIVE_INVOCATIONS_KEY)
    if not isinstance(active, dict):
        return
    pending = [
        (agent_name, invocation_id)
        for agent_name, invocations in active.items()
        if isinstance(agent_name, str) and isinstance(invocations, list)
        for invocation_id in reversed(invocations)
        if isinstance(invocation_id, str)
    ]
    context.metadata.pop(_ACTIVE_INVOCATIONS_KEY, None)
    for agent_name, invocation_id in pending:
        payload: dict[str, Any] = {
            "agent_name": agent_name,
            "invocation_id": invocation_id,
        }
        if error:
            payload["error"] = error
        if reason:
            payload["reason"] = reason
        await context.emit(f"agent.{status}", payload)


class ScholarWeaveRunHooks:
    """Translates harness lifecycle callbacks into ScholarWeave run events."""

    async def on_agent_start(self, context: ScholarWeaveContext, agent: Any) -> None:
        await start_agent_invocation(context, agent.name)

    async def on_agent_end(
        self,
        context: ScholarWeaveContext,
        agent: Any,
        output: Any,
    ) -> None:
        await finish_agent_invocation(
            context,
            agent.name,
            "completed",
            output=to_jsonable(output),
        )

    async def on_llm_start(
        self,
        context: ScholarWeaveContext,
        agent: Any,
        instructions: str | None,
        input_items: list[Any],
    ) -> None:
        serialized_input = json.dumps(
            to_jsonable(input_items),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        await context.emit(
            "model.started",
            {
                "agent_name": agent.name,
                "input_item_count": len(input_items),
                "has_system_prompt": bool(instructions),
                "input_character_count": len(instructions or "") + len(serialized_input),
            },
        )

    async def on_llm_end(
        self,
        context: ScholarWeaveContext,
        agent: Any,
        usage: dict[str, Any],
    ) -> None:
        await context.emit(
            "model.completed",
            {"agent_name": agent.name, "usage": to_jsonable(usage)},
        )

    async def on_tool_start(
        self,
        context: ScholarWeaveContext,
        agent: Any,
        tool_name: str,
        tool_call_id: str,
    ) -> None:
        await context.emit(
            "tool.started",
            {
                "agent_name": agent.name,
                "invocation_id": active_agent_invocation_id(context, agent.name),
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
            },
        )

    async def on_tool_end(
        self,
        context: ScholarWeaveContext,
        agent: Any,
        tool_name: str,
        tool_call_id: str,
        result: Any,
    ) -> None:
        from backend.tools.failures import consume_tool_failure

        invocation_id = active_agent_invocation_id(context, agent.name)
        failure = consume_tool_failure(context, tool_call_id or tool_name)
        if failure is not None:
            await context.emit(
                "tool.failed",
                {
                    "agent_name": agent.name,
                    "invocation_id": invocation_id,
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    **failure,
                },
            )
            return
        bound_result = getattr(context.tool_runtime, "bound_tool_result", None)
        event_result = (
            await bound_result(tool_name, result, context)
            if callable(bound_result)
            else result
        )
        await context.emit(
            "tool.completed",
            {
                "agent_name": agent.name,
                "invocation_id": invocation_id,
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "result": to_jsonable(event_result),
            },
        )

    async def fail_active(
        self,
        context: ScholarWeaveContext,
        error: BaseException,
    ) -> None:
        await finish_all_agent_invocations(
            context,
            "failed",
            error=f"{type(error).__name__}: {error}",
        )

    async def supersede_active(
        self,
        context: ScholarWeaveContext,
        reason: str,
    ) -> None:
        await finish_all_agent_invocations(context, "superseded", reason=reason)
