from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from backend.agents.harness import ModelBinding

if TYPE_CHECKING:
    from backend.providers.schemas import ProviderModel

# The harness model binding is the only "resolved model" the application needs.
ResolvedAgentModel = ModelBinding


class ProviderRuntimeError(RuntimeError):
    pass


class ProviderDiscoveryError(ProviderRuntimeError):
    def __init__(
        self,
        message: str,
        *,
        manual_models: list[ProviderModel],
    ) -> None:
        super().__init__(message)
        self.manual_models = manual_models


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


class AgentModelResolver(Protocol):
    def resolve_agent_model(
        self,
        reference: ModelReference,
        *,
        require_tools: bool = False,
    ) -> ResolvedAgentModel: ...
