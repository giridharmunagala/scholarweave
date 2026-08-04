from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent


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

    ollama_base_url: str = "http://127.0.0.1:11434"
    default_generation_model: str | None = None
    default_embedding_model: str | None = None
    # Capability-specific references are persisted in app_settings. The two legacy
    # names above remain the compatibility fallback for older workflow JSON.
    default_model_references: dict[str, dict[str, str | None]] = Field(default_factory=dict)
    request_timeout_seconds: float = 60.0

    # Agents SDK runtime. "ollama" talks to the local server's OpenAI-compatible
    # /v1 endpoint; "openai" talks to the cloud and needs a real key.
    agent_provider: Literal["ollama", "openai"] = "ollama"
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    agent_max_turns: int = 10
    agent_tracing_enabled: bool = False

    # Python node sandbox.
    python_node_enabled: bool = True
    python_node_timeout_seconds: float = 10.0
    python_node_memory_mb: int = 512
    python_node_allowed_imports: list[str] = Field(
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
    max_context_chars: int = 40000
    max_chunk_chars: int = 2500
    max_chunks_per_document: int = 2000
    max_map_items: int = 1024
    max_repeat_iterations: int = 8
    max_concurrent_nodes: int = 4
    max_subworkflow_depth: int = 4
    pdf_min_text_chars: int = 40
    ocr_language: str = "eng"
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
        return self

    def ensure_directories(self) -> None:
        for path in [self.data_dir, self.workspace_dir, self.artifacts_dir, self.documents_dir]:
            path.mkdir(parents=True, exist_ok=True)
