from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session, sessionmaker
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.agents.blueprint import ModelReferenceSpec
from backend.core.config import Settings
from backend.core.models import AppSetting
from backend.utils import utcnow

PERSISTED_SETTING_KEYS = {
    "ollama_base_url",
    "default_model_references",
    "last_chat_model_reference",
    "request_timeout_seconds",
    "agent_context_window_tokens",
    "agent_context_high_water_ratio",
    "agent_context_compaction_target_tokens",
    "tool_result_max_tokens",
    "agent_epoch_max_turns",
    "agent_max_epochs",
    "agent_run_timeout_seconds",
    "tool_call_timeout_seconds",
    "tool_read_retry_attempts",
    "agent_tracing_enabled",
    "user_timezone",
    "user_profile",
    "retrieval_max_context_chars",
    "ocr_llm_enhancement_enabled",
    "ocr_llm_model",
    "ocr_llm_triage_model",
}
MODEL_DEFAULT_CAPABILITIES = {
    "chat",
    "embedding",
    "vision",
}
OPTIONAL_MODEL_SETTING_KEYS = {"ocr_llm_model", "ocr_llm_triage_model"}


def _normalize_model_references(value: dict[str, Any]) -> dict[str, Any]:
    return {
        capability: reference
        for capability, reference in value.items()
        if capability in MODEL_DEFAULT_CAPABILITIES
    }


def _normalize_optional_model(value: Any) -> str | None:
    return value if isinstance(value, str) else None


class SettingsSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SettingsResponse(SettingsSchema):
    data_dir: str
    workspace_dir: str
    artifacts_dir: str
    documents_dir: str
    database_path: str
    ollama_base_url: str
    default_model_references: dict[str, ModelReferenceSpec]
    last_chat_model_reference: ModelReferenceSpec
    request_timeout_seconds: float
    agent_context_window_tokens: int
    agent_context_high_water_ratio: float
    agent_context_compaction_target_tokens: int
    tool_result_max_tokens: int
    agent_epoch_max_turns: int
    agent_max_epochs: int
    agent_run_timeout_seconds: float
    tool_call_timeout_seconds: float
    tool_read_retry_attempts: int
    agent_tracing_enabled: bool
    user_timezone: str
    user_profile: str
    retrieval_max_context_chars: int
    ocr_engine: Literal["tesseract"]
    ocr_llm_enhancement_enabled: bool
    ocr_llm_model: str | None
    ocr_llm_triage_model: str | None


class SettingsUpdate(SettingsSchema):
    ollama_base_url: str | None = None
    default_model_references: dict[str, ModelReferenceSpec] | None = None
    last_chat_model_reference: ModelReferenceSpec | None = None
    request_timeout_seconds: float | None = Field(default=None, gt=0, le=600)
    agent_context_window_tokens: int | None = Field(default=None, ge=4_096)
    agent_context_high_water_ratio: float | None = Field(default=None, ge=0.5, le=0.9)
    agent_context_compaction_target_tokens: int | None = Field(default=None, ge=512)
    tool_result_max_tokens: int | None = Field(default=None, ge=256)
    agent_epoch_max_turns: int | None = Field(default=None, ge=2, le=100)
    agent_max_epochs: int | None = Field(default=None, ge=1, le=50)
    agent_run_timeout_seconds: float | None = Field(default=None, ge=30, le=86_400)
    tool_call_timeout_seconds: float | None = Field(default=None, ge=1, le=3_600)
    tool_read_retry_attempts: int | None = Field(default=None, ge=1, le=5)
    agent_tracing_enabled: bool | None = None
    user_timezone: str | None = Field(default=None, min_length=1, max_length=100)
    user_profile: str | None = Field(default=None, max_length=2_000)
    retrieval_max_context_chars: int | None = Field(default=None, ge=1_000, le=1_000_000)
    ocr_llm_enhancement_enabled: bool | None = None
    ocr_llm_model: str | None = None
    ocr_llm_triage_model: str | None = None

    @field_validator("user_timezone")
    @classmethod
    def _validate_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown timezone '{value}'.") from exc
        return value


class SettingsService:
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
    ) -> None:
        self.settings = settings
        self._sessions = session_factory

    def load(self) -> None:
        with self._sessions() as session:
            for key in PERSISTED_SETTING_KEYS:
                record = session.get(AppSetting, key)
                if record is not None:
                    value = record.value_json
                    if key == "default_model_references":
                        value = _normalize_model_references(value)
                        record.value_json = value
                    if key == "last_chat_model_reference":
                        value = ModelReferenceSpec.model_validate(value).model_dump(mode="json")
                        record.value_json = value
                    if key in OPTIONAL_MODEL_SETTING_KEYS:
                        value = _normalize_optional_model(value)
                        if value is None:
                            session.delete(record)
                    setattr(self.settings, key, value)
            session.commit()
        self.settings.derive_paths()

    def response(self) -> SettingsResponse:
        return SettingsResponse(
            data_dir=str(self.settings.data_dir),
            workspace_dir=str(self.settings.workspace_dir),
            artifacts_dir=str(self.settings.artifacts_dir),
            documents_dir=str(self.settings.documents_dir),
            database_path=str(self.settings.database_path),
            ollama_base_url=self.settings.ollama_base_url,
            default_model_references={
                key: ModelReferenceSpec.model_validate(value)
                for key, value in self.settings.default_model_references.items()
            },
            last_chat_model_reference=ModelReferenceSpec.model_validate(
                self.settings.last_chat_model_reference
            ),
            request_timeout_seconds=self.settings.request_timeout_seconds,
            agent_context_window_tokens=self.settings.agent_context_window_tokens,
            agent_context_high_water_ratio=self.settings.agent_context_high_water_ratio,
            agent_context_compaction_target_tokens=(
                self.settings.agent_context_compaction_target_tokens
            ),
            tool_result_max_tokens=self.settings.tool_result_max_tokens,
            agent_epoch_max_turns=self.settings.agent_epoch_max_turns,
            agent_max_epochs=self.settings.agent_max_epochs,
            agent_run_timeout_seconds=self.settings.agent_run_timeout_seconds,
            tool_call_timeout_seconds=self.settings.tool_call_timeout_seconds,
            tool_read_retry_attempts=self.settings.tool_read_retry_attempts,
            agent_tracing_enabled=self.settings.agent_tracing_enabled,
            user_timezone=self.settings.user_timezone,
            user_profile=self.settings.user_profile,
            retrieval_max_context_chars=self.settings.retrieval_max_context_chars,
            ocr_engine="tesseract",
            ocr_llm_enhancement_enabled=self.settings.ocr_llm_enhancement_enabled,
            ocr_llm_model=_normalize_optional_model(self.settings.ocr_llm_model),
            ocr_llm_triage_model=_normalize_optional_model(
                self.settings.ocr_llm_triage_model
            ),
        )

    def update(self, payload: SettingsUpdate) -> SettingsResponse:
        values = payload.model_dump(exclude_unset=True)
        normalized: dict[str, Any] = {}
        for key, value in values.items():
            if key in {"user_timezone", "user_profile"} and value is None:
                continue
            if key == "default_model_references" and value is not None:
                value = {
                    capability: reference.model_dump(mode="json")
                    if isinstance(reference, ModelReferenceSpec)
                    else reference
                    for capability, reference in value.items()
                }
                value = _normalize_model_references(value)
            if key == "last_chat_model_reference" and isinstance(
                value, ModelReferenceSpec
            ):
                value = value.model_dump(mode="json")
            normalized[key] = value
        candidate = self.settings.model_copy(update=normalized)
        candidate.derive_paths()
        with self._sessions() as session:
            for key, value in normalized.items():
                setattr(self.settings, key, value)
                record = session.get(AppSetting, key)
                if record is None:
                    session.add(AppSetting(key=key, value_json=value))
                else:
                    record.value_json = value
                    record.updated_at = utcnow()
            session.commit()
        return self.response()
