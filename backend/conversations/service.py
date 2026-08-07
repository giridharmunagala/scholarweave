from __future__ import annotations

from typing import Any

from agents import TResponseInputItem

from backend.agents.blueprint import SessionPolicySpec
from backend.conversations.models import ConversationRecord
from backend.conversations.repository import ConversationRepository
from backend.providers.types import ResolvedAgentModel
from backend.runtime.serialization import to_jsonable
from backend.runtime.sessions import SdkSessionFactory


class ConversationService:
    def __init__(
        self,
        repository: ConversationRepository,
        sessions: SdkSessionFactory,
    ) -> None:
        self._repository = repository
        self._sessions = sessions

    def create(
        self,
        *,
        title: str,
        kind: str,
        agent_revision_id: str | None,
        model_reference: dict[str, Any],
        session_policy: SessionPolicySpec,
    ) -> ConversationRecord:
        return self._repository.create(
            title=title,
            kind=kind,
            agent_revision_id=agent_revision_id,
            model_reference=model_reference,
            session_policy=session_policy.model_dump(mode="json"),
        )

    def list(self, *, kind: str | None = None) -> list[ConversationRecord]:
        return self._repository.list(kind=kind)

    def get(self, conversation_id: str) -> ConversationRecord:
        return self._repository.get(conversation_id)

    async def items(
        self,
        conversation_id: str,
        primary_model: ResolvedAgentModel,
    ) -> list[dict[str, Any]]:
        record = self.get(conversation_id)
        policy = SessionPolicySpec.model_validate(record.session_policy_json)
        items = await self._sessions.get(record.id, policy, primary_model).get_items()
        return [_project_session_item(item) for item in items]

    def touch(self, conversation_id: str, preview: str) -> ConversationRecord:
        return self._repository.touch(conversation_id, preview=preview)

    async def delete(
        self,
        conversation_id: str,
        primary_model: ResolvedAgentModel,
    ) -> None:
        record = self.get(conversation_id)
        policy = SessionPolicySpec.model_validate(record.session_policy_json)
        await self._sessions.clear(record.id, policy, primary_model)
        self._repository.delete(conversation_id)


def _project_session_item(item: TResponseInputItem) -> dict[str, Any]:
    raw = to_jsonable(item)
    if not isinstance(raw, dict):
        return {
            "type": "unknown",
            "role": None,
            "text": str(raw),
            "raw": raw,
        }
    content = raw.get("content")
    text_parts: list[str] = []
    if isinstance(content, str):
        text_parts.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text_parts.append(part["text"])
    text = "\n".join(text_parts) or None
    return {
        "type": raw.get("type") or ("message" if raw.get("role") else "unknown"),
        "role": raw.get("role"),
        "text": text,
        "raw": raw,
    }
