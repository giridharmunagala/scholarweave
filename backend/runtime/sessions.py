from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from agents import SQLiteSession, Session, TResponseInputItem

from backend.agents.blueprint import SessionPolicySpec


class SdkSessionFactory:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._sessions: dict[str, Session] = {}
        self._run_locks: dict[str, asyncio.Lock] = {}

    def get(
        self,
        conversation_id: str,
        policy: SessionPolicySpec,
        _primary_model: ResolvedAgentModel | None = None,
    ) -> Session:
        key = (conversation_id,)
        session = self._sessions.get(key)
        if session is None:
            session = SQLiteSession(
                conversation_id,
                self._database_path,
                sessions_table="sdk_sessions",
                messages_table="sdk_session_items",
            )
            self._sessions[key] = session
        if policy.history_max_items is None and not policy.messages_only:
            return session
        return _PolicySession(session, policy)

    async def clear(
        self,
        conversation_id: str,
        policy: SessionPolicySpec,
    ) -> None:
        await self.get(conversation_id, policy).clear_session()

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
