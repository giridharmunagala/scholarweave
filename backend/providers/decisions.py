from __future__ import annotations

import json
import math
from typing import Any
from urllib.parse import urlsplit

from backend.core.errors import ValidationError
from backend.providers.runtime import ModelRuntime
from backend.providers.types import ModelReference, ProviderRuntimeError


def decision_input_size(state: Any, questions: dict) -> int:
    # Unknown decision tokenizers use a conservative byte upper bound, including the rubric.
    return len(json.dumps({"state": state, "questions": questions}, ensure_ascii=False).encode("utf-8")) + 64


def validate_decisions(payload: Any, questions: dict) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("answers"), dict):
        raise ProviderRuntimeError("Decision provider returned no typed answers.")
    answers = payload["answers"]
    if set(answers) != set(questions):
        raise ProviderRuntimeError("Decision provider returned different question IDs.")
    for name, question in questions.items():
        answer = answers[name]
        if not isinstance(answer, dict) or answer.get("type") != question["type"]:
            raise ProviderRuntimeError("Decision provider returned an invalid answer type.")
        if question["type"] == "noul":
            _probability(answer.get("noul"))
            continue
        probabilities = answer.get("probabilities")
        options = (
            set(question["criteria"]) if question["type"] == "choice"
            else {str(index) for index in range(len(question["criteria"]))}
        )
        if not isinstance(probabilities, dict) or set(probabilities) != options:
            raise ProviderRuntimeError("Decision probabilities do not match the supplied choices.")
        for value in probabilities.values():
            _probability(value)
        if not math.isclose(sum(probabilities.values()), 1, abs_tol=0.002):
            raise ProviderRuntimeError("Decision probabilities do not sum to one.")
        if question["type"] == "choice":
            choice = answer.get("choice")
            if choice not in options or probabilities[choice] != max(probabilities.values()):
                raise ProviderRuntimeError("Decision provider returned an inconsistent winning choice.")
        else:
            score = answer.get("score")
            if isinstance(score, bool) or not isinstance(score, (float, int)) or not math.isfinite(score):
                raise ProviderRuntimeError("Decision provider returned a non-finite score.")
            expected = sum(int(key) * value for key, value in probabilities.items())
            if not math.isclose(score, expected, abs_tol=0.002):
                raise ProviderRuntimeError("Decision provider returned an inconsistent score.")
        if "confidence" in answer:
            _probability(answer["confidence"])
    usage = payload.get("usage")
    if usage is not None:
        if not isinstance(usage, dict):
            raise ProviderRuntimeError("Decision provider returned invalid usage.")
        for name in ("input_tokens", "output_tokens"):
            value = usage.get(name, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ProviderRuntimeError("Decision provider returned invalid token usage.")
        if usage.get("output_tokens", 0) != 0:
            raise ProviderRuntimeError("A native decision response must not report generated output tokens.")
    return payload


def _probability(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ProviderRuntimeError("Decision provider returned an invalid probability.")


async def score_decisions(
    runtime: ModelRuntime, reference: ModelReference, state: Any, questions: dict, *, input_tokens: int,
) -> dict:
    if not questions or any(question.get("type") not in {"choice", "score", "noul"} for question in questions.values()):
        raise ValidationError("Supply supported typed decision questions.")
    resolved = runtime.resolve("decision", model_reference=reference)
    allowance = min(input_tokens, resolved.context_window_tokens or input_tokens)
    if decision_input_size(state, questions) > allowance:
        raise ValidationError(
            "Decision input exceeds this role's declared input allowance; use the main model or a larger verified allowance."
        )
    if resolved.kind != "openai_compatible":
        raise ValidationError("Native decision scoring requires an explicitly configured OpenAI-compatible System One endpoint.")
    async with runtime.client(resolved) as client:
        endpoint = "/v1/systemone" if not urlsplit(resolved.base_url).path.rstrip("/") else "/systemone"
        payload = await client.post(
            endpoint, cast_to=dict[str, Any],
            body={"model": resolved.model, "state": state, "questions": questions},
            options={"max_retries": 0},
        )
    return validate_decisions(payload, questions)
