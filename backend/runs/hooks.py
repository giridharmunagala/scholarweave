from __future__ import annotations

import json
import uuid
from typing import Any, Literal

from agents import Agent, RunHooks

from backend.agents.context import ScholarWeaveContext, unwrap_scholar_context
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


class ScholarWeaveRunHooks(RunHooks[ScholarWeaveContext]):
    async def on_agent_start(self, context, agent: Agent[ScholarWeaveContext]) -> None:
        scholar_context = unwrap_scholar_context(context)
        await start_agent_invocation(scholar_context, agent.name)

    async def on_agent_end(
        self,
        context,
        agent: Agent[ScholarWeaveContext],
        output: Any,
    ) -> None:
        scholar_context = unwrap_scholar_context(context)
        serialized_output = to_jsonable(output)
        await finish_agent_invocation(
            scholar_context,
            agent.name,
            "completed",
            output=serialized_output,
        )

    async def on_llm_start(
        self,
        context,
        agent: Agent[ScholarWeaveContext],
        system_prompt: str | None,
        input_items: list[Any],
    ) -> None:
        serialized_input = json.dumps(
            to_jsonable(input_items),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        await unwrap_scholar_context(context).emit(
            "model.started",
            {
                "agent_name": agent.name,
                "input_item_count": len(input_items),
                "has_system_prompt": bool(system_prompt),
                "input_character_count": len(system_prompt or "") + len(serialized_input),
            },
        )

    async def on_llm_end(
        self,
        context,
        agent: Agent[ScholarWeaveContext],
        response,
    ) -> None:
        await unwrap_scholar_context(context).emit(
            "model.completed",
            {
                "agent_name": agent.name,
                "usage": to_jsonable(response.usage),
            },
        )

    async def on_tool_start(self, context, agent, tool) -> None:
        scholar_context = unwrap_scholar_context(context)
        await scholar_context.emit(
            "tool.started",
            {
                "agent_name": agent.name,
                "invocation_id": active_agent_invocation_id(
                    scholar_context,
                    agent.name,
                ),
                "tool_name": getattr(tool, "name", type(tool).__name__),
                "tool_call_id": getattr(context, "tool_call_id", None),
            },
        )

    async def on_tool_end(self, context, agent, tool, result: object) -> None:
        from backend.tools.failures import consume_tool_failure

        tool_name = getattr(tool, "name", type(tool).__name__)
        failure = consume_tool_failure(context, tool_name)
        scholar_context = unwrap_scholar_context(context)
        invocation_id = active_agent_invocation_id(scholar_context, agent.name)
        if failure is not None:
            await scholar_context.emit(
                "tool.failed",
                {
                    "agent_name": agent.name,
                    "invocation_id": invocation_id,
                    "tool_name": tool_name,
                    "tool_call_id": getattr(context, "tool_call_id", None),
                    **failure,
                },
            )
            return
        bound_result = getattr(scholar_context.tool_runtime, "bound_tool_result", None)
        event_result = (
            await bound_result(tool_name, result, scholar_context)
            if callable(bound_result)
            else result
        )
        await scholar_context.emit(
            "tool.completed",
            {
                "agent_name": agent.name,
                "invocation_id": invocation_id,
                "tool_name": tool_name,
                "tool_call_id": getattr(context, "tool_call_id", None),
                "result": to_jsonable(event_result),
            },
        )

    async def on_handoff(self, context, from_agent, to_agent) -> None:
        await unwrap_scholar_context(context).emit(
            "handoff.completed",
            {
                "from_agent": from_agent.name,
                "to_agent": to_agent.name,
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
        await finish_all_agent_invocations(
            context,
            "superseded",
            reason=reason,
        )
