from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


OcrBatchStatus = Literal[
    "pending", "running", "stopping", "completed", "completed_with_errors", "stopped", "interrupted",
]
OcrBatchItemStatus = Literal[
    "pending", "running", "completed", "completed_with_errors", "failed", "skipped", "stopped",
]


@dataclass(slots=True)
class OcrBatchItem:
    document_id: str
    title: str
    status: OcrBatchItemStatus = "pending"
    completed_pages: int = 0
    total_pages: int | None = None
    error: str | None = None
    phase_label: str | None = None


@dataclass(slots=True)
class OcrBatch:
    id: str
    status: OcrBatchStatus
    created_at: str
    updated_at: str
    finished_at: str | None
    parallel_requests: int
    items: list[OcrBatchItem]
