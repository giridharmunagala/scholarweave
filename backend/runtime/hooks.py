from __future__ import annotations

import json
from typing import Any

from agents import Agent, RunHooks

from backend.runtime.context import ScholarWeaveContext
from backend.runtime.serialization import to_jsonable
from backend.tools.failures import consume_tool_failure


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
        serialized_input = json.dumps(
            to_jsonable(input_items),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        await context.context.emit(
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
        tool_name = getattr(tool, "name", type(tool).__name__)
        failure = consume_tool_failure(context, tool_name)
        if failure is not None:
            await context.context.emit(
                "tool.failed",
                {
                    "agent_name": agent.name,
                    "tool_name": tool_name,
                    **failure,
                },
            )
            return
        await context.context.emit(
            "tool.completed",
            {
                "agent_name": agent.name,
                "tool_name": tool_name,
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
