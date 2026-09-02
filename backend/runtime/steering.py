from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from agents.run_config import ModelInputData

from backend.core.errors import ConflictError
from backend.runtime.context import ScholarWeaveContext

_STEERING_MARKER_PREFIX = "<!-- scholarweave-steering:"
_STEERING_MARKER_SUFFIX = " -->\n"


@dataclass(frozen=True, slots=True)
class SteeringMessage:
    id: str
    content: str

    def input_item(self) -> dict[str, str]:
        return {"role": "user", "content": self.content}

    def session_item(self) -> dict[str, str]:
        return {
            "role": "user",
            "content": (
                f"{_STEERING_MARKER_PREFIX}{self.id}{_STEERING_MARKER_SUFFIX}"
                f"{self.content}"
            ),
        }


class SteeringInbox:
    """Moves user guidance into the next model call without interrupting the active one."""

    def __init__(self) -> None:
        self._pending: list[SteeringMessage] = []
        self._replay: list[SteeringMessage] = []
        self._session: Any = None
        self._closed = False

    def bind_session(self, session: Any) -> None:
        self._session = session

    def begin_epoch(self) -> None:
        self._replay.clear()

    def queue(self, content: str) -> SteeringMessage:
        if self._closed:
            raise ConflictError("The run has already finished accepting steering messages.")
        message = SteeringMessage(id=str(uuid.uuid4()), content=content)
        self._pending.append(message)
        return message

    def restore(self, message_id: str, content: str) -> None:
        if self._closed:
            raise RuntimeError("Cannot restore steering into a closed inbox.")
        self._pending.append(SteeringMessage(id=message_id, content=content))

    async def apply(
        self,
        model_data: ModelInputData,
        context: ScholarWeaveContext,
        *,
        present_message_ids: set[str] | None = None,
    ) -> ModelInputData:
        pending = self._drain()
        if pending:
            await self.persist(pending)
            self._replay.extend(pending)
            await emit_steering_applied(context, pending)
        if not self._replay:
            return model_data
        present_message_ids = present_message_ids or set()
        return ModelInputData(
            input=[
                *model_data.input,
                *(
                    message.input_item()
                    for message in self._replay
                    if message.id not in present_message_ids
                ),
            ],
            instructions=model_data.instructions,
        )

    async def persist(self, messages: list[SteeringMessage]) -> None:
        if self._session is None:
            raise RuntimeError("Steering requires a conversation session.")
        existing = await self._session.get_items()
        persisted_ids = {
            steering_message_id(item)
            for item in existing
            if isinstance(item, dict)
        }
        new_items = [
            message.session_item()
            for message in messages
            if message.id not in persisted_ids
        ]
        if new_items:
            await self._session.add_items(new_items)

    def take_pending_or_close(self) -> list[SteeringMessage]:
        pending = self._drain()
        if not pending:
            self._closed = True
        return pending

    def close(self) -> None:
        self._closed = True
        self._pending.clear()
        self._replay.clear()

    def _drain(self) -> list[SteeringMessage]:
        pending = self._pending
        self._pending = []
        return pending


def steering_message_id(item: dict[str, Any]) -> str | None:
    content = item.get("content")
    if not isinstance(content, str) or not content.startswith(_STEERING_MARKER_PREFIX):
        return None
    marker_end = content.find(_STEERING_MARKER_SUFFIX, len(_STEERING_MARKER_PREFIX))
    if marker_end < 0:
        return None
    return content[len(_STEERING_MARKER_PREFIX) : marker_end] or None


def strip_steering_marker(content: str) -> str:
    if not content.startswith(_STEERING_MARKER_PREFIX):
        return content
    marker_end = content.find(_STEERING_MARKER_SUFFIX, len(_STEERING_MARKER_PREFIX))
    if marker_end < 0:
        return content
    return content[marker_end + len(_STEERING_MARKER_SUFFIX) :]


def strip_steering_markers(items: list[Any]) -> list[Any]:
    stripped: list[Any] = []
    changed = False
    for item in items:
        if (
            isinstance(item, dict)
            and isinstance(item.get("content"), str)
            and steering_message_id(item) is not None
        ):
            clean = dict(item)
            clean["content"] = strip_steering_marker(item["content"])
            stripped.append(clean)
            changed = True
        else:
            stripped.append(item)
    return stripped if changed else items


def steering_message_ids(items: list[Any]) -> set[str]:
    return {
        message_id
        for item in items
        if isinstance(item, dict)
        and (message_id := steering_message_id(item)) is not None
    }


async def emit_steering_applied(
    context: ScholarWeaveContext,
    messages: list[SteeringMessage],
) -> None:
    for message in messages:
        await context.emit(
            "steering.applied",
            {"message_id": message.id, "content": message.content},
        )
