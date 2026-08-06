from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from agents import OpenAIResponsesCompactionSession, SQLiteSession, Session

from backend.agents.blueprint import SessionPolicySpec
from backend.providers.types import ResolvedAgentModel
from backend.runtime.compaction import AgentHistoryCompactor, LocalCompactionSession
from backend.runtime.compaction_events import emit_compaction_event


class ObservableResponsesCompactionSession(OpenAIResponsesCompactionSession):
    def __init__(self, *args, threshold_items: int, **kwargs) -> None:
        self._scholarweave_threshold = threshold_items
        self._scholarweave_triggered = False

        def should_trigger(context) -> bool:
            triggered = (
                len(context["compaction_candidate_items"])
                >= self._scholarweave_threshold
            )
            self._scholarweave_triggered = triggered
            return triggered

        super().__init__(*args, should_trigger_compaction=should_trigger, **kwargs)

    async def run_compaction(self, args=None) -> None:
        self._scholarweave_triggered = False
        forced = bool(args and args.get("force"))
        before = len(await self.underlying_session.get_items())
        try:
            await super().run_compaction(args)
        except Exception as exc:
            if self._scholarweave_triggered or forced:
                await emit_compaction_event(
                    "compaction.failed",
                    {
                        "strategy": "openai_responses",
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
            raise
        if not self._scholarweave_triggered and not forced:
            return
        after = len(await self.underlying_session.get_items())
        await emit_compaction_event(
            "compaction.started",
            {"strategy": "openai_responses", "candidate_items": before},
        )
        await emit_compaction_event(
            "compaction.completed",
            {
                "strategy": "openai_responses",
                "previous_items": before,
                "remaining_items": after,
            },
        )


class SdkSessionFactory:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._sessions: dict[tuple[object, ...], Session] = {}
        self._run_locks: dict[str, asyncio.Lock] = {}

    def get(
        self,
        conversation_id: str,
        policy: SessionPolicySpec,
        primary_model: ResolvedAgentModel,
    ) -> Session:
        strategy = self._strategy(policy, primary_model)
        key = (
            conversation_id,
            strategy,
            policy.compaction_enabled,
            policy.compaction_threshold_items,
            policy.recent_items_to_keep,
            primary_model.provider_kind,
            primary_model.model_name,
        )
        existing = self._sessions.get(key)
        if existing is not None:
            return existing

        base = SQLiteSession(
            conversation_id,
            self._database_path,
            sessions_table="sdk_sessions",
            messages_table="sdk_session_items",
        )
        if not policy.compaction_enabled:
            session: Session = base
        elif strategy == "openai_responses":
            if primary_model.responses_client is None or primary_model.model_name is None:
                raise ValueError(
                    "OpenAI Responses compaction requires the resolved Responses client and model."
                )
            session = ObservableResponsesCompactionSession(
                conversation_id,
                base,
                client=primary_model.responses_client,
                model=primary_model.model_name,
                threshold_items=policy.compaction_threshold_items,
            )
        else:
            session = LocalCompactionSession(
                base,
                AgentHistoryCompactor(primary_model.model),
                threshold_items=policy.compaction_threshold_items,
                recent_items_to_keep=policy.recent_items_to_keep,
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

    @staticmethod
    def _strategy(
        policy: SessionPolicySpec,
        primary_model: ResolvedAgentModel,
    ) -> str:
        if policy.strategy == "auto":
            return "openai_responses" if primary_model.supports_responses else "local"
        if policy.strategy == "openai_responses" and not primary_model.supports_responses:
            raise ValueError(
                "This conversation requires OpenAI Responses compaction, but its primary "
                "model does not support Responses."
            )
        return policy.strategy
