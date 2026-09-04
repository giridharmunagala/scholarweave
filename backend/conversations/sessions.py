from __future__ import annotations

import asyncio
import inspect
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from agents import SQLiteSession, Session, TResponseInputItem

from backend.agents.blueprint import SessionPolicySpec


class SdkSessionFactory:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._sessions: dict[str, SQLiteSession] = {}
        self._run_locks: dict[str, asyncio.Lock] = {}

    def get(
        self,
        conversation_id: str,
        policy: SessionPolicySpec,
        _primary_model: ResolvedAgentModel | None = None,
    ) -> Session:
        session = self._sessions.get(conversation_id)
        if session is None:
            session = SQLiteSession(
                conversation_id,
                self._database_path,
                sessions_table="sdk_sessions",
                messages_table="sdk_session_items",
            )
            self._sessions[conversation_id] = session
        if policy.history_max_items is None and not policy.messages_only:
            return session
        return _PolicySession(session, policy)

    async def clear(
        self,
        conversation_id: str,
        policy: SessionPolicySpec,
    ) -> None:
        await self.get(conversation_id, policy).clear_session()

    async def evict(self, session_id: str, *, clear: bool = False) -> None:
        session = self._sessions.pop(session_id, None)
        self._run_locks.pop(session_id, None)
        if session is None:
            if not clear:
                return
            session = SQLiteSession(
                session_id,
                self._database_path,
                sessions_table="sdk_sessions",
                messages_table="sdk_session_items",
            )
        if clear:
            await session.clear_session()
        await _close_session(session)

    async def close(self) -> None:
        sessions = tuple(self._sessions.values())
        self._sessions.clear()
        self._run_locks.clear()
        for session in sessions:
            await _close_session(session)

    def delete_persisted(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        self._run_locks.pop(session_id, None)
        if session is not None:
            outcome = session.close()
            if inspect.isawaitable(outcome):
                asyncio.get_running_loop().create_task(outcome)
        if not self._database_path.exists():
            return
        with sqlite3.connect(self._database_path) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if "sdk_session_items" in tables:
                connection.execute(
                    "DELETE FROM sdk_session_items WHERE session_id = ?",
                    (session_id,),
                )
            if "sdk_sessions" in tables:
                connection.execute(
                    "DELETE FROM sdk_sessions WHERE session_id = ?",
                    (session_id,),
                )

    @asynccontextmanager
    async def run_lock(self, conversation_id: str) -> AsyncIterator[None]:
        lock = self._run_locks.setdefault(conversation_id, asyncio.Lock())
        async with lock:
            yield


class _PolicySession:
    def __init__(self, session: Session, policy: SessionPolicySpec) -> None:
        self._session = session
        self._policy = policy

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        items = await self._session.get_items()
        if self._policy.messages_only:
            items = [
                item
                for item in items
                if isinstance(item, dict)
                and item.get("role") in {"user", "assistant", "system", "developer"}
            ]
        effective_limit = self._policy.history_max_items
        if limit is not None:
            effective_limit = (
                min(effective_limit, limit)
                if effective_limit is not None
                else limit
            )
        return items[-effective_limit:] if effective_limit is not None else items

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        if self._policy.messages_only:
            items = [
                item
                for item in items
                if isinstance(item, dict)
                and item.get("role") in {"user", "assistant", "system", "developer"}
            ]
        await self._session.add_items(items)

    async def pop_item(self) -> TResponseInputItem | None:
        return await self._session.pop_item()

    async def clear_session(self) -> None:
        await self._session.clear_session()

    async def close(self) -> None:
        close = getattr(self._session, "close", None)
        if close is not None:
            outcome = close()
            if inspect.isawaitable(outcome):
                await outcome


async def _close_session(session: SQLiteSession) -> None:
    outcome = session.close()
    if inspect.isawaitable(outcome):
        await outcome
