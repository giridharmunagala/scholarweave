from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from agents.tool import with_function_tool_failure_error_handler
from agents.tool_context import ToolContext

from backend.runtime.context import ScholarWeaveContext

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


def consume_tool_failure(
    context: Any,
    tool_name: str,
) -> dict[str, str] | None:
    scholar_context = getattr(context, "context", None)
    if not isinstance(scholar_context, ScholarWeaveContext):
        return None
    failures = scholar_context.metadata.get(_FAILURES_KEY)
    if not isinstance(failures, dict):
        return None
    failure = failures.pop(_tool_call_key(context, tool_name), None)
    if not failures:
        scholar_context.metadata.pop(_FAILURES_KEY, None)
    return failure if isinstance(failure, dict) else None


def _record_failure(
    context: ToolContext[ScholarWeaveContext],
    tool_name: str,
    error: Exception,
) -> None:
    failures = context.context.metadata.setdefault(_FAILURES_KEY, {})
    if not isinstance(failures, dict):
        failures = {}
        context.context.metadata[_FAILURES_KEY] = failures
    failures[_tool_call_key(context, tool_name)] = {
        "error_type": type(error).__name__,
        "error": str(error) or type(error).__name__,
    }


def _tool_call_key(context: Any, tool_name: str) -> str:
    tool_call = getattr(context, "tool_call", None)
    call_id = getattr(tool_call, "call_id", None)
    return str(call_id or tool_name)
