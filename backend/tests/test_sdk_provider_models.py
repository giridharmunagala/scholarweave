from __future__ import annotations

import pytest
from agents import OpenAIChatCompletionsModel, OpenAIResponsesModel
from openai import AsyncOpenAI

from backend.providers.runtime import ResolvedModel
from backend.providers.sdk_models import ProfileModelResolver, SdkClientPool
from backend.providers.types import ModelReference


class Runtime:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.client_calls = 0
        self.capabilities: list[str] = []

    def resolve(self, capability, *, model_reference):
        self.capabilities.append(capability)
        return ResolvedModel(
            profile_id="profile",
            profile_name="Profile",
            kind=self.kind,
            base_url="https://api.openai.com/v1",
            api_key="test",
            model=model_reference.model or "test-model",
        )

    def client(self, resolved):
        self.client_calls += 1
        return AsyncOpenAI(api_key="test", base_url=resolved.base_url)


@pytest.mark.anyio
async def test_openai_profile_uses_responses_and_reuses_client() -> None:
    runtime = Runtime("openai")
    pool = SdkClientPool(runtime)  # type: ignore[arg-type]
    resolver = ProfileModelResolver(runtime, pool)  # type: ignore[arg-type]

    first = resolver.resolve_agent_model(ModelReference("profile", "gpt-4.1"))
    second = resolver.resolve_agent_model(
        ModelReference("profile", "gpt-4.1"),
        require_tools=True,
    )

    assert isinstance(first.model, OpenAIResponsesModel)
    assert isinstance(second.model, OpenAIResponsesModel)
    assert first.supports_responses is True
    assert runtime.capabilities == ["chat", "chat"]
    assert runtime.client_calls == 1
    await pool.close()


@pytest.mark.anyio
@pytest.mark.parametrize("provider_kind", ["ollama", "openai_compatible"])
async def test_local_profile_uses_buffered_chat_completions(provider_kind: str) -> None:
    runtime = Runtime(provider_kind)
    pool = SdkClientPool(runtime)  # type: ignore[arg-type]
    resolver = ProfileModelResolver(runtime, pool)  # type: ignore[arg-type]

    resolved = resolver.resolve_agent_model(ModelReference("profile", "qwen"))

    assert isinstance(resolved.model, OpenAIChatCompletionsModel)
    assert resolved.model._buffer_streamed_tool_calls is True
    assert resolved.supports_responses is False
    assert resolved.supports_hosted_tools is False
    await pool.close()
