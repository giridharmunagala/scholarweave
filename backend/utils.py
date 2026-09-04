from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from fastapi.encoders import jsonable_encoder

ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]


def clean_filename(filename: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("._")
    return cleaned or "file"


def cosine_similarity(a: Iterable[float], b: Iterable[float]) -> float:
    left = list(a)
    right = list(b)
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(x * y for x, y in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(x * x for x in left))
    right_norm = math.sqrt(sum(y * y for y in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def dumps_json(value: Any) -> str:
    return json.dumps(
        jsonable_encoder(value),
        ensure_ascii=False,
        sort_keys=True,
    )


def loads_json(value: str | None, default: Any = None) -> Any:
    if value in (None, ""):
        return default
    return json.loads(value)


async def report_progress(
    progress: ProgressCallback | None,
    payload: dict[str, Any],
) -> None:
    if progress is not None:
        await progress(payload)


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return to_jsonable(model_dump(mode="json"))
    if is_dataclass(value):
        return to_jsonable(asdict(value))
    return str(value)


def merge_usage(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged = dict(left)
    for key, value in right.items():
        current = merged.get(key)
        if (
            isinstance(current, (int, float))
            and not isinstance(current, bool)
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        ):
            merged[key] = current + value
        elif isinstance(current, dict) and isinstance(value, dict):
            merged[key] = merge_usage(current, value)
        elif isinstance(current, list) and isinstance(value, list):
            merged[key] = [*current, *value][-100:]
        else:
            merged[key] = value
    return merged


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
