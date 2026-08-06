from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, Text, create_engine, inspect
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator

from backend.core.config import Settings
from backend.core.json import dumps_json, loads_json

SCHEMA_GENERATION = 2
_ARTIFACTS_BACKUP_TABLE = "_sdk_cutover_artifacts"
_CHUNKS_BACKUP_TABLE = "_sdk_cutover_document_chunks"
_RUNTIME_TABLES = (
    "agent_run_interruptions",
    "agent_run_events",
    "agent_run_items",
    "agent_runs",
    "sdk_session_items",
    "sdk_sessions",
    "conversations",
    "function_tool_revisions",
    "function_tool_definitions",
    "agent_revisions",
    "agent_definitions",
    "node_runs",
    "run_events",
    "runs",
    "custom_node_revisions",
    "custom_node_definitions",
    "agent_versions",
    "agents",
    "workflow_versions",
    "workflows",
)
_OBSOLETE_SETTING_KEYS = (
    "agent_provider",
    "agent_max_turns",
    "agent_utility_model_reference",
    "agent_utility_fallback_enabled",
    "agent_todo_max_items",
    "agent_todo_max_depth",
    "agent_todo_max_retries",
    "agent_max_replans",
    "agent_compaction_threshold_chars",
    "agent_memory_max_file_bytes",
    "default_generation_model",
    "default_embedding_model",
    "max_map_items",
    "max_repeat_iterations",
    "max_concurrent_nodes",
    "max_subagent_depth",
    "max_subworkflow_depth",
)
_RENAMED_SETTING_KEYS = {
    "python_node_enabled": "python_tool_enabled",
    "python_node_timeout_seconds": "python_tool_timeout_seconds",
    "python_node_memory_mb": "python_tool_memory_mb",
    "python_node_allowed_imports": "python_tool_allowed_imports",
    "max_context_chars": "retrieval_max_context_chars",
}


class Base(DeclarativeBase):
    pass


class JSONText(TypeDecorator[Any]):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        return dumps_json(value)

    def process_result_value(self, value: str | None, dialect: Any) -> Any:
        return loads_json(value)


def create_session_factory(settings: Settings) -> sessionmaker[Session]:
    _register_models()
    engine = create_engine(
        f"sqlite:///{settings.database_path}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    _cut_over_schema(engine, settings)
    Base.metadata.create_all(engine)
    _restore_preserved_runtime_dependents(engine)
    _write_schema_generation(engine)
    return sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _register_models() -> None:
    from backend.agents import models as agent_models  # noqa: F401
    from backend.conversations import models as conversation_models  # noqa: F401
    from backend.core import models as core_models  # noqa: F401
    from backend.documents import models as document_models  # noqa: F401
    from backend.providers import models as provider_models  # noqa: F401
    from backend.runs import models as run_models  # noqa: F401
    from backend.tools import models as tool_models  # noqa: F401


def _cut_over_schema(engine: Engine, settings: Settings) -> None:
    tables = set(inspect(engine).get_table_names())
    generation = _read_schema_generation(engine) if "app_settings" in tables else None
    if generation == SCHEMA_GENERATION:
        return
    if not tables:
        return

    _backup_database(settings.database_path)
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
        with connection.begin():
            current = set(inspect(connection).get_table_names())
            if (
                "document_chunks" in current
                and _CHUNKS_BACKUP_TABLE not in current
            ):
                connection.exec_driver_sql(
                    f'ALTER TABLE "document_chunks" RENAME TO "{_CHUNKS_BACKUP_TABLE}"'
                )
            current = set(inspect(connection).get_table_names())
            if "artifacts" in current and _ARTIFACTS_BACKUP_TABLE not in current:
                connection.exec_driver_sql(
                    f'ALTER TABLE "artifacts" RENAME TO "{_ARTIFACTS_BACKUP_TABLE}"'
                )
            current = set(inspect(connection).get_table_names())
            for table in _RUNTIME_TABLES:
                if table in current:
                    connection.exec_driver_sql(f'DROP TABLE "{table}"')
            if "app_settings" in current:
                _migrate_setting_keys(connection)
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.commit()
    _remove_legacy_memory(settings)


def _restore_preserved_runtime_dependents(engine: Engine) -> None:
    tables = set(inspect(engine).get_table_names())
    if not ({_ARTIFACTS_BACKUP_TABLE, _CHUNKS_BACKUP_TABLE} & tables):
        return
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
        with connection.begin():
            if _ARTIFACTS_BACKUP_TABLE in tables:
                connection.exec_driver_sql(
                    f"""
                    INSERT OR IGNORE INTO artifacts (
                        id, document_id, run_id, owner_type, kind, relative_path,
                        media_type, size_bytes, sha256, metadata_json, created_at, updated_at
                    )
                    SELECT
                        id, document_id, run_id, owner_type, kind, relative_path,
                        media_type, size_bytes, sha256, metadata_json, created_at, updated_at
                    FROM {_ARTIFACTS_BACKUP_TABLE}
                    """
                )
            if _CHUNKS_BACKUP_TABLE in tables:
                connection.exec_driver_sql(
                    f"""
                    INSERT OR IGNORE INTO document_chunks (
                        id, document_id, artifact_id, chunk_index, section_title,
                        page_start, page_end, citation, text, embedding_json,
                        metadata_json, created_at, updated_at
                    )
                    SELECT
                        id, document_id, artifact_id, chunk_index, section_title,
                        page_start, page_end, citation, text, embedding_json,
                        metadata_json, created_at, updated_at
                    FROM {_CHUNKS_BACKUP_TABLE}
                    """
                )
                connection.exec_driver_sql(f'DROP TABLE "{_CHUNKS_BACKUP_TABLE}"')
            if _ARTIFACTS_BACKUP_TABLE in tables:
                connection.exec_driver_sql(f'DROP TABLE "{_ARTIFACTS_BACKUP_TABLE}"')
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.commit()


def _read_schema_generation(engine: Engine) -> int | None:
    with engine.connect() as connection:
        row = connection.exec_driver_sql(
            "SELECT value_json FROM app_settings WHERE key = ?",
            ("schema_generation",),
        ).first()
    if row is None:
        return None
    value = loads_json(row[0], default=None)
    return value if isinstance(value, int) else None


def _write_schema_generation(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            INSERT INTO app_settings (key, value_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            (
                "schema_generation",
                dumps_json(SCHEMA_GENERATION),
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def _migrate_setting_keys(connection) -> None:
    for old_key, new_key in _RENAMED_SETTING_KEYS.items():
        connection.exec_driver_sql(
            """
            INSERT OR REPLACE INTO app_settings (key, value_json, updated_at)
            SELECT ?, value_json, updated_at FROM app_settings WHERE key = ?
            """,
            (new_key, old_key),
        )
        connection.exec_driver_sql(
            "DELETE FROM app_settings WHERE key = ?",
            (old_key,),
        )
    for key in _OBSOLETE_SETTING_KEYS:
        connection.exec_driver_sql(
            "DELETE FROM app_settings WHERE key = ?",
            (key,),
        )


def _backup_database(database_path: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = database_path.with_name(
        f"{database_path.stem}.pre-sdk-{timestamp}{database_path.suffix}"
    )
    source = sqlite3.connect(database_path)
    destination = sqlite3.connect(backup_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return backup_path


def _remove_legacy_memory(settings: Settings) -> None:
    memory_dir = (settings.data_dir / "agent_memory").resolve()
    protected = {
        settings.data_dir.resolve(),
        settings.workspace_dir.resolve(),
        settings.artifacts_dir.resolve(),
        settings.documents_dir.resolve(),
    }
    if memory_dir in protected:
        raise RuntimeError("Refusing to remove a protected data directory.")
    if memory_dir.exists():
        shutil.rmtree(memory_dir)
