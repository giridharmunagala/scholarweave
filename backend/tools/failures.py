from __future__ import annotations

from collections.abc import Awaitable, Callable
import hashlib
import json
from typing import Any

import httpx
from openai import APIConnectionError, APITimeoutError

from backend.agents.context import ScholarWeaveContext
from backend.agents.harness import ToolInvocation
from backend.core.errors import NotFoundError, ValidationError
from backend.runs.repository import LeaseOwnershipError
from backend.tools.policy import ToolInputError, operation_policy

_FAILURES_KEY = "_recoverable_tool_failures"
_INFORMATION_FAILURE_COUNTS_KEY = "_consecutive_information_failure_counts"
_DISABLED_INFORMATION_TOOLS_KEY = "_disabled_information_tools"
MAX_CONSECUTIVE_INFORMATION_FAILURES = 3

ToolInvoker = Callable[[ToolInvocation, str], Awaitable[Any]]


def recoverable_tool_invoker(
    tool_name: str,
    invoke: ToolInvoker,
    *,
    catalog_id: str | None = None,
) -> ToolInvoker:
    """Record tool failures and re-raise them with model-facing recovery guidance.

    The harness converts the raised error into the tool result the model sees, so a
    failing tool never terminates the run on its own.
    """

    async def invoke_and_record(
        invocation: ToolInvocation,
        raw_arguments: str,
    ) -> Any:
        try:
            parsed = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError:
            parsed = {}
        arguments = parsed if isinstance(parsed, dict) else {}
        scope = _failure_scope(catalog_id, arguments) if catalog_id else None
        disabled = invocation.context.metadata.get(_DISABLED_INFORMATION_TOOLS_KEY)
        if scope and scope != catalog_id and isinstance(disabled, list) and scope in disabled:
            raise RuntimeError(
                "This source is paused after three consecutive failures. Use a different "
                "document, URL, or path; the tool remains available for other sources."
            )
        try:
            result = await invoke(invocation, raw_arguments)
        except LeaseOwnershipError:
            raise
        except Exception as exc:
            category, transient = classify_tool_error(exc)
            policy = operation_policy(catalog_id or "", arguments)
            retryable = policy.safe_retry and transient
            unknown_outcome = policy.mutating and not isinstance(
                exc, (ToolInputError, json.JSONDecodeError)
            )
            failure = _record_failure(
                invocation.context,
                invocation.tool_call_id or tool_name,
                exc,
                catalog_id=catalog_id,
                category=category,
                retryable=retryable,
                unknown_outcome=unknown_outcome,
                scope=scope,
            )
            if failure.get("failure_limit_reached"):
                guidance = (
                    "This source is paused after three consecutive failures. The tool remains "
                    "available for a different document, URL, or path."
                    if scope != catalog_id
                    else (
                        f"The {tool_name} tool is now disabled after three consecutive failures. "
                        "Continue with the remaining tools or a different source. Answer from the "
                        "available evidence only if no remaining tool can fill the gap, and state "
                        "what could not be verified."
                    )
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
            raise RuntimeError(json.dumps({
                "status": "error",
                "category": category,
                "retryable": retryable,
                "unknown_outcome": unknown_outcome,
                "message": str(exc),
                "next_action": guidance,
            }, ensure_ascii=False)) from exc
        record_tool_success(invocation.context, catalog_id, scope=scope)
        return result

    return invoke_and_record


def consume_tool_failure(
    context: ScholarWeaveContext,
    failure_key: str,
) -> dict[str, Any] | None:
    failures = context.metadata.get(_FAILURES_KEY)
    if not isinstance(failures, dict):
        return None
    failure = failures.pop(failure_key, None)
    if not failures:
        context.metadata.pop(_FAILURES_KEY, None)
    return failure if isinstance(failure, dict) else None


def _record_failure(
    context: ScholarWeaveContext,
    failure_key: str,
    error: Exception,
    *,
    catalog_id: str | None = None,
    category: str = "tool_error",
    retryable: bool = False,
    unknown_outcome: bool = False,
    scope: str | None = None,
) -> dict[str, Any]:
    failures = context.metadata.setdefault(_FAILURES_KEY, {})
    if not isinstance(failures, dict):
        failures = {}
        context.metadata[_FAILURES_KEY] = failures
    consecutive_information_failures = 0
    failure_limit_reached = False
    count_failure = category != "invalid_input"
    if catalog_id and is_failure_limited_tool(catalog_id) and count_failure:
        key = scope or catalog_id
        counts = context.metadata.setdefault(_INFORMATION_FAILURE_COUNTS_KEY, {})
        if not isinstance(counts, dict):
            counts = {}
            context.metadata[_INFORMATION_FAILURE_COUNTS_KEY] = counts
        current = counts.get(key, 0)
        consecutive_information_failures = (
            current + 1 if isinstance(current, int) and not isinstance(current, bool) else 1
        )
        counts[key] = consecutive_information_failures
        failure_limit_reached = (
            consecutive_information_failures >= MAX_CONSECUTIVE_INFORMATION_FAILURES
        )
        if failure_limit_reached:
            disabled = context.metadata.setdefault(_DISABLED_INFORMATION_TOOLS_KEY, [])
            if not isinstance(disabled, list):
                disabled = []
                context.metadata[_DISABLED_INFORMATION_TOOLS_KEY] = disabled
            if key not in disabled:
                disabled.append(key)
    failure = {
        "error_type": type(error).__name__,
        "error": str(error) or type(error).__name__,
        "category": category,
        "retryable": retryable,
        "unknown_outcome": unknown_outcome,
        "display_message": _display_message(
            category,
            unknown_outcome=unknown_outcome,
            failure_limit_reached=failure_limit_reached,
        ),
    }
    if scope and scope != catalog_id:
        failure["failure_scope"] = scope
        if failure_limit_reached:
            failure["display_message"] = (
                "This source was paused after three consecutive failures. "
                "The tool is still available for other sources."
            )
    if catalog_id and is_failure_limited_tool(catalog_id):
        failure.update(
            {
                "consecutive_information_failures": consecutive_information_failures,
                "failure_limit_reached": failure_limit_reached,
            }
        )
    failures[failure_key] = failure
    return failure


def _display_message(
    category: str,
    *,
    unknown_outcome: bool,
    failure_limit_reached: bool,
) -> str:
    if failure_limit_reached:
        return (
            "This tool was paused after three consecutive failures. The agent will use "
            "another available source or explain what could not be verified."
        )
    if unknown_outcome:
        return (
            "The tool stopped before it could confirm whether the change was saved. "
            "The agent will inspect the result before trying again."
        )
    return {
        "timeout": (
            "The tool took too long to respond. The agent can retry or use another source."
        ),
        "rate_limited": (
            "The service is temporarily limiting requests. The agent can retry shortly."
        ),
        "upstream_unavailable": (
            "The external service is temporarily unavailable. The agent can retry or use "
            "another source."
        ),
        "transport": (
            "The tool could not reach its service. Check the connection or try again."
        ),
        "invalid_input": (
            "The tool could not use this request. The agent will correct it before trying again."
        ),
        "not_found": "This source is unavailable. Select another source or prepare the missing paper.",
        "access_denied": "The source could not be accessed. Check its location or local permissions.",
    }.get(
        category,
        "The tool could not complete this request. The agent will try another available approach.",
    )


def classify_tool_error(error: Exception) -> tuple[str, bool]:
    status_code = getattr(error, "status_code", None)
    if status_code is None and isinstance(error, httpx.HTTPStatusError):
        status_code = error.response.status_code
    message = str(error).casefold()
    if isinstance(error, (TimeoutError, httpx.TimeoutException, APITimeoutError)):
        return "timeout", True
    if isinstance(error, (FileNotFoundError, NotFoundError)) or status_code == 404:
        return "not_found", False
    if isinstance(error, PermissionError) or status_code in {401, 403}:
        return "access_denied", False
    if status_code == 429 or "rate limit" in message:
        return "rate_limited", True
    if (isinstance(status_code, int) and status_code >= 500) or any(
        marker in message for marker in ("http 500", "http 502", "http 503", "http 504")
    ):
        return "upstream_unavailable", True
    if isinstance(error, (ConnectionError, httpx.TransportError, APIConnectionError)):
        return "transport", True
    if isinstance(error, (ValueError, TypeError, KeyError, ValidationError)) or status_code in {400, 422}:
        return "invalid_input", False
    return "tool_error", False


def _failure_scope(catalog_id: str, arguments: dict[str, Any]) -> str:
    target = {
        key: arguments[key]
        for key in ("document_id", "url", "path", "source_id", "result_ref")
        if isinstance(arguments.get(key), str) and arguments[key]
    }
    if not target:
        return catalog_id
    digest = hashlib.sha256(json.dumps(target, sort_keys=True).encode()).hexdigest()[:24]
    return f"{catalog_id}::{digest}"


def is_information_tool(catalog_id: str) -> bool:
    return catalog_id in {
        "research.sources.search",
        "research.library.search",
        "research.paper.read",
        "research.summary.read",
        "research.web.read",
        "research.notes.search",
        "research.notes.read",
        "tool.results.read",
        "work.plan.read",
    }


def is_failure_limited_tool(catalog_id: str) -> bool:
    return is_information_tool(catalog_id) or catalog_id == "research.sources.acquire"


def tool_enabled_after_failures(
    catalog_id: str,
) -> Callable[[ScholarWeaveContext], bool]:
    def enabled(context: ScholarWeaveContext) -> bool:
        disabled = context.metadata.get(_DISABLED_INFORMATION_TOOLS_KEY)
        return not isinstance(disabled, list) or catalog_id not in disabled

    return enabled


def record_tool_success(
    context: ScholarWeaveContext,
    catalog_id: str | None,
    *,
    scope: str | None = None,
) -> None:
    if not catalog_id or not is_failure_limited_tool(catalog_id):
        return
    key = scope or catalog_id
    counts = context.metadata.get(_INFORMATION_FAILURE_COUNTS_KEY)
    if isinstance(counts, dict):
        counts.pop(key, None)
        if not counts:
            context.metadata.pop(_INFORMATION_FAILURE_COUNTS_KEY, None)
    disabled = context.metadata.get(_DISABLED_INFORMATION_TOOLS_KEY)
    if isinstance(disabled, list) and key in disabled:
        disabled[:] = [item for item in disabled if item != key]
        if not disabled:
            context.metadata.pop(_DISABLED_INFORMATION_TOOLS_KEY, None)


def restore_tool_failure_state_from_attempts(
    metadata: dict[str, Any],
    attempts: list[Any],
) -> None:
    final_attempts: dict[tuple[str, str], tuple[int, str, str | None]] = {}
    for index, attempt in enumerate(attempts):
        catalog_id = getattr(attempt, "catalog_id", None)
        if not isinstance(catalog_id, str) or not is_failure_limited_tool(catalog_id):
            continue
        call_id = getattr(attempt, "tool_call_id", None)
        call_key = str(call_id) if call_id else f"attempt-{index}"
        arguments = getattr(attempt, "arguments_json", {})
        scope = _failure_scope(catalog_id, arguments if isinstance(arguments, dict) else {})
        final_attempts[(scope, call_key)] = (
            index,
            str(getattr(attempt, "status", "")),
            getattr(attempt, "failure_category", None),
        )

    counts: dict[str, int] = {}
    disabled: list[str] = []
    for (catalog_id, _call_id), (_index, status, category) in sorted(
        final_attempts.items(),
        key=lambda item: item[1][0],
    ):
        if status == "completed":
            counts.pop(catalog_id, None)
            if catalog_id in disabled:
                disabled.remove(catalog_id)
            continue
        if status not in {"failed", "unknown_outcome"} or category == "invalid_input":
            continue
        counts[catalog_id] = counts.get(catalog_id, 0) + 1
        if (
            counts[catalog_id] >= MAX_CONSECUTIVE_INFORMATION_FAILURES
            and catalog_id not in disabled
        ):
            disabled.append(catalog_id)

    metadata.pop(_INFORMATION_FAILURE_COUNTS_KEY, None)
    metadata.pop(_DISABLED_INFORMATION_TOOLS_KEY, None)
    restore_tool_failure_state(metadata, {"counts": counts, "disabled": disabled})


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
            catalog_id for catalog_id in disabled if isinstance(catalog_id, str)
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
            catalog_id for catalog_id in disabled if isinstance(catalog_id, str)
        ]
        if restored_disabled:
            metadata[_DISABLED_INFORMATION_TOOLS_KEY] = restored_disabled
