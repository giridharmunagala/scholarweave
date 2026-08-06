from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


class DocumentProcessingError(RuntimeError):
    pass


ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]


async def report_progress(
    progress: ProgressCallback | None,
    payload: dict[str, Any],
) -> None:
    if progress is not None:
        await progress(payload)
