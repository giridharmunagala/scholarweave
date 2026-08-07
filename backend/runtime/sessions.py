from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from agents import SQLiteSession, Session

from backend.agents.blueprint import SessionPolicySpec
from backend.providers.types import ResolvedAgentModel


class SdkSessionFactory:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._sessions: dict[tuple[object, ...], Session] = {}
        self._run_locks: dict[str, asyncio.Lock] = {}

    def get(
        self,
        conversation_id: str,
        _policy: SessionPolicySpec,
        _primary_model: ResolvedAgentModel,
    ) -> Session:
        key = (conversation_id,)
        existing = self._sessions.get(key)
        if existing is not None:
            return existing

        session: Session = SQLiteSession(
            conversation_id,
            self._database_path,
            sessions_table="sdk_sessions",
            messages_table="sdk_session_items",
        )
        self._sessions[key] = session
        return session

    async def clear(
        self,
        conversation_id: str,
        policy: SessionPolicySpec,
        primary_model: ResolvedAgentModel,
    ) -> None:
        await self.get(conversation_id, policy, primary_model).clear_session()

    @asynccontextmanager
    async def run_lock(self, conversation_id: str) -> AsyncIterator[None]:
        lock = self._run_locks.setdefault(conversation_id, asyncio.Lock())
        async with lock:
            yield
