from __future__ import annotations

import json
from typing import Any

from agents import Agent, RunHooks

from backend.runtime.context import ScholarWeaveContext, unwrap_scholar_context
from backend.runtime.lifecycle import (
    active_agent_invocation_id,
    finish_agent_invocation,
    finish_all_agent_invocations,
    start_agent_invocation,
)
from backend.runtime.priorities import advance_priority_progress, record_priority_decision
from backend.runtime.serialization import to_jsonable
from backend.tools.failures import consume_tool_failure


class ScholarWeaveRunHooks(RunHooks[ScholarWeaveContext]):
    async def on_agent_start(self, context, agent: Agent[ScholarWeaveContext]) -> None:
        scholar_context = unwrap_scholar_context(context)
        if agent.name == "Focused Work Specialist":
            advance_priority_progress(scholar_context)
        await start_agent_invocation(scholar_context, agent.name)

    async def on_agent_end(
        self,
        context,
        agent: Agent[ScholarWeaveContext],
        output: Any,
    ) -> None:
        scholar_context = unwrap_scholar_context(context)
        serialized_output = to_jsonable(output)
        if agent.name == "Research Work Prioritizer":
            decision = record_priority_decision(serialized_output, scholar_context)
            if decision is not None:
                await scholar_context.emit("extended.priorities.updated", decision)
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
