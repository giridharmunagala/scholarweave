from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class ToolRuntime(Protocol):
    async def invoke(
        self,
        catalog_id: str,
        arguments: dict[str, Any],
        context: "ScholarWeaveContext",
        *,
        tool_call_id: str | None = None,
    ) -> Any: ...

    async def bound_tool_result(
        self,
        catalog_id: str,
        result: Any,
        context: "ScholarWeaveContext",
        *,
        max_tokens: int | None = None,
    ) -> Any: ...

    def store_context_checkpoint(
        self,
        checkpoint: dict[str, Any],
        context: "ScholarWeaveContext",
    ) -> dict[str, Any]: ...

    def store_context_history(
        self,
        items: list[dict[str, Any]],
        context: "ScholarWeaveContext",
    ) -> dict[str, Any]: ...


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
    """Local run dependencies; the harness never places this object in model input."""

    run_id: str
    tool_runtime: ToolRuntime
    conversation_id: str | None = None
    event_sink: RuntimeEventSink | None = None
    receipts: list[ToolReceipt] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    agent_assignment: str | None = None
    agent_invocation: tuple[str, str] | None = None

    async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.event_sink is not None:
            await self.event_sink.emit(event_type, payload)
