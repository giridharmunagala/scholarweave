from __future__ import annotations

from typing import Any

from agents import Agent, RunHooks

from backend.runtime.context import ScholarWeaveContext
from backend.runtime.serialization import to_jsonable


class ScholarWeaveRunHooks(RunHooks[ScholarWeaveContext]):
    async def on_agent_start(self, context, agent: Agent[ScholarWeaveContext]) -> None:
        await context.context.emit("agent.started", {"agent_name": agent.name})

    async def on_agent_end(
        self,
        context,
        agent: Agent[ScholarWeaveContext],
        output: Any,
    ) -> None:
        await context.context.emit(
            "agent.completed",
            {"agent_name": agent.name, "output": to_jsonable(output)},
        )

    async def on_llm_start(
        self,
        context,
        agent: Agent[ScholarWeaveContext],
        system_prompt: str | None,
        input_items: list[Any],
    ) -> None:
        await context.context.emit(
            "model.started",
            {
                "agent_name": agent.name,
                "input_item_count": len(input_items),
                "has_system_prompt": bool(system_prompt),
            },
        )

    async def on_llm_end(
        self,
        context,
        agent: Agent[ScholarWeaveContext],
        response,
    ) -> None:
        await context.context.emit(
            "model.completed",
            {
                "agent_name": agent.name,
                "usage": to_jsonable(response.usage),
            },
        )

    async def on_tool_start(self, context, agent, tool) -> None:
        await context.context.emit(
            "tool.started",
            {
                "agent_name": agent.name,
                "tool_name": getattr(tool, "name", type(tool).__name__),
            },
        )

    async def on_tool_end(self, context, agent, tool, result: object) -> None:
        await context.context.emit(
            "tool.completed",
            {
                "agent_name": agent.name,
                "tool_name": getattr(tool, "name", type(tool).__name__),
                "result": to_jsonable(result),
            },
        )

    async def on_handoff(self, context, from_agent, to_agent) -> None:
        await context.context.emit(
            "handoff.completed",
            {
                "from_agent": from_agent.name,
                "to_agent": to_agent.name,
            },
        )
