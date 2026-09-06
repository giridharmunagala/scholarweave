from __future__ import annotations

from typing import Literal

ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]

REASONING_EFFORTS: tuple[ReasoningEffort, ...] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)


def infer_reasoning_efforts(
    provider_kind: str,
    model_name: str,
) -> list[ReasoningEffort] | None:
    """Return conservative known effort levels; None means the model is unknown."""

    model = model_name.casefold()
    if provider_kind == "openai_compatible":
        model = model.rsplit("/", 1)[-1]
    if model.startswith("gpt-5.6-"):
        return ["none", "low", "medium", "high", "xhigh", "max"]
    if model.startswith("gpt-5.5-pro"):
        return ["medium", "high", "xhigh"]
    if model.startswith(("gpt-5.5", "gpt-5.4")):
        return ["none", "low", "medium", "high", "xhigh"]
    if model.startswith("gpt-5.3-codex"):
        return ["low", "medium", "high", "xhigh"]
    if model.startswith("gpt-5-mini"):
        return ["low", "medium", "high"]
    if model == "gpt-5" or model.startswith("gpt-5-"):
        return ["minimal", "low", "medium", "high"]
    if provider_kind == "openai_compatible" and model.startswith("gemma-4-"):
        return ["none", "high"]
    if provider_kind == "openai_compatible" and model.startswith("qwen3.8-"):
        return ["none", "low", "medium", "xhigh"]
    if provider_kind == "openai_compatible" and model.startswith("qwen3.6-"):
        return ["low", "medium", "xhigh"]
    if provider_kind == "openai_compatible" and model == "qwen-27b":
        return ["none", "high"]
    return None
