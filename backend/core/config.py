"""Application configuration with SDK-first runtime settings."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[2]


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

    ollama_base_url: str = "http://127.0.0.1:11434"
    default_model_references: dict[str, dict[str, str | None]] = Field(default_factory=dict)
    last_chat_model_reference: dict[str, str | None] = Field(default_factory=dict)
    request_timeout_seconds: float = 60.0

    searxng_base_url: str = "http://127.0.0.1:8888"
    arxiv_api_url: str = "https://export.arxiv.org/api/query"
    wikipedia_api_url: str = "https://en.wikipedia.org/w/api.php"
    search_user_agent: str = (
        "ScholarWeave/0.1 (+https://github.com/giridharmunagala/scholarweave)"
    )
    search_request_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    web_search_requests_per_minute: int = Field(default=30, ge=1, le=600)
    arxiv_search_requests_per_minute: int = Field(default=20, ge=1, le=20)
    wikipedia_search_requests_per_minute: int = Field(default=60, ge=1, le=600)
    web_source_ttl_minutes: int = Field(default=240, ge=5, le=1440)
    max_web_source_bytes: int = 5 * 1024 * 1024
    max_temporary_web_sources: int = Field(default=20, ge=1, le=100)

    agent_tracing_enabled: bool = False

    python_tool_enabled: bool = True
    python_tool_timeout_seconds: float = 10.0
    python_tool_memory_mb: int = 512
    python_tool_allowed_imports: list[str] = Field(
        default_factory=lambda: [
            "base64",
            "collections",
            "datetime",
            "functools",
            "hashlib",
            "itertools",
            "json",
            "math",
            "random",
            "re",
            "statistics",
            "string",
            "textwrap",
            "unicodedata",
            "urllib.parse",
            "uuid",
        ]
    )

    max_upload_bytes: int = 40 * 1024 * 1024
    max_workspace_file_bytes: int = 2 * 1024 * 1024
    max_artifact_bytes: int = 6 * 1024 * 1024
    retrieval_max_context_chars: int = 40000
    max_chunk_chars: int = 2500
    max_chunks_per_document: int = 2000
    pdf_min_text_chars: int = 40
    ocr_language: str = "eng"
    ocr_engine: Literal["tesseract", "docling"] = "docling"
    docling_device: Literal["auto", "cuda", "cpu"] = "auto"
    docling_ocr_backend: Literal["onnxruntime", "torch"] = "onnxruntime"
    docling_batch_size: int = 4
    docling_num_threads: int = 4
    ocr_llm_enhancement_enabled: bool = False
    ocr_llm_model: str | None = None
    ocr_llm_triage_model: str | None = None

    @model_validator(mode="after")
    def derive_paths(self) -> "Settings":
        self.data_dir = self.data_dir.resolve()
        self.workspace_dir = self.workspace_dir.resolve()
        self.frontend_dist_dir = self.frontend_dist_dir.resolve()
        self.artifacts_dir = (self.artifacts_dir or self.data_dir / "artifacts").resolve()
        self.documents_dir = (self.documents_dir or self.data_dir / "documents").resolve()
        self.database_path = (self.database_path or self.data_dir / "metadata.sqlite3").resolve()
        self.llm_log_path = (self.llm_log_path or self.data_dir / "llm_calls.jsonl").resolve()
        return self

    def ensure_directories(self) -> None:
        for path in [
            self.data_dir,
            self.workspace_dir,
            self.artifacts_dir,
            self.documents_dir,
        ]:
            path.mkdir(parents=True, exist_ok=True)
