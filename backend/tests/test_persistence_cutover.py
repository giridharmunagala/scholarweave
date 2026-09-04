from __future__ import annotations

import sqlite3

from sqlalchemy import create_engine, inspect, text

from backend.core.config import Settings
from backend.persistence.database import (
    SCHEMA_GENERATION,
    _remove_legacy_compaction_items,
    create_session_factory,
)


def test_sdk_schema_cutover_backs_up_and_preserves_research_data(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        frontend_dist_dir=tmp_path / "frontend",
    )
    settings.ensure_directories()
    legacy_memory = settings.data_dir / "agent_memory" / "builder"
    legacy_memory.mkdir(parents=True)
    (legacy_memory / "index.json").write_text("{}", encoding="utf-8")
    _create_legacy_database(settings.database_path)

    session_factory = create_session_factory(settings)
    engine = session_factory.kw["bind"]
    tables = set(inspect(engine).get_table_names())
    claim_columns = {
        column["name"] for column in inspect(engine).get_columns("agent_run_claims")
    }

    assert {
        "provider_profiles",
        "documents",
        "artifacts",
        "document_chunks",
        "conversations",
        "agent_runs",
        "agent_run_items",
        "agent_run_events",
    } <= tables
    assert {
        "agent_run_interruptions",
        "agents",
        "agent_versions",
        "custom_node_definitions",
        "custom_node_revisions",
        "runs",
        "node_runs",
        "run_events",
        "agent_definitions",
        "agent_revisions",
        "function_tool_definitions",
        "function_tool_revisions",
    }.isdisjoint(tables)
    assert {"run_id", "owner_id", "generation", "token", "expires_at"} <= claim_columns

    with engine.connect() as connection:
        assert connection.execute(text("SELECT name FROM provider_profiles")).scalar_one() == "Provider"
        assert connection.execute(text("SELECT title FROM documents")).scalar_one() == "Paper"
        assert connection.execute(text("SELECT relative_path FROM artifacts")).scalar_one() == "paper/source.pdf"
        assert connection.execute(text("SELECT text FROM document_chunks")).scalar_one() == "Preserved text"
        assert connection.execute(
            text("SELECT value_json FROM app_settings WHERE key='schema_generation'")
        ).scalar_one() == str(SCHEMA_GENERATION)
        assert connection.execute(
            text("SELECT COUNT(*) FROM app_settings WHERE key='python_tool_enabled'")
        ).scalar_one() == 0
        assert connection.execute(
            text("SELECT COUNT(*) FROM app_settings WHERE key='agent_todo_max_items'")
        ).scalar_one() == 0
        artifact_foreign_keys = connection.exec_driver_sql(
            "PRAGMA foreign_key_list(artifacts)"
        ).all()
        assert not any(row[2] == "runs" for row in artifact_foreign_keys)

    backups = list(settings.database_path.parent.glob("metadata.pre-sdk-*.sqlite3"))
    assert len(backups) == 1
    assert backups[0].stat().st_size > 0
    assert not (settings.data_dir / "agent_memory").exists()

    engine.dispose()
    second_factory = create_session_factory(settings)
    second_factory.kw["bind"].dispose()
    assert len(list(settings.database_path.parent.glob("metadata.pre-sdk-*.sqlite3"))) == 1


def test_legacy_compaction_items_are_removed_without_deleting_normal_history(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'sessions.sqlite3'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE sdk_session_items (
                id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                message_data TEXT NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            "INSERT INTO sdk_session_items VALUES (1, 'conversation', ?)",
            ('{"role":"assistant","content":"[ScholarWeave history compacted]\\nsummary"}',),
        )
        connection.exec_driver_sql(
            "INSERT INTO sdk_session_items VALUES (2, 'conversation', ?)",
            ('{"role":"user","content":"latest"}',),
        )

    _remove_legacy_compaction_items(engine)

    with engine.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT message_data FROM sdk_session_items ORDER BY id"
        ).scalars().all()
    assert rows == ['{"role":"user","content":"latest"}']


def _create_legacy_database(path) -> None:
    timestamp = "2025-01-01 00:00:00"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE app_settings (
                key TEXT PRIMARY KEY,
                value_json TEXT,
                updated_at DATETIME
            );
            CREATE TABLE provider_profiles (
                id TEXT PRIMARY KEY,
                name TEXT UNIQUE,
                kind TEXT,
                base_url TEXT,
                api_version TEXT,
                api_key TEXT,
                state TEXT,
                models_json TEXT,
                created_at DATETIME,
                updated_at DATETIME
            );
            CREATE TABLE documents (
                id TEXT PRIMARY KEY,
                title TEXT,
                source_filename TEXT,
                content_type TEXT,
                status TEXT,
                page_count INTEGER,
                metadata_json TEXT,
                created_at DATETIME,
                updated_at DATETIME
            );
            CREATE TABLE agents (id TEXT PRIMARY KEY);
            CREATE TABLE agent_versions (id TEXT PRIMARY KEY, agent_id TEXT);
            CREATE TABLE custom_node_definitions (id TEXT PRIMARY KEY);
            CREATE TABLE custom_node_revisions (id TEXT PRIMARY KEY, definition_id TEXT);
            CREATE TABLE runs (id TEXT PRIMARY KEY);
            CREATE TABLE node_runs (id TEXT PRIMARY KEY, run_id TEXT);
            CREATE TABLE run_events (id INTEGER PRIMARY KEY, run_id TEXT);
            CREATE TABLE artifacts (
                id TEXT PRIMARY KEY,
                document_id TEXT,
                run_id TEXT,
                owner_type TEXT,
                kind TEXT,
                relative_path TEXT UNIQUE,
                media_type TEXT,
                size_bytes INTEGER,
                sha256 TEXT,
                metadata_json TEXT,
                created_at DATETIME,
                updated_at DATETIME,
                FOREIGN KEY(document_id) REFERENCES documents(id),
                FOREIGN KEY(run_id) REFERENCES runs(id)
            );
            CREATE TABLE document_chunks (
                id TEXT PRIMARY KEY,
                document_id TEXT,
                artifact_id TEXT,
                chunk_index INTEGER,
                section_title TEXT,
                page_start INTEGER,
                page_end INTEGER,
                citation TEXT,
                text TEXT,
                embedding_json TEXT,
                metadata_json TEXT,
                created_at DATETIME,
                updated_at DATETIME,
                FOREIGN KEY(document_id) REFERENCES documents(id),
                FOREIGN KEY(artifact_id) REFERENCES artifacts(id)
            );
            """
        )
        connection.execute(
            "INSERT INTO app_settings VALUES (?, ?, ?)",
            ("schema_generation", "1", timestamp),
        )
        connection.execute(
            "INSERT INTO app_settings VALUES (?, ?, ?)",
            ("python_node_enabled", "true", timestamp),
        )
        connection.execute(
            "INSERT INTO app_settings VALUES (?, ?, ?)",
            ("agent_todo_max_items", "64", timestamp),
        )
        connection.execute(
            "INSERT INTO provider_profiles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "provider-1",
                "Provider",
                "ollama",
                "http://localhost:11434",
                None,
                None,
                "active",
                "[]",
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "document-1",
                "Paper",
                "paper.pdf",
                "application/pdf",
                "ready",
                1,
                "{}",
                timestamp,
                timestamp,
            ),
        )
        connection.execute("INSERT INTO runs VALUES (?)", ("run-1",))
        connection.execute(
            "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "artifact-1",
                "document-1",
                "run-1",
                "document",
                "source",
                "paper/source.pdf",
                "application/pdf",
                10,
                "abc",
                "{}",
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            "INSERT INTO document_chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "chunk-1",
                "document-1",
                "artifact-1",
                0,
                "Intro",
                1,
                1,
                "p. 1",
                "Preserved text",
                None,
                "{}",
                timestamp,
                timestamp,
            ),
        )
        connection.commit()
    finally:
        connection.close()
