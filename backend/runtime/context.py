from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class ToolRuntime(Protocol):
    async def invoke(
        self,
        catalog_id: str,
        arguments: dict[str, Any],
        context: "ScholarWeaveContext",
    ) -> Any: ...


class RuntimeEventSink(Protocol):
    async def emit(self, event_type: str, payload: dict[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class ToolReceipt:
    kind: str
    title: str
    description: str = ""
    href: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ScholarWeaveContext:
    """Local run dependencies; the SDK never places this object in model input."""

    run_id: str
    tool_runtime: ToolRuntime
    conversation_id: str | None = None
    event_sink: RuntimeEventSink | None = None
    receipts: list[ToolReceipt] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.event_sink is not None:
            await self.event_sink.emit(event_type, payload)


def unwrap_scholar_context(value: Any) -> ScholarWeaveContext:
    current = getattr(value, "context", value)
    while not isinstance(current, ScholarWeaveContext):
        nested = getattr(current, "context", None)
        if nested is None or nested is current:
            raise TypeError("Could not resolve the ScholarWeave context.")
        current = nested
    return current
