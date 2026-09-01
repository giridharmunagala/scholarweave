from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from agents import Model
from openai import AsyncOpenAI


@dataclass(frozen=True, slots=True)
class ModelReference:
    provider_profile_id: str | None = None
    model: str | None = None

    @classmethod
    def model_validate(cls, value: Any) -> "ModelReference":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("A model reference must be a mapping.")
        return cls(
            provider_profile_id=value.get("provider_profile_id"),
            model=value.get("model"),
        )


@dataclass(frozen=True, slots=True)
class AgentModelDefaults:
    chat: ModelReference | None = None
    embedding: ModelReference | None = None
    vision: ModelReference | None = None


@dataclass(frozen=True, slots=True)
class ResolvedAgentModel:
    model: Model
    provider_kind: str
    supports_responses: bool
    supports_hosted_tools: bool
    supports_parallel_tool_calls: bool
    model_name: str | None = None
    responses_client: AsyncOpenAI | None = None
    context_window_tokens: int | None = None


class AgentModelResolver(Protocol):
    def resolve_agent_model(
        self,
        reference: ModelReference,
        *,
        require_tools: bool = False,
    ) -> ResolvedAgentModel: ...
