from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx


class ToolInputError(ValueError):
    """A request rejected before a handler can perform side effects."""


@dataclass(frozen=True, slots=True)
class ToolOperationPolicy:
    safe_retry: bool
    mutating: bool
    timeout_seconds: float | None = None


_LOCAL_READ = ToolOperationPolicy(True, False, 30.0)
_NETWORK_READ = ToolOperationPolicy(True, False, 60.0)
_WRITE = ToolOperationPolicy(False, True)
_LOCAL_READS = {
    "research.library.search",
    "research.notes.search",
    "research.notes.read",
    "tool.results.read",
    "work.plan.read",
}


def operation_policy(
    catalog_id: str, arguments: dict[str, Any]
) -> ToolOperationPolicy:
    """Classify actual operations, not words in a model-visible tool name."""
    if catalog_id == "research.summary.run":
        # The child run owns its writes; holding the mutation lock here deadlocks its tools.
        return ToolOperationPolicy(False, False)
    if catalog_id in _LOCAL_READS:
        return _LOCAL_READ
    if catalog_id in {"research.sources.search", "research.web.read"}:
        return _NETWORK_READ
    action = arguments.get("action")
    if catalog_id == "research.paper.read":
        return _LOCAL_READ if action in {"inspect", "pages", "chunks", "search"} else _WRITE
    if catalog_id == "research.summary.read":
        # Batch reads advance durable coverage/pending-checkpoint state.
        return _LOCAL_READ if action == "inspect" else _WRITE
    if catalog_id == "research.summary.checkpoint":
        return _LOCAL_READ if action == "read" else _WRITE
    return _WRITE


def retry_delay(
    error: Exception,
    attempt: int,
    *,
    now: datetime | None = None,
) -> float:
    """Respect Retry-After; callers must decline delays beyond their deadline."""
    response = getattr(error, "response", None)
    headers = response.headers if isinstance(response, httpx.Response) else None
    value = headers.get("Retry-After") if headers is not None else None
    if value:
        try:
            seconds = float(value)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
            except (ValueError, TypeError, OverflowError):
                retry_at = None
            if retry_at is not None:
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max(0.0, (retry_at - (now or datetime.now(timezone.utc))).total_seconds())
        else:
            if 0 <= seconds < float("inf"):
                return seconds
    return random.uniform(0.25, min(8.0, 0.5 * 2 ** max(0, attempt - 1)))
