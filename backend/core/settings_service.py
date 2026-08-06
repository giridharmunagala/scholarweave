from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from backend.agents.blueprint import ModelReferenceSpec
from backend.core.config import Settings
from backend.core.models import AppSetting
from backend.core.time import utcnow

PERSISTED_SETTING_KEYS = {
    "ollama_base_url",
    "default_model_references",
    "request_timeout_seconds",
    "agent_tracing_enabled",
    "agent_compaction_threshold_items",
    "agent_compaction_recent_items",
    "python_tool_enabled",
    "python_tool_timeout_seconds",
    "python_tool_memory_mb",
    "python_tool_allowed_imports",
    "retrieval_max_context_chars",
    "ocr_engine",
    "surya_model",
    "surya_device",
    "surya_max_new_tokens",
    "surya_max_image_width",
    "surya_timeout_seconds",
    "surya_unload_ollama_models",
    "ocr_llm_enhancement_enabled",
    "ocr_llm_model",
    "ocr_llm_triage_model",
}


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
    request_timeout_seconds: float
    agent_tracing_enabled: bool
    agent_compaction_threshold_items: int
    agent_compaction_recent_items: int
    python_tool_enabled: bool
    python_tool_timeout_seconds: float
    python_tool_memory_mb: int
    python_tool_allowed_imports: list[str]
    retrieval_max_context_chars: int
    ocr_engine: Literal["tesseract", "surya"]
    surya_model: str
    surya_device: Literal["auto", "cuda", "cpu"]
    surya_max_new_tokens: int
    surya_max_image_width: int
    surya_timeout_seconds: float
    surya_unload_ollama_models: bool
    ocr_llm_enhancement_enabled: bool
    ocr_llm_model: str | None
    ocr_llm_triage_model: str | None


class SettingsUpdate(SettingsSchema):
    ollama_base_url: str | None = None
    default_model_references: dict[str, ModelReferenceSpec] | None = None
    request_timeout_seconds: float | None = Field(default=None, gt=0, le=600)
    agent_tracing_enabled: bool | None = None
    agent_compaction_threshold_items: int | None = Field(default=None, ge=4, le=10_000)
    agent_compaction_recent_items: int | None = Field(default=None, ge=2, le=1_000)
    python_tool_enabled: bool | None = None
    python_tool_timeout_seconds: float | None = Field(default=None, gt=0, le=300)
    python_tool_memory_mb: int | None = Field(default=None, ge=32, le=8192)
    python_tool_allowed_imports: list[str] | None = None
    retrieval_max_context_chars: int | None = Field(default=None, ge=1_000, le=1_000_000)
    ocr_engine: Literal["tesseract", "surya"] | None = None
    surya_model: str | None = None
    surya_device: Literal["auto", "cuda", "cpu"] | None = None
    surya_max_new_tokens: int | None = Field(default=None, ge=512, le=32768)
    surya_max_image_width: int | None = Field(default=None, ge=512, le=4096)
    surya_timeout_seconds: float | None = Field(default=None, ge=30, le=7200)
    surya_unload_ollama_models: bool | None = None
    ocr_llm_enhancement_enabled: bool | None = None
    ocr_llm_model: str | None = None
    ocr_llm_triage_model: str | None = None


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
                    setattr(self.settings, key, record.value_json)

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
            request_timeout_seconds=self.settings.request_timeout_seconds,
            agent_tracing_enabled=self.settings.agent_tracing_enabled,
            agent_compaction_threshold_items=self.settings.agent_compaction_threshold_items,
            agent_compaction_recent_items=self.settings.agent_compaction_recent_items,
            python_tool_enabled=self.settings.python_tool_enabled,
            python_tool_timeout_seconds=self.settings.python_tool_timeout_seconds,
            python_tool_memory_mb=self.settings.python_tool_memory_mb,
            python_tool_allowed_imports=self.settings.python_tool_allowed_imports,
            retrieval_max_context_chars=self.settings.retrieval_max_context_chars,
            ocr_engine=self.settings.ocr_engine,
            surya_model=self.settings.surya_model,
            surya_device=self.settings.surya_device,
            surya_max_new_tokens=self.settings.surya_max_new_tokens,
            surya_max_image_width=self.settings.surya_max_image_width,
            surya_timeout_seconds=self.settings.surya_timeout_seconds,
            surya_unload_ollama_models=self.settings.surya_unload_ollama_models,
            ocr_llm_enhancement_enabled=self.settings.ocr_llm_enhancement_enabled,
            ocr_llm_model=self.settings.ocr_llm_model,
            ocr_llm_triage_model=self.settings.ocr_llm_triage_model,
        )

    def update(self, payload: SettingsUpdate) -> SettingsResponse:
        values = payload.model_dump(exclude_unset=True)
        normalized: dict[str, Any] = {}
        for key, value in values.items():
            if key == "default_model_references" and value is not None:
                value = {
                    capability: reference.model_dump(mode="json")
                    if isinstance(reference, ModelReferenceSpec)
                    else reference
                    for capability, reference in value.items()
                }
            normalized[key] = value
        recent = normalized.get(
            "agent_compaction_recent_items",
            self.settings.agent_compaction_recent_items,
        )
        threshold = normalized.get(
            "agent_compaction_threshold_items",
            self.settings.agent_compaction_threshold_items,
        )
        if recent >= threshold:
            raise ValueError(
                "agent_compaction_recent_items must be smaller than the compaction threshold."
            )
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
