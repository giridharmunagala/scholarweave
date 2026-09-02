from __future__ import annotations

from agents import Agent, AgentsException, RunConfig, Runner, function_tool
from openai import OpenAIError

from backend.providers.errors import ProviderDiscoveryError, ProviderRuntimeError
from backend.providers.ollama import OllamaError
from backend.providers.reasoning import infer_reasoning_efforts
from backend.providers.runtime import ModelRuntime
from backend.providers.repository import ProviderRepository
from backend.providers.schemas import (
    ProviderCreate,
    ProviderModel,
    ProviderModelsResponse,
    ProviderResponse,
    ProviderUpdate,
    ProviderVerifyResponse,
)
from backend.providers.sdk_models import ProfileModelResolver, SdkClientPool
from backend.providers.types import ModelReference


class ProviderService:
    def __init__(
        self,
        repository: ProviderRepository,
        runtime: ModelRuntime,
        model_resolver: ProfileModelResolver,
        clients: SdkClientPool,
    ) -> None:
        self._repository = repository
        self._runtime = runtime
        self._models = model_resolver
        self._clients = clients

    def list(self, *, include_archived: bool = False) -> list[ProviderResponse]:
        return [
            self._response(record)
            for record in self._repository.list(include_archived=include_archived)
        ]

    def get(self, profile_id: str) -> ProviderResponse:
        return self._response(self._repository.get(profile_id))

    def create(self, payload: ProviderCreate) -> ProviderResponse:
        record = self._repository.create(
            name=payload.name,
            kind=payload.kind,
            base_url=payload.base_url.rstrip("/"),
            api_key=payload.api_key or None,
            models_json=[
                self._with_inferred_reasoning(payload.kind, model).model_dump(mode="json")
                for model in payload.models
            ],
            state="active",
        )
        return self._response(record)

    def update(self, profile_id: str, payload: ProviderUpdate) -> ProviderResponse:
        values = payload.model_dump(exclude_unset=True)
        if "models" in values:
            provider_kind = values.get("kind") or self._repository.get(profile_id).kind
            values["models_json"] = [
                self._with_inferred_reasoning(
                    provider_kind,
                    ProviderModel.model_validate(model),
                ).model_dump(mode="json")
                for model in values.pop("models")
            ]
        if values.get("base_url"):
            values["base_url"] = values["base_url"].rstrip("/")
        if values.get("api_key") == "":
            values.pop("api_key")
        record = self._repository.update(profile_id, **values)
        self._clients.invalidate_profile(profile_id)
        return self._response(record)

    def archive(self, profile_id: str) -> ProviderResponse:
        record = self._repository.archive(profile_id)
        self._clients.invalidate_profile(profile_id)
        return self._response(record)

    async def transcribe(
        self,
        model_reference: ModelReference,
        *,
        filename: str,
        content: bytes,
        content_type: str,
    ) -> str:
        resolved = self._runtime.resolve("speech", model_reference=model_reference)
        return await self._runtime.transcribe(
            resolved,
            filename=filename,
            content=content,
            content_type=content_type,
        )

    async def discover(self, profile_id: str) -> ProviderModelsResponse:
        record = self._repository.get(profile_id)
        existing_models = {
            str(item["name"]): item
            for item in (record.models_json or [])
            if item.get("name")
        }
        try:
            entries = await self._runtime.discover(record)
        except ProviderDiscoveryError as exc:
            return ProviderModelsResponse(
                models=[
                    self._merge_discovered_model(record.kind, entry, existing_models)
                    for entry in exc.manual_models
                ],
                discovery_error=f"{type(exc).__name__}: {exc}",
            )
        discovered_models = [
            self._merge_discovered_model(record.kind, entry, existing_models)
            for entry in entries
        ]
        self._repository.update(
            profile_id,
            models_json=[
                entry.model_dump(mode="json")
                for entry in discovered_models
            ],
        )
        return ProviderModelsResponse(models=discovered_models)

    async def verify(
        self,
        profile_id: str,
        *,
        model_name: str | None,
    ) -> ProviderVerifyResponse:
        profile = self._repository.get(profile_id)
        selected = model_name or next(
            (
                str(item.get("name"))
                for item in profile.models_json or []
                if item.get("name")
            ),
            None,
        )
        if not selected:
            return ProviderVerifyResponse(
                provider=profile.kind,
                base_url=profile.base_url,
                model=None,
                reachable=False,
                tool_calling=False,
                detail="Choose or discover a model before verification.",
            )
        declared = next(
            (
                item
                for item in profile.models_json or []
                if item.get("name") == selected
            ),
            None,
        )
        capabilities = set((declared or {}).get("capabilities") or [])
        if "embedding" in capabilities and not capabilities.intersection({"chat", "tools"}):
            try:
                resolved = self._runtime.resolve(
                    "embedding",
                    model_reference=ModelReference(
                        provider_profile_id=profile.id,
                        model=selected,
                    ),
                )
                vectors = await self._runtime.embed(
                    resolved,
                    ["provider verification"],
                )
            except (OllamaError, OpenAIError, ProviderRuntimeError) as exc:
                return ProviderVerifyResponse(
                    provider=profile.kind,
                    base_url=profile.base_url,
                    model=selected,
                    reachable=False,
                    tool_calling=False,
                    detail=f"{type(exc).__name__}: {exc}",
                )
            return ProviderVerifyResponse(
                provider=profile.kind,
                base_url=profile.base_url,
                model=selected,
                reachable=bool(vectors and vectors[0]),
                tool_calling=False,
                detail="Provider returned an embedding vector.",
            )
        called = False

        @function_tool
        def record_colour(colour: str) -> str:
            """Record the colour from the verification request."""

            nonlocal called
            called = True
            return f"recorded {colour}"

        try:
            resolved = self._models.resolve_agent_model(
                ModelReference(
                    provider_profile_id=profile.id,
                    model=selected,
                ),
                require_tools=True,
            )
            agent = Agent(
                name="Provider verification",
                instructions=(
                    "Call record_colour exactly once with the colour named by the user, "
                    "then answer done."
                ),
                model=resolved.model,
                tools=[record_colour],
            )
            result = await Runner.run(
                agent,
                "The colour is teal.",
                max_turns=3,
                run_config=RunConfig(
                    workflow_name="provider-verification",
                    tracing_disabled=True,
                ),
            )
        except (AgentsException, OpenAIError, ProviderRuntimeError) as exc:
            return ProviderVerifyResponse(
                provider=profile.kind,
                base_url=profile.base_url,
                model=selected,
                reachable=False,
                tool_calling=False,
                detail=f"{type(exc).__name__}: {exc}",
            )
        detail = (
            "Provider answered and called the test tool."
            if called
            else f"Provider answered but ignored the tool: {str(result.final_output)[:200]}"
        )
        return ProviderVerifyResponse(
            provider=profile.kind,
            base_url=profile.base_url,
            model=selected,
            reachable=True,
            tool_calling=called,
            detail=detail,
        )

    @staticmethod
    def _response(record) -> ProviderResponse:
        return ProviderResponse(
            id=record.id,
            name=record.name,
            kind=record.kind,
            base_url=record.base_url,
            api_key_set=bool(record.api_key),
            state=record.state,
            models=[
                ProviderService._with_inferred_reasoning(
                    record.kind,
                    ProviderModel.model_validate({
                        **item,
                        "enabled": item.get(
                            "enabled",
                            record.kind != "azure_openai",
                        ),
                    }),
                )
                for item in (record.models_json or [])
            ],
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _with_inferred_reasoning(
        provider_kind: str,
        model: ProviderModel,
    ) -> ProviderModel:
        if model.reasoning_efforts is not None:
            return model
        return model.model_copy(
            update={
                "reasoning_efforts": infer_reasoning_efforts(provider_kind, model.name),
            }
        )

    @staticmethod
    def _merge_discovered_model(
        provider_kind: str,
        discovered: ProviderModel,
        existing_models: dict[str, dict],
    ) -> ProviderModel:
        existing = existing_models.get(discovered.name, {})
        reasoning_efforts = (
            existing.get("reasoning_efforts")
            if "reasoning_efforts" in existing
            else discovered.reasoning_efforts
        )
        return ProviderService._with_inferred_reasoning(
            provider_kind,
            ProviderModel(
                name=discovered.name,
                capabilities=discovered.capabilities,
                reasoning_efforts=reasoning_efforts,
                preserve_thinking=bool(
                    existing.get(
                        "preserve_thinking",
                        discovered.preserve_thinking,
                    )
                ),
                context_window_tokens=(
                    existing.get("context_window_tokens")
                    or discovered.context_window_tokens
                ),
                enabled=bool(existing.get("enabled", provider_kind != "azure_openai")),
            ),
        )
