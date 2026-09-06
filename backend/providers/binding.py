"""Provider clients and model bindings for the native Chat Completions harness."""

from __future__ import annotations

import hashlib
import inspect
from typing import Any

from openai import AsyncOpenAI

from backend.agents.harness import ModelBinding
from backend.providers.runtime import ModelRuntime, ResolvedModel
from backend.providers.types import ModelReference

# Providers that reliably honour the OpenAI `parallel_tool_calls` request field.
_PARALLEL_TOOL_CALL_KINDS = {"openai", "azure_openai", "azure_foundry"}


class ProviderClientPool:
    """Owns one ``AsyncOpenAI`` client per profile/credential for the app lifespan."""

    def __init__(self, runtime: ModelRuntime) -> None:
        self._runtime = runtime
        self._clients: dict[tuple[str, str, str, str], Any] = {}
        self._retired_clients: list[Any] = []

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
        # Running agents may still hold this client. Retire it from future
        # resolution now, then close it with all other clients at shutdown.
        stale = [key for key in self._clients if key[0] == profile_id]
        for key in stale:
            self._retired_clients.append(self._clients.pop(key))

    async def close(self) -> None:
        clients: list[Any] = []
        for client in (*self._clients.values(), *self._retired_clients):
            if not any(existing is client for existing in clients):
                clients.append(client)
        self._clients.clear()
        self._retired_clients.clear()
        for client in clients:
            outcome = client.close()
            if inspect.isawaitable(outcome):
                await outcome


class ProfileModelResolver:
    """Turns a blueprint model reference into a harness-ready model binding."""

    def __init__(self, runtime: ModelRuntime, clients: ProviderClientPool) -> None:
        self._runtime = runtime
        self._clients = clients

    def resolve_agent_model(
        self,
        reference: ModelReference,
        *,
        require_tools: bool = False,
    ) -> ModelBinding:
        del require_tools  # Tool support is a provider-side concern, verified separately.
        resolved = self._runtime.resolve(
            "chat",
            model_reference=ModelReference(
                provider_profile_id=reference.provider_profile_id,
                model=reference.model,
            ),
        )
        return ModelBinding(
            client=self._clients.get(resolved),
            model_name=resolved.model,
            provider_kind=resolved.kind,
            supports_parallel_tool_calls=resolved.kind in _PARALLEL_TOOL_CALL_KINDS,
            preserve_thinking=resolved.preserve_thinking,
            context_window_tokens=resolved.context_window_tokens,
            local_inference=resolved.local_inference,
            reasoning_efforts=resolved.reasoning_efforts,
        )
