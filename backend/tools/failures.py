from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from agents.tool import with_function_tool_failure_error_handler
from agents.tool_context import ToolContext

from backend.runtime.context import ScholarWeaveContext
from backend.runtime.context import unwrap_scholar_context
from backend.runtime.lifecycle import finish_agent_invocation

_FAILURES_KEY = "_recoverable_tool_failures"
_INFORMATION_FAILURE_COUNTS_KEY = "_consecutive_information_failure_counts"
_DISABLED_INFORMATION_TOOLS_KEY = "_disabled_information_tools"
MAX_CONSECUTIVE_INFORMATION_FAILURES = 3


def recoverable_tool_invoker(
    tool_name: str,
    invoke: Callable[[ToolContext[ScholarWeaveContext], str], Awaitable[Any]],
    *,
    catalog_id: str | None = None,
) -> Callable[[ToolContext[ScholarWeaveContext], str], Awaitable[Any]]:
    async def invoke_and_record(
        context: ToolContext[ScholarWeaveContext],
        raw_arguments: str,
    ) -> Any:
        try:
            result = await invoke(context, raw_arguments)
        except Exception as exc:
            category, retryable = _failure_policy(exc)
            unknown_outcome = bool(catalog_id and not _safe_read(catalog_id))
            failure = _record_failure(
                context,
                tool_name,
                exc,
                catalog_id=catalog_id,
                category=category,
                retryable=retryable,
                unknown_outcome=unknown_outcome,
            )
            if failure.get("failure_limit_reached"):
                guidance = (
                    f"The {tool_name} tool is now disabled after three consecutive failures. "
                    "Continue with the remaining tools or a different source. Answer from the "
                    "available evidence only if no remaining tool can fill the gap, and state "
                    "what could not be verified."
                )
            elif unknown_outcome:
                guidance = (
                    "Do not retry this write until its outcome is inspected or reconciled; "
                    "try a different tool or source without repeating the write."
                )
            else:
                guidance = (
                    f"The {tool_name} tool failed. Do not repeat the identical call; correct "
                    "the request or try a different tool or source that is still available, "
                    "then continue."
                )
            raise RuntimeError(
                f"{category}: {type(exc).__name__}: {exc}. {guidance}"
            ) from exc
        record_tool_success(context.context, catalog_id)
        return result

    return with_function_tool_failure_error_handler(
        invoke_and_record,
        lambda _tool, _error, _input: None,
    )


def consume_tool_failure(
    context: Any,
    tool_name: str,
) -> dict[str, Any] | None:
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
    *,
    catalog_id: str | None = None,
    category: str = "tool_error",
    retryable: bool = False,
    unknown_outcome: bool = False,
) -> dict[str, Any]:
    failures = context.context.metadata.setdefault(_FAILURES_KEY, {})
    if not isinstance(failures, dict):
        failures = {}
        context.context.metadata[_FAILURES_KEY] = failures
    consecutive_information_failures = 0
    failure_limit_reached = False
    if catalog_id and is_information_tool(catalog_id):
        counts = context.context.metadata.setdefault(
            _INFORMATION_FAILURE_COUNTS_KEY,
            {},
        )
        if not isinstance(counts, dict):
            counts = {}
            context.context.metadata[_INFORMATION_FAILURE_COUNTS_KEY] = counts
        current = counts.get(catalog_id, 0)
        consecutive_information_failures = (
            current + 1 if isinstance(current, int) and not isinstance(current, bool) else 1
        )
        counts[catalog_id] = consecutive_information_failures
        failure_limit_reached = (
            consecutive_information_failures
            >= MAX_CONSECUTIVE_INFORMATION_FAILURES
        )
        if failure_limit_reached:
            disabled = context.context.metadata.setdefault(
                _DISABLED_INFORMATION_TOOLS_KEY,
                [],
            )
            if not isinstance(disabled, list):
                disabled = []
                context.context.metadata[_DISABLED_INFORMATION_TOOLS_KEY] = disabled
            if catalog_id not in disabled:
                disabled.append(catalog_id)
    failure = {
        "error_type": type(error).__name__,
        "error": str(error) or type(error).__name__,
        "category": category,
        "retryable": retryable,
        "unknown_outcome": unknown_outcome,
    }
    if catalog_id and is_information_tool(catalog_id):
        failure.update(
            {
                "consecutive_information_failures": consecutive_information_failures,
                "failure_limit_reached": failure_limit_reached,
            }
        )
    failures[_tool_call_key(context, tool_name)] = failure
    return failure


def nested_agent_failure_handler(
    tool_name: str,
    agent_name: str,
) -> Callable[[Any, Exception], Awaitable[str]]:
    async def handle(context: Any, error: Exception) -> str:
        scholar_context = unwrap_scholar_context(context)
        _record_failure(context, tool_name, error)
        await finish_agent_invocation(
            scholar_context,
            agent_name,
            "failed",
            error=str(error) or type(error).__name__,
        )
        notes = scholar_context.metadata.get("extended_work_notes")
        summaries = (
            [
                str(note.get("summary"))
                for note in notes.values()
                if isinstance(note, dict) and note.get("summary")
            ]
            if isinstance(notes, dict)
            else []
        )
        checkpoint = (
            " Saved progress: " + " ".join(summaries)
            if summaries
            else ""
        )
        return (
            f"{agent_name} failed: {type(error).__name__}: {error}.{checkpoint} "
            "Keep completed work, then delegate only the unfinished scope to a fresh "
            "sub-agent or continue directly."
        )

    return handle


def _failure_policy(error: Exception) -> tuple[str, bool]:
    status_code = getattr(error, "status_code", None)
    message = str(error).casefold()
    if isinstance(error, TimeoutError):
        return "timeout", True
    if status_code == 429 or "rate limit" in message:
        return "rate_limited", True
    if (isinstance(status_code, int) and status_code >= 500) or any(
        marker in message for marker in ("http 500", "http 502", "http 503", "http 504")
    ):
        return "upstream_unavailable", True
    if isinstance(error, (ConnectionError, OSError)):
        return "transport", True
    if isinstance(error, (ValueError, TypeError, KeyError)):
        return "invalid_input", False
    return "tool_error", False


def _safe_read(catalog_id: str) -> bool:
    return catalog_id == "webpage.download" or any(
        token in catalog_id
        for token in (
            ".list",
            ".read",
            ".search",
            ".inspect",
            "retrieval.",
            "tools.search",
            "sdk.catalog",
            "agents.get",
            "agents.validate",
        )
    )


def is_information_tool(catalog_id: str) -> bool:
    return (
        catalog_id != "webpage.download" and _safe_read(catalog_id)
    ) or catalog_id in {
        "documents.download",
    }


def tool_enabled_after_failures(
    catalog_id: str,
) -> Callable[[Any, Any], bool]:
    def enabled(context: Any, _agent: Any) -> bool:
        scholar_context = unwrap_scholar_context(context)
        disabled = scholar_context.metadata.get(_DISABLED_INFORMATION_TOOLS_KEY)
        return not isinstance(disabled, list) or catalog_id not in disabled

    return enabled


def record_tool_success(
    context: ScholarWeaveContext,
    catalog_id: str | None,
) -> None:
    if not catalog_id or not is_information_tool(catalog_id):
        return
    counts = context.metadata.get(_INFORMATION_FAILURE_COUNTS_KEY)
    if isinstance(counts, dict):
        counts.pop(catalog_id, None)
        if not counts:
            context.metadata.pop(_INFORMATION_FAILURE_COUNTS_KEY, None)


def serialize_tool_failure_state(metadata: dict[str, Any]) -> dict[str, Any]:
    counts = metadata.get(_INFORMATION_FAILURE_COUNTS_KEY)
    disabled = metadata.get(_DISABLED_INFORMATION_TOOLS_KEY)
    return {
        "counts": {
            str(catalog_id): count
            for catalog_id, count in counts.items()
            if isinstance(catalog_id, str)
            and isinstance(count, int)
            and not isinstance(count, bool)
            and count > 0
        }
        if isinstance(counts, dict)
        else {},
        "disabled": [
            catalog_id
            for catalog_id in disabled
            if isinstance(catalog_id, str)
        ]
        if isinstance(disabled, list)
        else [],
    }


def restore_tool_failure_state(
    metadata: dict[str, Any],
    state: Any,
) -> None:
    if not isinstance(state, dict):
        return
    counts = state.get("counts")
    if isinstance(counts, dict):
        restored_counts = {
            str(catalog_id): count
            for catalog_id, count in counts.items()
            if isinstance(catalog_id, str)
            and isinstance(count, int)
            and not isinstance(count, bool)
            and count > 0
        }
        if restored_counts:
            metadata[_INFORMATION_FAILURE_COUNTS_KEY] = restored_counts
    disabled = state.get("disabled")
    if isinstance(disabled, list):
        restored_disabled = [
            catalog_id
            for catalog_id in disabled
            if isinstance(catalog_id, str)
        ]
        if restored_disabled:
            metadata[_DISABLED_INFORMATION_TOOLS_KEY] = restored_disabled


def _tool_call_key(context: Any, tool_name: str) -> str:
    tool_call = getattr(context, "tool_call", None)
    call_id = getattr(tool_call, "call_id", None)
    return str(call_id or tool_name)
