"""SQLite ownership for compact working snapshots beside canonical audit history."""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator

logger = logging.getLogger(__name__)


class SessionRepository:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection, None, None]:
        connection = sqlite3.connect(self._database_path)
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sdk_sessions (
                    session_id TEXT PRIMARY KEY,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS sdk_session_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    message_data TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_sdk_session_items_session
                    ON sdk_session_items(session_id, id);
                CREATE TABLE IF NOT EXISTS conversation_working_context (
                    session_id TEXT PRIMARY KEY,
                    cursor INTEGER NOT NULL,
                    items TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversation_context_commits (
                    session_id TEXT NOT NULL,
                    commit_id TEXT NOT NULL,
                    cursor INTEGER NOT NULL,
                    PRIMARY KEY(session_id, commit_id)
                );
                """
            )
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _decode(rows: list[Any]) -> list[dict[str, Any]]:
        items = []
        for (payload,) in rows:
            try:
                value = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                items.append(value)
        return items

    @staticmethod
    def _cursor(connection: sqlite3.Connection, session_id: str) -> int:
        return connection.execute(
            "SELECT COALESCE(MAX(id), 0) FROM sdk_session_items WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]

    def checkpoint(self, session_id: str) -> int:
        with self.connect() as connection:
            return self._cursor(connection, session_id)

    def read(self, session_id: str, *, working: bool = False) -> list[dict[str, Any]]:
        with self.connect() as connection:
            cursor = 0
            prefix: list[dict[str, Any]] = []
            if working:
                row = connection.execute(
                    "SELECT cursor, items FROM conversation_working_context WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row is not None and row[0] <= self._cursor(connection, session_id):
                    try:
                        value = json.loads(row[1])
                        if isinstance(value, list) and all(isinstance(v, dict) for v in value):
                            legacy_checkpoint = any(
                                item.get("_scholarweave_context_checkpoint") is True
                                and item.get("_scholarweave_context_policy_version") != 2
                                for item in value
                            )
                            if legacy_checkpoint:
                                logger.info("Rebuilding legacy compacted context from canonical session history.")
                            else:
                                cursor, prefix = row[0], value
                    except json.JSONDecodeError:
                        pass
            rows = connection.execute(
                "SELECT message_data FROM sdk_session_items WHERE session_id = ? AND id > ? ORDER BY id",
                (session_id, cursor),
            ).fetchall()
            return [*prefix, *self._decode(rows)]

    def append(
        self,
        session_id: str,
        items: list[dict[str, Any]],
        *,
        working_items: list[dict[str, Any]] | None = None,
        base_cursor: int | None = None,
        commit_id: str | None = None,
        covered_item_count: int | None = None,
    ) -> None:
        with self.connect() as connection:
            if commit_id is not None and connection.execute(
                "SELECT 1 FROM conversation_context_commits WHERE session_id = ? AND commit_id = ?",
                (session_id, commit_id),
            ).fetchone():
                return
            # Steering can be durably appended while the model is running. It is not
            # part of the runner's compact working list and must survive the snapshot.
            delta = []
            if working_items is not None and base_cursor is not None:
                delta = self._decode(connection.execute(
                    "SELECT message_data FROM sdk_session_items WHERE session_id = ? AND id > ? ORDER BY id",
                    (session_id, base_cursor),
                ).fetchall())
            connection.execute(
                "INSERT OR IGNORE INTO sdk_sessions (session_id) VALUES (?)", (session_id,)
            )
            covered = len(items) if covered_item_count is None else covered_item_count
            # Persist only the last prepared request as the snapshot. Its final
            # model/tool round remains an uncovered delta, not unbounded snapshot data.
            connection.executemany(
                "INSERT INTO sdk_session_items (session_id, message_data) VALUES (?, ?)",
                [(session_id, json.dumps(item, ensure_ascii=False)) for item in items[:covered]],
            )
            cursor = self._cursor(connection, session_id)
            if working_items is not None:
                connection.execute(
                    "INSERT OR REPLACE INTO conversation_working_context VALUES (?, ?, ?)",
                    (session_id, cursor, json.dumps([*working_items, *delta], ensure_ascii=False)),
                )
            connection.executemany(
                "INSERT INTO sdk_session_items (session_id, message_data) VALUES (?, ?)",
                [(session_id, json.dumps(item, ensure_ascii=False)) for item in items[covered:]],
            )
            if commit_id is not None:
                connection.execute(
                    "INSERT INTO conversation_context_commits VALUES (?, ?, ?)",
                    (session_id, commit_id, self._cursor(connection, session_id)),
                )

    def rollback(self, session_id: str, cursor: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM sdk_session_items WHERE session_id = ? AND id > ?", (session_id, cursor)
            )
            self._invalidate(connection, session_id, cursor)

    @staticmethod
    def _invalidate(connection: sqlite3.Connection, session_id: str, cursor: int) -> None:
        connection.execute(
            "DELETE FROM conversation_working_context WHERE session_id = ? AND cursor > ?",
            (session_id, cursor),
        )
        connection.execute(
            "DELETE FROM conversation_context_commits WHERE session_id = ? AND cursor > ?",
            (session_id, cursor),
        )

    def pop(self, session_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id, message_data FROM sdk_session_items WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            connection.execute("DELETE FROM sdk_session_items WHERE id = ?", (row[0],))
            self._invalidate(connection, session_id, row[0] - 1)
            items = self._decode([(row[1],)])
            return items[0] if items else None

    def clear(self, session_id: str) -> None:
        with self.connect() as connection:
            for table in (
                "sdk_session_items", "sdk_sessions",
                "conversation_working_context", "conversation_context_commits",
            ):
                connection.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))
