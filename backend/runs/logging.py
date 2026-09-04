"""Durable operational snapshots for completed agent runs."""

from __future__ import annotations

import json
import os
import threading
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class RunDetailLogger:
    """Keeps one bounded, replaceable JSON snapshot per run outside the UI."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._lock = threading.Lock()

    def write(self, record: Any) -> Path:
        payload = _run_details(record)
        path = self.directory / f"{record.id}.json"
        temporary = path.with_suffix(".json.tmp")
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._lock:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=True, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        return path


def _run_details(record: Any) -> dict[str, Any]:
    events = list(record.events)
    items = list(record.items)
    started_at = record.started_at
    finished_at = record.finished_at
    duration_seconds = (
        max(0.0, (finished_at - started_at).total_seconds())
        if started_at is not None and finished_at is not None
        else None
    )
    return {
        "schema_version": 1,
        "logged_at": datetime.now(UTC).isoformat(),
        "run": {
            "id": record.id,
            "conversation_id": record.conversation_id,
            "agent_name": record.agent_name,
            "status": record.status,
            "last_agent_name": record.last_agent_name,
            "usage": record.usage_json or {},
            "error": record.error,
            "trace_id": record.trace_id,
            "sdk_version": record.sdk_version,
            "cancel_requested": record.cancel_requested,
            "created_at": record.created_at,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": duration_seconds,
        },
        "item_counts": dict(Counter(item.item_type for item in items)),
        "event_counts": dict(Counter(event.event_type for event in events)),
        "compactions": [
            {
                "sequence": event.sequence,
                "event_type": event.event_type,
                "created_at": event.created_at,
                "details": event.payload_json,
            }
            for event in events
            if event.event_type.startswith("context.compact")
        ],
        "epochs": [
            {
                "epoch_index": epoch.epoch_index,
                "status": epoch.status,
                "terminal_reason": epoch.terminal_reason,
                "usage": epoch.usage_json or {},
                "error": epoch.error,
                "started_at": epoch.started_at,
                "finished_at": epoch.finished_at,
            }
            for epoch in record.epochs
        ],
        "tool_attempts": [
            {
                "catalog_id": attempt.catalog_id,
                "attempt": attempt.attempt,
                "status": attempt.status,
                "failure_category": attempt.failure_category,
                "retryable": attempt.retryable,
                "result_ref": attempt.result_ref,
                "error": attempt.error,
                "started_at": attempt.started_at,
                "finished_at": attempt.finished_at,
            }
            for attempt in record.tool_attempts
        ],
        "interruptions": [
            {
                "tool_name": interruption.tool_name,
                "status": interruption.status,
                "created_at": interruption.created_at,
                "resolved_at": interruption.resolved_at,
            }
            for interruption in record.interruptions
        ],
    }
