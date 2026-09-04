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
    """Unwrap SDK and nested-agent context wrappers without depending on SDK internals."""

    current = value
    seen: set[int] = set()
    for _ in range(8):
        if isinstance(current, ScholarWeaveContext):
            return current
        marker = id(current)
        if marker in seen:
            break
        seen.add(marker)
        current = getattr(current, "context", None)
        if current is None:
            break
    raise TypeError("A ScholarWeaveContext could not be found in the SDK context wrapper.")
