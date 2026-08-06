from __future__ import annotations

import json
from typing import Any

from fastapi.encoders import jsonable_encoder


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
