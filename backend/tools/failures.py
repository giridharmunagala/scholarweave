from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from agents.tool import with_function_tool_failure_error_handler
from agents.tool_context import ToolContext

from backend.runtime.context import ScholarWeaveContext, unwrap_scholar_context
from backend.runtime.lifecycle import finish_agent_invocation

_FAILURES_KEY = "_recoverable_tool_failures"


def recoverable_tool_invoker(
    tool_name: str,
    invoke: Callable[[ToolContext[ScholarWeaveContext], str], Awaitable[Any]],
) -> Callable[[ToolContext[ScholarWeaveContext], str], Awaitable[Any]]:
    async def invoke_and_record(
        context: ToolContext[ScholarWeaveContext],
        raw_arguments: str,
    ) -> Any:
        try:
            return await invoke(context, raw_arguments)
        except Exception as exc:
            _record_failure(context, tool_name, exc)
            raise RuntimeError(
                f"{type(exc).__name__}: {exc}. The tool did not complete its action. "
                "Correct the request or try a different tool or source, then continue."
            ) from exc

    return with_function_tool_failure_error_handler(
        invoke_and_record,
        lambda _tool, _error, _input: None,
    )


def nested_agent_failure_handler(tool_name: str, agent_name: str):
    async def handle(context: Any, error: Exception) -> str:
        _record_failure(context, tool_name, error)
        scholar_context = unwrap_scholar_context(context)
        await finish_agent_invocation(
            scholar_context,
            agent_name,
            "failed",
            error=f"{type(error).__name__}: {error}",
        )
        plan = scholar_context.metadata.get("extended_work_plan")
        notes = scholar_context.metadata.get("extended_work_notes")
        priorities = scholar_context.metadata.get("extended_work_priorities")
        budget = scholar_context.metadata.get("extended_work_budget")
        budget_usage = scholar_context.metadata.get("extended_work_safety_usage")
        partial_state = {
            "plan": [
                {
                    key: task.get(key)
                    for key in (
                        "id",
                        "title",
                        "status",
                        "summary",
                        "instructions",
                        "expected_output",
                        "effort",
                        "source_target",
                        "rationale",
                    )
                }
                for task in plan
                if isinstance(task, dict)
            ]
            if isinstance(plan, list)
            else [],
            "saved_notes": [
                {
                    "note_id": note.get("id"),
                    "task_id": note.get("task_id"),
                    "title": note.get("title"),
                    "summary": note.get("summary"),
                    "source_count": len(note.get("sources", []))
                    if isinstance(note.get("sources"), list)
                    else 0,
                }
                for note in notes.values()
                if isinstance(note, dict)
            ]
            if isinstance(notes, dict)
            else [],
            "priority_decisions": [
                {
                    key: value
                    for key, value in decision.items()
                    if not key.startswith("_")
                }
                for decision in priorities[-20:]
                if isinstance(decision, dict)
            ]
            if isinstance(priorities, list)
            else [],
            "budget": budget if isinstance(budget, dict) else {},
            "safety_usage": budget_usage if isinstance(budget_usage, dict) else {},
        }
        return (
            f"The sub-agent stopped before completion: {type(error).__name__}: {error}. "
            f"Partial saved state: {partial_state}. Summarize the usable progress, read any saved "
            "notes that are relevant, and delegate only the unfinished scope to a fresh sub-agent."
        )

    return handle


def consume_tool_failure(
    context: Any,
    tool_name: str,
) -> dict[str, str] | None:
    try:
        scholar_context = unwrap_scholar_context(context)
    except TypeError:
        return None
    failures = scholar_context.metadata.get(_FAILURES_KEY)
    if not isinstance(failures, dict):
        return None
    failure = failures.pop(_tool_call_key(context, tool_name), None)
    if not failures:
        scholar_context.metadata.pop(_FAILURES_KEY, None)
    return failure if isinstance(failure, dict) else None


def _record_failure(
    context: Any,
    tool_name: str,
    error: Exception,
) -> None:
    scholar_context = unwrap_scholar_context(context)
    failures = scholar_context.metadata.setdefault(_FAILURES_KEY, {})
    if not isinstance(failures, dict):
        failures = {}
        scholar_context.metadata[_FAILURES_KEY] = failures
    failures[_tool_call_key(context, tool_name)] = {
        "error_type": type(error).__name__,
        "error": str(error) or type(error).__name__,
    }


def _tool_call_key(context: Any, tool_name: str) -> str:
    tool_call = getattr(context, "tool_call", None)
    call_id = getattr(tool_call, "call_id", None)
    return str(call_id or tool_name)
