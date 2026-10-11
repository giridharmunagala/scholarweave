from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.providers.reasoning import REASONING_EFFORTS, ReasoningEffort

ProviderKind = Literal[
    "ollama",
    "openai",
    "azure_openai",
    "azure_foundry",
    "openai_compatible",
]
ModelCapability = Literal["chat", "embedding", "vision", "tools", "decision"]
ProviderApiMode = Literal["chat_completions", "responses"]


class ProviderSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderModel(ProviderSchema):
    name: str = Field(min_length=1, max_length=255)
    capabilities: set[ModelCapability] = Field(default_factory=set)
    reasoning_efforts: list[ReasoningEffort] | None = None
    preserve_thinking: bool = False
    context_window_tokens: int | None = Field(default=None, ge=128, le=2_000_000)
    enabled: bool = True

    @model_validator(mode="after")
    def validate_context_window(self) -> ProviderModel:
        decision_only = "decision" in self.capabilities and not self.capabilities.intersection({"chat", "tools", "vision"})
        if not decision_only and self.context_window_tokens is not None and self.context_window_tokens < 4_096:
            raise ValueError("Generative models require a context window of at least 4096 tokens.")
        return self

    @field_validator("reasoning_efforts")
    @classmethod
    def normalize_reasoning_efforts(
        cls,
        value: list[ReasoningEffort] | None,
    ) -> list[ReasoningEffort] | None:
        if value is None:
            return None
        selected = set(value)
        return [effort for effort in REASONING_EFFORTS if effort in selected]


class ProviderCreate(ProviderSchema):
    name: str = Field(min_length=1, max_length=120)
    kind: ProviderKind
    base_url: str = Field(min_length=1, max_length=1024)
    api_key: str | None = Field(default=None, max_length=16_384)
    models: list[ProviderModel] = Field(default_factory=list)
    api_mode: ProviderApiMode = Field(
        default="chat_completions",
        description="Explicit generation protocol. Responses uses stateless local history and application function tools.",
    )
    default_reasoning_effort: ReasoningEffort | None = Field(
        default=None, description="Reasoning effort when a request has no explicit selection.",
    )
    tool_reasoning_effort: ReasoningEffort | None = Field(
        default=None, description="Override reasoning effort on requests with function tools. Use none for models that reject reasoning with tools.",
    )
    serialize_model_switches: bool | None = Field(
        default=None,
        description="Prevent overlapping calls to different models on this provider. Defaults on for local providers.",
    )

    @model_validator(mode="after")
    def validate_provider(self) -> "ProviderCreate":
        if self.kind not in {"ollama", "openai_compatible"} and not self.api_key:
            raise ValueError(f"{self.kind} providers require an API key.")
        if self.kind == "ollama" and self.api_mode == "responses":
            raise ValueError("Ollama requires Chat Completions; its native API does not support Responses.")
        return self


class ProviderUpdate(ProviderSchema):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    kind: ProviderKind | None = None
    base_url: str | None = Field(default=None, min_length=1, max_length=1024)
    api_key: str | None = Field(default=None, max_length=16_384)
    models: list[ProviderModel] | None = None
    api_mode: ProviderApiMode | None = None
    default_reasoning_effort: ReasoningEffort | None = None
    tool_reasoning_effort: ReasoningEffort | None = None
    serialize_model_switches: bool = Field(
        default=True,
        description="Allow same-model concurrency, but wait for active calls before switching models.",
    )

    @model_validator(mode="after")
    def validate_api_mode(self) -> "ProviderUpdate":
        if "api_mode" in self.model_fields_set and self.api_mode is None:
            raise ValueError("api_mode cannot be null.")
        return self


class ProviderResponse(ProviderSchema):
    id: str
    name: str
    kind: ProviderKind
    base_url: str
    api_key_set: bool
    state: Literal["active", "archived"]
    models: list[ProviderModel]
    api_mode: ProviderApiMode = "chat_completions"
    default_reasoning_effort: ReasoningEffort | None = None
    tool_reasoning_effort: ReasoningEffort | None = None
    serialize_model_switches: bool
    created_at: datetime
    updated_at: datetime


class ProviderModelsResponse(ProviderSchema):
    models: list[ProviderModel]
    discovery_error: str | None = None


class ProviderVerifyRequest(ProviderSchema):
    model: str | None = Field(default=None, max_length=255)


class ProviderVerifyResponse(ProviderSchema):
    provider: ProviderKind
    base_url: str
    model: str | None
    reachable: bool
    tool_calling: bool
    detail: str
