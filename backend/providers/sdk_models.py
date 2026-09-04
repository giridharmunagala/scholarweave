from __future__ import annotations

from dataclasses import replace
import hashlib
import inspect
from typing import Any

from agents import OpenAIChatCompletionsModel, OpenAIResponsesModel
from openai import AsyncOpenAI

from backend.providers.runtime import ModelRuntime, ResolvedModel
from backend.providers.types import ModelReference, ResolvedAgentModel


def _replay_same_model_reasoning(context: Any) -> bool:
    """Replay only reasoning emitted by the exact model receiving the continuation."""

    return context.reasoning.origin_model == context.model


class CompatibleChatCompletionsModel(OpenAIChatCompletionsModel):
    """Avoid a non-portable combination of tool and structured-output grammars."""

    @staticmethod
    def _compatible_request(
        model_settings: Any,
        tools: list[Any],
        output_schema: Any,
        handoffs: list[Any],
    ) -> tuple[Any, Any]:
        if (
            output_schema is None
            or output_schema.is_plain_text()
            or not (tools or handoffs)
        ):
            return model_settings, output_schema

        # Several OpenAI-compatible llama.cpp servers cannot compose a strict
        # json_schema response grammar with their tool-call grammar. Keep JSON
        # syntax constrained provider-side; the runner still validates the final
        # value against output_schema after the model returns it.
        extra_args = dict(model_settings.extra_args or {})
        extra_args["response_format"] = {"type": "json_object"}
        return replace(model_settings, extra_args=extra_args), None

    async def get_response(
        self,
        system_instructions,
        input,
        model_settings,
        tools,
        output_schema,
        handoffs,
        tracing,
        *,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    ):
        model_settings, request_output_schema = self._compatible_request(
            model_settings,
            tools,
            output_schema,
            handoffs,
        )
        return await super().get_response(
            system_instructions,
            input,
            model_settings,
            tools,
            request_output_schema,
            handoffs,
            tracing,
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            prompt=prompt,
        )

    async def stream_response(
        self,
        system_instructions,
        input,
        model_settings,
        tools,
        output_schema,
        handoffs,
        tracing,
        *,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    ):
        model_settings, request_output_schema = self._compatible_request(
            model_settings,
            tools,
            output_schema,
            handoffs,
        )
        async for event in super().stream_response(
            system_instructions,
            input,
            model_settings,
            tools,
            request_output_schema,
            handoffs,
            tracing,
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            prompt=prompt,
        ):
            yield event


class SdkClientPool:
    """Owns provider clients for the application lifespan."""

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
        # Compiled/running agents may still hold this client. Retire it from
        # future resolution now, then close it with all other clients at shutdown.
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
            "chat",
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
                context_window_tokens=resolved.context_window_tokens,
                local_inference=resolved.local_inference,
            )
        chat_model_type = (
            CompatibleChatCompletionsModel
            if resolved.kind in {"ollama", "openai_compatible"}
            else OpenAIChatCompletionsModel
        )
        model = chat_model_type(
            model=resolved.model,
            openai_client=client,
            should_replay_reasoning_content=(
                _replay_same_model_reasoning
                if resolved.preserve_thinking
                else None
            ),
            buffer_streamed_tool_calls=resolved.kind in {"ollama", "openai_compatible"},
        )
        return ResolvedAgentModel(
            model=model,
            provider_kind=resolved.kind,
            supports_responses=False,
            supports_hosted_tools=False,
            supports_parallel_tool_calls=resolved.kind
            in {"azure_openai", "azure_foundry"},
            model_name=resolved.model,
            context_window_tokens=resolved.context_window_tokens,
            local_inference=resolved.local_inference,
        )
