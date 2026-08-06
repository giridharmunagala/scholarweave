from __future__ import annotations

import hashlib
import inspect
from typing import Any

from agents import OpenAIChatCompletionsModel, OpenAIResponsesModel
from openai import AsyncOpenAI

from backend.providers.runtime import ModelRuntime, ResolvedModel
from backend.providers.types import ModelReference, ResolvedAgentModel


class SdkClientPool:
    """Owns provider clients for the application lifespan."""

    def __init__(self, runtime: ModelRuntime) -> None:
        self._runtime = runtime
        self._clients: dict[tuple[str, str, str, str], Any] = {}

    def get(self, resolved: ResolvedModel) -> AsyncOpenAI:
        credential_fingerprint = hashlib.sha256(
            (resolved.api_key or "").encode("utf-8")
        ).hexdigest()
        key = (
            resolved.profile_id,
            resolved.kind,
            resolved.base_url,
            credential_fingerprint,
        )
        client = self._clients.get(key)
        if client is None:
            client = self._runtime.client(resolved)
            self._clients[key] = client
        return client

    def invalidate_profile(self, profile_id: str) -> None:
        stale = [key for key in self._clients if key[0] == profile_id]
        for key in stale:
            self._clients.pop(key)

    async def close(self) -> None:
        clients = tuple(self._clients.values())
        self._clients.clear()
        for client in clients:
            outcome = client.close()
            if inspect.isawaitable(outcome):
                await outcome


class ProfileModelResolver:
    def __init__(self, runtime: ModelRuntime, clients: SdkClientPool) -> None:
        self._runtime = runtime
        self._clients = clients

    def resolve_agent_model(
        self,
        reference: ModelReference,
        *,
        require_tools: bool = False,
    ) -> ResolvedAgentModel:
        resolved = self._runtime.resolve(
            "tools" if require_tools else "chat",
            model_reference=ModelReference(
                provider_profile_id=reference.provider_profile_id,
                model=reference.model,
            ),
        )
        client = self._clients.get(resolved)
        if resolved.kind == "openai":
            model = OpenAIResponsesModel(model=resolved.model, openai_client=client)
            return ResolvedAgentModel(
                model=model,
                provider_kind=resolved.kind,
                supports_responses=True,
                supports_hosted_tools=True,
                supports_parallel_tool_calls=True,
                model_name=resolved.model,
                responses_client=client,
            )
        model = OpenAIChatCompletionsModel(
            model=resolved.model,
            openai_client=client,
        )
        return ResolvedAgentModel(
            model=model,
            provider_kind=resolved.kind,
            supports_responses=False,
            supports_hosted_tools=False,
            supports_parallel_tool_calls=resolved.kind
            in {"azure_openai", "azure_foundry"},
            model_name=resolved.model,
        )
