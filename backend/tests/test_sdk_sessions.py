from __future__ import annotations

from pathlib import Path

import pytest
from agents import Model, ModelResponse, ModelSettings, SQLiteSession, TResponseInputItem, Usage

from backend.agents.blueprint import SessionPolicySpec
from backend.providers.types import ResolvedAgentModel
from backend.runtime.sessions import SdkSessionFactory


class NoopModel(Model):
    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools,
        output_schema,
        handoffs,
        tracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt,
    ) -> ModelResponse:
        return ModelResponse(output=[], usage=Usage(), response_id=None)

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_session_factory_uses_unmodified_sqlite_history(tmp_path: Path) -> None:
    factory = SdkSessionFactory(tmp_path / "sessions.sqlite3")
    model = ResolvedAgentModel(NoopModel(), "llama_cpp", False, False, False)
    policy = SessionPolicySpec()

    session = factory.get("conversation", policy, model)
    assert isinstance(session, SQLiteSession)

    items: list[TResponseInputItem] = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "one"},
    ]
    await session.add_items(items)

    assert await session.get_items() == items
    assert factory.get("conversation", policy, model) is session


def test_legacy_compaction_policy_is_ignored() -> None:
    policy = SessionPolicySpec.model_validate(
        {
            "strategy": "local",
            "compaction_enabled": True,
            "compaction_threshold_items": 20,
            "recent_items_to_keep": 8,
        }
    )

    assert policy == SessionPolicySpec()
