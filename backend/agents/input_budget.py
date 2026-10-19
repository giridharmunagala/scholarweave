from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Sequence
from functools import lru_cache
from typing import Any

import tiktoken

from backend.core.errors import ValidationError

_logger = logging.getLogger(__name__)


@lru_cache(maxsize=64)
def _encoding(model: str) -> tiktoken.Encoding | None:
    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        _logger.info("No tokenizer for %s; using a conservative UTF-8 byte budget.", model)
        return None


class InputBudget:
    def __init__(self, model: str, limit: int) -> None:
        self.encoding = _encoding(model)
        self.limit = limit
        self.method = self.encoding.name if self.encoding else "utf8-byte-upper-bound"
        self._provider_counts: dict[str, int] = {}

    @staticmethod
    def serialize(system: str, prompt: str) -> str:
        return json.dumps(
            [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            ensure_ascii=False, separators=(",", ":"),
        )

    def cached_count(self, system: str, prompt: str) -> int | None:
        key = hashlib.sha256(self.serialize(system, prompt).encode("utf-8")).hexdigest()
        return self._provider_counts.get(key)

    def remember_count(self, system: str, prompt: str, tokens: int) -> None:
        key = hashlib.sha256(self.serialize(system, prompt).encode("utf-8")).hexdigest()
        self._provider_counts[key] = tokens

    def count(self, system: str, prompt: str) -> int:
        cached = self.cached_count(system, prompt)
        if cached is not None:
            return cached
        serialized = self.serialize(system, prompt)
        return 32 + (
            len(self.encoding.encode(serialized, disallowed_special=()))
            if self.encoding else len(serialized.encode("utf-8"))
        )

    def batches(self, records: Sequence[Any], system: str) -> list[list[Any]]:
        result: list[list[Any]] = []
        current: list[Any] = []
        for record in records:
            candidate = [*current, record]
            prompt = json.dumps({"stage": "final", "records": candidate}, ensure_ascii=False, separators=(",", ":"))
            if self.count(system, prompt) <= self.limit:
                current = candidate
                continue
            single = json.dumps({"stage": "final", "records": [record]}, ensure_ascii=False, separators=(",", ":"))
            if not current or self.count(system, single) > self.limit:
                raise ValidationError(
                    "One complete source or summary part exceeds the input budget. "
                    "Choose a model with a larger context; records will not be cut or silently omitted."
                )
            result.append(current)
            current = [record]
        if current:
            result.append(current)
        return result
