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
    "last_chat_model_reference",
    "request_timeout_seconds",
    "agent_tracing_enabled",
    "python_tool_enabled",
    "python_tool_timeout_seconds",
    "python_tool_memory_mb",
    "python_tool_allowed_imports",
    "retrieval_max_context_chars",
    "ocr_engine",
    "docling_device",
    "docling_ocr_backend",
    "docling_batch_size",
    "docling_num_threads",
    "ocr_llm_enhancement_enabled",
    "ocr_llm_model",
    "ocr_llm_triage_model",
}
MODEL_DEFAULT_CAPABILITIES = {"chat", "embedding", "vision", "speech"}
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
    agent_tracing_enabled: bool
    python_tool_enabled: bool
    python_tool_timeout_seconds: float
    python_tool_memory_mb: int
    python_tool_allowed_imports: list[str]
    retrieval_max_context_chars: int
    ocr_engine: Literal["tesseract", "docling"]
    docling_device: Literal["auto", "cuda", "cpu"]
    docling_ocr_backend: Literal["onnxruntime", "torch"]
    docling_batch_size: int
    docling_num_threads: int
    ocr_llm_enhancement_enabled: bool
    ocr_llm_model: str | None
    ocr_llm_triage_model: str | None


class SettingsUpdate(SettingsSchema):
    ollama_base_url: str | None = None
    default_model_references: dict[str, ModelReferenceSpec] | None = None
    last_chat_model_reference: ModelReferenceSpec | None = None
    request_timeout_seconds: float | None = Field(default=None, gt=0, le=600)
    agent_tracing_enabled: bool | None = None
    python_tool_enabled: bool | None = None
    python_tool_timeout_seconds: float | None = Field(default=None, gt=0, le=300)
    python_tool_memory_mb: int | None = Field(default=None, ge=32, le=8192)
    python_tool_allowed_imports: list[str] | None = None
    retrieval_max_context_chars: int | None = Field(default=None, ge=1_000, le=1_000_000)
    ocr_engine: Literal["tesseract", "docling"] | None = None
    docling_device: Literal["auto", "cuda", "cpu"] | None = None
    docling_ocr_backend: Literal["onnxruntime", "torch"] | None = None
    docling_batch_size: int | None = Field(default=None, ge=1, le=32)
    docling_num_threads: int | None = Field(default=None, ge=1, le=64)
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
                    value = record.value_json
                    if key == "ocr_engine" and value == "surya":
                        value = "docling"
                        record.value_json = value
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
            agent_tracing_enabled=self.settings.agent_tracing_enabled,
            python_tool_enabled=self.settings.python_tool_enabled,
            python_tool_timeout_seconds=self.settings.python_tool_timeout_seconds,
            python_tool_memory_mb=self.settings.python_tool_memory_mb,
            python_tool_allowed_imports=self.settings.python_tool_allowed_imports,
            retrieval_max_context_chars=self.settings.retrieval_max_context_chars,
            ocr_engine=self.settings.ocr_engine,
            docling_device=self.settings.docling_device,
            docling_ocr_backend=self.settings.docling_ocr_backend,
            docling_batch_size=self.settings.docling_batch_size,
            docling_num_threads=self.settings.docling_num_threads,
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
