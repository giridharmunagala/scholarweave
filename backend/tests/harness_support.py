"""Shared helpers for exercising the native agent harness in tests."""

from __future__ import annotations

import asyncio
from typing import Any

from openai import AsyncOpenAI

from backend.agents.harness import ModelBinding
from backend.providers.types import ModelReference


def stub_binding(
    stub_provider: Any,
    *,
    model_name: str = "stub-model",
    provider_kind: str = "ollama",
    **overrides: Any,
) -> ModelBinding:
    """Bind the deterministic stub provider to the harness."""
    return ModelBinding(
        client=AsyncOpenAI(api_key="test", base_url=f"{stub_provider.base_url}/v1"),
        model_name=model_name,
        provider_kind=provider_kind,
        **overrides,
    )


class StubResolver:
    """An `AgentModelResolver` that always returns one prepared binding."""

    def __init__(self, binding: ModelBinding) -> None:
        self.binding = binding
        self.requests: list[tuple[ModelReference, bool]] = []

    def resolve_agent_model(
        self,
        reference: ModelReference,
        *,
        require_tools: bool = False,
    ) -> ModelBinding:
        self.requests.append((reference, require_tools))
        return self.binding


class _Completions:
    def __init__(self, create) -> None:
        self.create = create


class _Chat:
    def __init__(self, create) -> None:
        self.completions = _Completions(create)


class FakeClient:
    """A minimal stand-in for `AsyncOpenAI` that scripts chat completions."""

    def __init__(self, create) -> None:
        self.chat = _Chat(create)
        self.requests: list[dict[str, Any]] = []

    @classmethod
    def failing(cls, error: Exception) -> "FakeClient":
        async def create(**parameters: Any):
            raise error

        return cls(create)

    @classmethod
    def blocking(cls, started: asyncio.Event) -> "FakeClient":
        async def create(**parameters: Any):
            started.set()
            await asyncio.Event().wait()

        return cls(create)

    @classmethod
    def scripted(cls, turns: list[list[dict[str, Any]]]) -> "FakeClient":
        """Reply with one prepared chunk list per model call."""
        client: FakeClient

        async def create(**parameters: Any):
            client.requests.append(parameters)
            index = min(len(client.requests) - 1, len(turns) - 1)
            return _ChunkStream(turns[index])

        client = cls(create)
        return client


class _ChunkStream:
    def __init__(self, chunks: list[dict[str, Any]]) -> None:
        self._chunks = chunks

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for chunk in self._chunks:
            await asyncio.sleep(0)
            yield _Chunk(chunk)

    async def close(self) -> None:
        return None


class _Chunk:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def model_dump(self) -> dict[str, Any]:
        return self._payload


def text_chunks(text: str, *, usage: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Build streaming chunks for a plain assistant reply."""
    chunks: list[dict[str, Any]] = [
        {"choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
        for piece in _split(text)
    ]
    chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    if usage is not None:
        chunks.append({"choices": [], "usage": usage})
    return chunks


def tool_call_chunks(
    name: str,
    arguments: str,
    *,
    call_id: str = "call-1",
    reasoning: str | None = None,
    text: str | None = None,
) -> list[dict[str, Any]]:
    """Build streaming chunks for one tool call, optionally with reasoning or text."""
    chunks: list[dict[str, Any]] = []
    if reasoning is not None:
        chunks.append(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"reasoning_content": reasoning},
                        "finish_reason": None,
                    }
                ]
            }
        )
    if text is not None:
        chunks.extend(
            {"choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
            for piece in _split(text)
        )
    chunks.append(
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": call_id,
                                "type": "function",
                                "function": {"name": name, "arguments": arguments},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        }
    )
    chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
    return chunks


def multi_tool_call_chunks(
    calls: list[tuple[str, str]],
    *,
    text: str | None = None,
    usage: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build streaming chunks for several tool calls announced in one delta."""
    chunks: list[dict[str, Any]] = []
    if text is not None:
        chunks.extend(
            {"choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
            for piece in _split(text)
        )
    chunks.append(
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": index,
                                "id": f"call-{index}",
                                "type": "function",
                                "function": {"name": name, "arguments": arguments},
                            }
                            for index, (name, arguments) in enumerate(calls)
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        }
    )
    chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
    if usage is not None:
        chunks.append({"choices": [], "usage": usage})
    return chunks


def tool_call_fragments(fragments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Wrap raw ``tool_calls`` fragments as one streamed response."""
    return [
        *(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"tool_calls": [fragment]},
                        "finish_reason": None,
                    }
                ]
            }
            for fragment in fragments
        ),
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]


def _split(text: str, size: int = 12) -> list[str]:
    return [text[start : start + size] for start in range(0, len(text), size)] or [""]
