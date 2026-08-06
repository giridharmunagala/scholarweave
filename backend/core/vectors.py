from __future__ import annotations

import math
from collections.abc import Iterable


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
