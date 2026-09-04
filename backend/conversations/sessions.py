"""Durable conversation history for the native agent harness.

Items are stored as JSON rows in the application's SQLite database.  The tables keep
their historical names so an existing database keeps working after the cutover.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

from backend.agents.blueprint import SessionPolicySpec
from backend.agents.harness import ConversationItem

_SESSIONS_TABLE = "sdk_sessions"
_ITEMS_TABLE = "sdk_session_items"
_MESSAGE_ROLES = {"user", "assistant", "system", "developer"}


class ConversationSession:
    """A SQLite-backed, ordered list of canonical conversation items."""

    def __init__(self, session_id: str, database_path: Path) -> None:
        self._session_id = session_id
        self._database_path = database_path
        self._lock = asyncio.Lock()
        self._initialized = False

    @property
    def session_id(self) -> str:
        return self._session_id

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database_path)
        try:
            if not self._initialized:
                connection.executescript(
                    f"""
                    CREATE TABLE IF NOT EXISTS {_SESSIONS_TABLE} (
                        session_id TEXT PRIMARY KEY,
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE IF NOT EXISTS {_ITEMS_TABLE} (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        message_data TEXT NOT NULL,
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE INDEX IF NOT EXISTS idx_{_ITEMS_TABLE}_session
                        ON {_ITEMS_TABLE}(session_id, id);
                    """
                )
                self._initialized = True
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _read_items(self) -> list[ConversationItem]:
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT message_data FROM {_ITEMS_TABLE} WHERE session_id = ? ORDER BY id",
                (self._session_id,),
            ).fetchall()
        items: list[ConversationItem] = []
        for (payload,) in rows:
            try:
                value = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                items.append(value)
        return items

    def _write_items(self, items: list[ConversationItem]) -> None:
        with self._connect() as connection:
            connection.execute(
                f"INSERT OR IGNORE INTO {_SESSIONS_TABLE} (session_id) VALUES (?)",
                (self._session_id,),
            )
            connection.executemany(
                f"INSERT INTO {_ITEMS_TABLE} (session_id, message_data) VALUES (?, ?)",
                [
                    (self._session_id, json.dumps(item, ensure_ascii=False))
                    for item in items
                ],
            )

    async def get_items(self, limit: int | None = None) -> list[ConversationItem]:
        async with self._lock:
            items = await asyncio.to_thread(self._read_items)
        return items[-limit:] if limit is not None else items

    async def add_items(self, items: list[ConversationItem]) -> None:
        if not items:
            return
        async with self._lock:
            await asyncio.to_thread(self._write_items, list(items))

    async def checkpoint(self) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._checkpoint)

    def _checkpoint(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT COALESCE(MAX(id), 0) FROM {_ITEMS_TABLE} WHERE session_id = ?",
                (self._session_id,),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    async def rollback_to(self, checkpoint: int) -> None:
        async with self._lock:
            await asyncio.to_thread(self._rollback_to, checkpoint)

    def _rollback_to(self, checkpoint: int) -> None:
        with self._connect() as connection:
            connection.execute(
                f"DELETE FROM {_ITEMS_TABLE} WHERE session_id = ? AND id > ?",
                (self._session_id, checkpoint),
            )

    async def pop_item(self) -> ConversationItem | None:
        async with self._lock:
            return await asyncio.to_thread(self._pop_item)

    def _pop_item(self) -> ConversationItem | None:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT id, message_data FROM {_ITEMS_TABLE} "
                "WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (self._session_id,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(f"DELETE FROM {_ITEMS_TABLE} WHERE id = ?", (row[0],))
        try:
            value = json.loads(row[1])
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    async def clear_session(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._clear)

    def _clear(self) -> None:
        with self._connect() as connection:
            connection.execute(
                f"DELETE FROM {_ITEMS_TABLE} WHERE session_id = ?",
                (self._session_id,),
            )
            connection.execute(
                f"DELETE FROM {_SESSIONS_TABLE} WHERE session_id = ?",
                (self._session_id,),
            )

    async def close(self) -> None:
        return None


class PolicySession:
    """Applies a conversation's persisted history policy to reads and writes."""

    def __init__(self, session: ConversationSession, policy: SessionPolicySpec) -> None:
        self._session = session
        self._policy = policy

    @property
    def session_id(self) -> str:
        return self._session.session_id

    async def get_items(self, limit: int | None = None) -> list[ConversationItem]:
        items = await self._session.get_items()
        if self._policy.messages_only:
            items = [item for item in items if item.get("role") in _MESSAGE_ROLES]
        effective_limit = self._policy.history_max_items
        if limit is not None:
            effective_limit = (
                min(effective_limit, limit) if effective_limit is not None else limit
            )
        return items[-effective_limit:] if effective_limit is not None else items

    async def add_items(self, items: list[ConversationItem]) -> None:
        if self._policy.messages_only:
            items = [item for item in items if item.get("role") in _MESSAGE_ROLES]
        await self._session.add_items(items)

    async def checkpoint(self) -> int:
        return await self._session.checkpoint()

    async def rollback_to(self, checkpoint: int) -> None:
        await self._session.rollback_to(checkpoint)

    async def pop_item(self) -> ConversationItem | None:
        return await self._session.pop_item()

    async def clear_session(self) -> None:
        await self._session.clear_session()

    async def close(self) -> None:
        await self._session.close()


class ConversationSessionFactory:
    """Owns one session object per conversation plus its serialized run lock."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._sessions: dict[str, ConversationSession] = {}
        self._run_locks: dict[str, asyncio.Lock] = {}

    def get(
        self,
        conversation_id: str,
        policy: SessionPolicySpec,
        _primary_model: Any = None,
    ) -> ConversationSession | PolicySession:
        session = self._sessions.get(conversation_id)
        if session is None:
            session = ConversationSession(conversation_id, self._database_path)
            self._sessions[conversation_id] = session
        if policy.history_max_items is None and not policy.messages_only:
            return session
        return PolicySession(session, policy)

    async def clear(self, conversation_id: str, policy: SessionPolicySpec) -> None:
        await self.get(conversation_id, policy).clear_session()

    async def evict(self, session_id: str, *, clear: bool = False) -> None:
        session = self._sessions.pop(session_id, None)
        self._run_locks.pop(session_id, None)
        if session is None:
            if not clear:
                return
            session = ConversationSession(session_id, self._database_path)
        if clear:
            await session.clear_session()
        await session.close()

    async def close(self) -> None:
        sessions = tuple(self._sessions.values())
        self._sessions.clear()
        self._run_locks.clear()
        for session in sessions:
            await session.close()

    def delete_persisted(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        self._run_locks.pop(session_id, None)
        if not self._database_path.exists():
            return
        with sqlite3.connect(self._database_path) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if _ITEMS_TABLE in tables:
                connection.execute(
                    f"DELETE FROM {_ITEMS_TABLE} WHERE session_id = ?",
                    (session_id,),
                )
            if _SESSIONS_TABLE in tables:
                connection.execute(
                    f"DELETE FROM {_SESSIONS_TABLE} WHERE session_id = ?",
                    (session_id,),
                )

    @asynccontextmanager
    async def run_lock(self, conversation_id: str) -> AsyncIterator[None]:
        lock = self._run_locks.setdefault(conversation_id, asyncio.Lock())
        async with lock:
            yield
