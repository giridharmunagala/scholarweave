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
ModelCapability = Literal["chat", "embedding", "vision", "tools", "speech"]


class ProviderSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderModel(ProviderSchema):
    name: str = Field(min_length=1, max_length=255)
    capabilities: set[ModelCapability] = Field(default_factory=set)
    reasoning_efforts: list[ReasoningEffort] | None = None
    context_window_tokens: int | None = Field(default=None, ge=4_096, le=2_000_000)
    enabled: bool = True

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

    @model_validator(mode="after")
    def validate_provider(self) -> "ProviderCreate":
        if self.kind not in {"ollama", "openai_compatible"} and not self.api_key:
            raise ValueError(f"{self.kind} providers require an API key.")
        return self


class ProviderUpdate(ProviderSchema):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    kind: ProviderKind | None = None
    base_url: str | None = Field(default=None, min_length=1, max_length=1024)
    api_key: str | None = Field(default=None, max_length=16_384)
    models: list[ProviderModel] | None = None


class ProviderResponse(ProviderSchema):
    id: str
    name: str
    kind: ProviderKind
    base_url: str
    api_key_set: bool
    state: Literal["active", "archived"]
    models: list[ProviderModel]
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


class TranscriptionResponse(ProviderSchema):
    text: str


class BuiltInSpeechStatus(ProviderSchema):
    state: Literal["not_installed", "installing", "ready", "running", "error"]
    available: bool
    installed: bool
    running: bool
    model: str
    downloaded_bytes: int = 0
    total_bytes: int
    error: str | None = None
