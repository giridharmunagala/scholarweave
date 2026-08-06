from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any


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
