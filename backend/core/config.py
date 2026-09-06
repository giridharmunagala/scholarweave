"""Application configuration for the native agent runtime."""

from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[2]
RUNTIME_VERSION = "native-1"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SCHOLARWEAVE_", env_file=".env", extra="ignore")

    app_name: str = "ScholarWeave API"
    api_prefix: str = "/api"
    data_dir: Path = Field(default_factory=lambda: ROOT_DIR / "local_data")
    workspace_dir: Path = Field(default_factory=lambda: ROOT_DIR / "workspace")
    frontend_dist_dir: Path = Field(default_factory=lambda: ROOT_DIR / "frontend" / "dist")
    artifacts_dir: Path | None = None
    documents_dir: Path | None = None
    database_path: Path | None = None
    llm_log_path: Path | None = None
    prompt_config_dir: Path | None = None

    ollama_base_url: str = "http://127.0.0.1:11434"
    default_model_references: dict[str, dict[str, str | None]] = Field(default_factory=dict)
    last_chat_model_reference: dict[str, str | None] = Field(default_factory=dict)
    request_timeout_seconds: float = 300.0
    agent_context_window_tokens: int = Field(default=32_768, ge=4_096, le=2_000_000)
    agent_context_high_water_ratio: float = Field(default=0.85, ge=0.5, le=0.95)
    agent_context_use_model_window: bool = Field(
        default=True,
        description="Legacy compatibility setting; model-window budgeting is always active.",
    )
    agent_working_context_tokens: int = Field(
        default=12_000, ge=2_048, le=500_000,
        description="Legacy compatibility value; no longer limits model input.",
    )
    agent_context_response_reserve_tokens: int = Field(
        default=2_048, ge=256, le=128_000,
        description="Initial generation allowance, including reasoning; automatically grows within model context.",
    )
    agent_context_model_summary_enabled: bool = True
    agent_context_compaction_target_tokens: int = Field(
        default=8_192,
        ge=1_024,
        le=500_000,
        description=(
            "Legacy compatibility value; retention targets now follow model context capacity."
        ),
    )
    tool_result_max_tokens: int = Field(
        default=3_000, ge=512, le=16_000,
        description="Legacy compatibility value; fresh tool outputs are not capped.",
    )
    agent_epoch_max_turns: int = Field(default=12, ge=2, le=100)
    agent_max_epochs: int = Field(
        default=8, ge=1, le=50, description="Legacy compatibility value; epochs have no total ceiling.",
    )
    agent_run_timeout_seconds: float = Field(
        default=3_600.0, ge=30.0, description="Legacy compatibility value; runs have no lifetime deadline.",
    )
    tool_call_timeout_seconds: float = Field(
        default=600.0, ge=1.0, description="Legacy compatibility value; tools have no whole-operation deadline.",
    )
    tool_read_retry_attempts: int = Field(default=2, ge=1, le=5)

    arxiv_api_url: str = "https://export.arxiv.org/api/query"
    wikipedia_api_url: str = "https://en.wikipedia.org/w/api.php"
    search_user_agent: str = (
        "ScholarWeave/0.1 (+https://github.com/giridharmunagala/scholarweave)"
    )
    search_request_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    web_search_max_requests_per_session: int = Field(default=100, ge=1, le=100)
    arxiv_search_requests_per_minute: int = Field(default=20, ge=1, le=20)
    wikipedia_search_requests_per_minute: int = Field(default=60, ge=1, le=600)
    web_source_ttl_minutes: int = Field(default=240, ge=5, le=1440)
    max_web_source_bytes: int = 5 * 1024 * 1024
    max_temporary_web_sources: int = Field(default=20, ge=1, le=100)

    run_retention_days: int = Field(default=2, ge=1, le=365)
    user_timezone: str = "Asia/Kolkata"
    user_profile: str = "Based in Hyderabad, Telangana, India."

    max_upload_bytes: int = 40 * 1024 * 1024
    max_workspace_file_bytes: int = 2 * 1024 * 1024
    max_artifact_bytes: int = 6 * 1024 * 1024
    retrieval_max_context_chars: int = 40000
    max_chunk_chars: int = 2500
    max_chunks_per_document: int = 2000
    pdf_min_text_chars: int = 40
    ocr_language: str = "eng"
    ocr_llm_enhancement_enabled: bool = False
    ocr_llm_model: str | None = None
    ocr_llm_triage_model: str | None = None

    @model_validator(mode="after")
    def derive_paths(self) -> "Settings":
        try:
            ZoneInfo(self.user_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown user timezone '{self.user_timezone}'.") from exc
        self.data_dir = self.data_dir.resolve()
        self.workspace_dir = self.workspace_dir.resolve()
        self.frontend_dist_dir = self.frontend_dist_dir.resolve()
        self.artifacts_dir = (self.artifacts_dir or self.data_dir / "artifacts").resolve()
        self.documents_dir = (self.documents_dir or self.data_dir / "documents").resolve()
        self.database_path = (self.database_path or self.data_dir / "metadata.sqlite3").resolve()
        self.llm_log_path = (self.llm_log_path or self.data_dir / "llm_calls.jsonl").resolve()
        self.prompt_config_dir = (
            self.prompt_config_dir or self.data_dir / "config"
        ).resolve()
        return self

    def ensure_directories(self) -> None:
        for path in [
            self.data_dir,
            self.workspace_dir,
            self.artifacts_dir,
            self.documents_dir,
            self.prompt_config_dir,
        ]:
            path.mkdir(parents=True, exist_ok=True)
