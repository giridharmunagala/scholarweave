from __future__ import annotations

import uuid
from typing import Any, Literal

from backend.runtime.context import ScholarWeaveContext

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
