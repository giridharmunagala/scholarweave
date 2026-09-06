"""Summarize local model-call logs without printing prompts, responses, or credentials."""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


def _number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _distribution(values: list[float]) -> dict[str, float | int | None]:
    ordered = sorted(values)
    return {
        "samples": len(ordered),
        "p50": ordered[math.ceil(len(ordered) * 0.5) - 1] if ordered else None,
        "p95": ordered[math.ceil(len(ordered) * 0.95) - 1] if ordered else None,
        "total": sum(ordered) if ordered else None,
    }


def summarize(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        if not str(entry.get("operation", "")).endswith("/chat/completions"):
            continue
        groups[(str(entry.get("provider", "unknown")), str(entry.get("model", "unknown")))].append(entry)
    summaries = []
    for (provider, model), calls in sorted(groups.items()):
        timing = {}
        for field in ("duration_seconds", "first_byte_seconds", "queue_wait_seconds"):
            values = [
                float(call["timing"][field])
                for call in calls
                if isinstance(call.get("timing"), dict)
                and _number(call["timing"].get(field))
            ]
            timing[field] = _distribution(values)
        usage = [
            call["response"]["usage"]
            for call in calls
            if isinstance(call.get("response"), dict)
            and isinstance(call["response"].get("usage"), dict)
        ]
        input_tokens = [
            float(item["prompt_tokens"]) for item in usage if _number(item.get("prompt_tokens"))
        ]
        output_tokens = [
            float(item["completion_tokens"]) for item in usage if _number(item.get("completion_tokens"))
        ]
        summaries.append({
            "provider": provider,
            "model": model,
            "calls": len(calls),
            "errors": sum("error" in call for call in calls),
            "timing": timing,
            "prompt_tokens": _distribution(input_tokens),
            "completion_tokens": _distribution(output_tokens),
        })
    return summaries


def read_entries(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON at line {line_number}; retry after the writer finishes.") from exc
            if not isinstance(entry, dict):
                raise ValueError(f"Expected a model-call object at line {line_number}.")
            yield entry


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path, help="Path to llm_calls.jsonl (read-only).")
    args = parser.parse_args()
    try:
        report = summarize(read_entries(args.log))
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Cannot generate report: {exc}\n")
    print(json.dumps({
        "models": report,
        "notes": [
            "Missing measurements are null, not zero.",
            "First-byte latency is not necessarily time to first generated token.",
            "Compare the same task set and cache state; these aggregates do not establish speedups.",
            "A model switch must amortize both switch-in and switch-back time.",
        ],
    }, indent=2))


if __name__ == "__main__":
    main()
