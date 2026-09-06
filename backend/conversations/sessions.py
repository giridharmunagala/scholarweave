"""Durable audit history and compact working context for the native harness."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from backend.agents.blueprint import SessionPolicySpec
from backend.agents.harness import ConversationItem
from backend.conversations.session_repository import SessionRepository

_MESSAGE_ROLES = {"user", "assistant", "system", "developer"}


class ConversationSession:
    def __init__(self, session_id: str, database_path: Path) -> None:
        self._session_id = session_id
        self._repository = SessionRepository(database_path)
        self._lock = asyncio.Lock()

    @property
    def session_id(self) -> str:
        return self._session_id

    async def get_items(self, limit: int | None = None) -> list[ConversationItem]:
        async with self._lock:
            items = await asyncio.to_thread(self._repository.read, self.session_id)
        return items[-limit:] if limit is not None else items

    async def get_working_items(self) -> list[ConversationItem]:
        async with self._lock:
            return await asyncio.to_thread(self._repository.read, self.session_id, working=True)

    async def add_items(self, items: list[ConversationItem]) -> None:
        if items:
            async with self._lock:
                await asyncio.to_thread(self._repository.append, self.session_id, list(items))

    async def commit_working_items(
        self,
        items: list[ConversationItem],
        working_items: list[ConversationItem] | None,
        *,
        base_cursor: int,
        commit_id: str,
        covered_item_count: int | None = None,
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._repository.append, self.session_id, items,
                working_items=working_items, base_cursor=base_cursor, commit_id=commit_id,
                covered_item_count=covered_item_count,
            )

    async def checkpoint(self) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._repository.checkpoint, self.session_id)

    async def rollback_to(self, checkpoint: int) -> None:
        async with self._lock:
            await asyncio.to_thread(self._repository.rollback, self.session_id, checkpoint)

    async def pop_item(self) -> ConversationItem | None:
        async with self._lock:
            return await asyncio.to_thread(self._repository.pop, self.session_id)

    async def clear_session(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._repository.clear, self.session_id)

    async def close(self) -> None:
        return None


class PolicySession:
    """Preserve configured audit policy without slicing exact working constraints."""

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
            effective_limit = min(effective_limit, limit) if effective_limit is not None else limit
        return items[-effective_limit:] if effective_limit is not None else items

    async def get_working_items(self) -> list[ConversationItem]:
        items = await self._session.get_working_items()
        if self._policy.messages_only:
            items = [item for item in items if item.get("role") in _MESSAGE_ROLES]
        return items

    async def add_items(self, items: list[ConversationItem]) -> None:
        if self._policy.messages_only:
            items = [item for item in items if item.get("role") in _MESSAGE_ROLES]
        await self._session.add_items(items)

    async def commit_working_items(
        self,
        items: list[ConversationItem],
        working_items: list[ConversationItem] | None,
        *,
        base_cursor: int,
        commit_id: str,
        covered_item_count: int | None = None,
    ) -> None:
        if self._policy.messages_only:
            if working_items is not None:
                working_items = [
                    item for item in working_items if item.get("role") in _MESSAGE_ROLES
                ]
            if covered_item_count is not None:
                covered_item_count = sum(
                    item.get("role") in _MESSAGE_ROLES for item in items[:covered_item_count]
                )
            items = [item for item in items if item.get("role") in _MESSAGE_ROLES]
        await self._session.commit_working_items(
            items, working_items, base_cursor=base_cursor, commit_id=commit_id,
            covered_item_count=covered_item_count,
        )

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
        if self._database_path.exists():
            SessionRepository(self._database_path).clear(session_id)

    @asynccontextmanager
    async def run_lock(self, conversation_id: str) -> AsyncIterator[None]:
        lock = self._run_locks.setdefault(conversation_id, asyncio.Lock())
        async with lock:
            yield
