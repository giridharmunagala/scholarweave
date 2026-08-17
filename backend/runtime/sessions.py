from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from agents import SQLiteSession, Session

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
    ) -> Session:
        existing = self._sessions.get(conversation_id)
        if existing is None:
            existing = SQLiteSession(
                conversation_id,
                self._database_path,
                sessions_table="sdk_sessions",
                messages_table="sdk_session_items",
            )
            self._sessions[conversation_id] = existing

        if policy.history_max_items is None and not policy.messages_only:
            return existing
        return PolicySession(existing, policy)

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


class PolicySession:
    """A bounded view that keeps extended-work internals out of future prompts."""

    def __init__(self, session: Session, policy: SessionPolicySpec) -> None:
        self._session = session
        self._policy = policy
        self.session_id = session.session_id
        self.session_settings = session.session_settings

    async def get_items(self, limit: int | None = None) -> list[Any]:
        items = await self._session.get_items()
        if self._policy.messages_only:
            items = [item for item in items if _message_role(item) in {"user", "assistant"}]
        limits = [
            value
            for value in (limit, self._policy.history_max_items)
            if value is not None
        ]
        return items[-min(limits):] if limits else items

    async def add_items(self, items: list[Any]) -> None:
        selected = (
            [item for item in items if _message_role(item) in {"user", "assistant"}]
            if self._policy.messages_only
            else items
        )
        if selected:
            await self._session.add_items(selected)

    async def pop_item(self) -> Any | None:
        return await self._session.pop_item()

    async def clear_session(self) -> None:
        await self._session.clear_session()


def _message_role(item: Any) -> str | None:
    if isinstance(item, dict):
        role = item.get("role")
    else:
        role = getattr(item, "role", None)
    return role if isinstance(role, str) else None
