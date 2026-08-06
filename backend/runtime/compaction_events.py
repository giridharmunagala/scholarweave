from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

CompactionEventHandler = Callable[[str, dict[str, Any]], Awaitable[None]]

_current_handler: ContextVar[CompactionEventHandler | None] = ContextVar(
    "scholarweave_compaction_event_handler",
    default=None,
)


@asynccontextmanager
async def observe_compaction(
    handler: CompactionEventHandler,
) -> AsyncIterator[None]:
    token = _current_handler.set(handler)
    try:
        yield
    finally:
        _current_handler.reset(token)


async def emit_compaction_event(
    event_type: str,
    payload: dict[str, Any],
) -> None:
    handler = _current_handler.get()
    if handler is not None:
        await handler(event_type, payload)
