from __future__ import annotations

import json
import asyncio

from openai import OpenAIError
from backend.core.errors import ConflictError, ValidationError

from backend.agents.harness import (
    AgentDefinition,
    FunctionTool,
    HarnessError,
    ModelSettings,
    RunSettings,
    ToolInvocation,
    run_agent,
)
from backend.agents.context import ScholarWeaveContext
from backend.providers.ollama import OllamaError
from backend.providers.reasoning import infer_reasoning_efforts
from backend.providers.runtime import ModelRuntime, ResolvedModel, uses_local_inference
from backend.providers.repository import ProviderRepository
from backend.providers.schemas import (
    ProviderCreate,
    ProviderModel,
    ProviderModelsResponse,
    ProviderResponse,
    ProviderUpdate,
    ProviderVerifyResponse,
    ResidencyConfirm,
    ResidencyResponse,
)
from backend.providers.binding import ProfileModelResolver, ProviderClientPool
from backend.providers.types import (
    ModelReference,
    ProviderDiscoveryError,
    ProviderRuntimeError,
)
from backend.prompting.registry import PromptRegistry

_VERIFICATION_INSTRUCTIONS = (
    "Call record_colour exactly once with the colour named by the user, then answer done."
)


class _VerificationToolRuntime:
    """A no-op tool runtime; provider verification never touches product state."""

    async def invoke(self, catalog_id, arguments, context, *, tool_call_id=None):
        raise NotImplementedError

    async def bound_tool_result(self, catalog_id, result, context, *, max_tokens=None):
        return result

    def store_context_checkpoint(self, checkpoint, context):
        return {}


class ProviderService:
    def __init__(
        self,
        repository: ProviderRepository,
        runtime: ModelRuntime,
        model_resolver: ProfileModelResolver,
        clients: ProviderClientPool,
        prompts: PromptRegistry | None = None,
    ) -> None:
        self._repository = repository
        self._runtime = runtime
        self._models = model_resolver
        self._clients = clients
        self._prompts = prompts
        self._residency_control = asyncio.Lock()

    def residency(self) -> ResidencyResponse:
        return ResidencyResponse.model_validate(self._runtime.inference_scheduler.snapshot())

    async def configure_residency(self, profile_id: str, enabled: bool) -> ResidencyResponse:
        async with self._residency_control:
            profile = self._repository.get(profile_id)
            scheduler = self._runtime.inference_scheduler
            if enabled:
                if profile.kind != "openai_compatible" or not uses_local_inference(profile.kind, profile.base_url):
                    raise ValidationError("Manual residency protection requires a local OpenAI-compatible profile.")
                if scheduler.profile_id == profile_id:
                    return self.residency()
                if scheduler.snapshot()["active_requests"] or scheduler.snapshot()["queue"]:
                    raise ConflictError("Stop active and queued local work before enabling residency protection.")
                scheduler.restore(profile_id, None, server_url=profile.base_url)
            else:
                await scheduler.disable(profile_id)
            self._save_residency(profile_id, enabled=enabled, resident_model=None)
            return self.residency()

    async def begin_residency_switch(self, profile_id: str) -> ResidencyResponse:
        async with self._residency_control:
            self._repository.get(profile_id)
            await self._runtime.inference_scheduler.begin_switch(profile_id)
            return self.residency()

    async def confirm_residency(self, profile_id: str, payload: ResidencyConfirm) -> ResidencyResponse:
        async with self._residency_control:
            profile = self._repository.get(profile_id)
            scheduler = self._runtime.inference_scheduler
            state = scheduler.snapshot()
            if state["profile_id"] != profile_id or not state["paused"] or state["active_requests"]:
                raise ConflictError("Drain and pause inference before confirming externally loaded weights.")
            # /v1/models is discovery, not a load command. The user separately attests
            # that the desired weights were loaded; catalog entries alone cannot prove it.
            resolved = ResolvedModel(profile.id, profile.name, profile.kind,
                                     profile.base_url, profile.api_key, payload.model)
            try:
                async with self._runtime.client(resolved) as client:
                    result = await client.models.list()
            except OpenAIError as exc:
                raise ValidationError("The server is not ready: /v1/models must succeed before confirmation.") from exc
            if [item.id for item in result.data] != [payload.model]:
                raise ValidationError(
                    "Manual single-model confirmation requires /v1/models to report exactly the selected model. "
                    "Catalog/router servers are not supported by this conservative mode."
                )
            reported = result.data[0].model_dump().get("status")
            readiness = reported.get("value") if isinstance(reported, dict) else reported
            if readiness is not None and readiness not in ("loaded", "ready"):
                raise ValidationError("The server reports that this model is not ready; leave inference paused.")
            self._save_residency(profile_id, enabled=True, resident_model=payload.model,
                                 session_mode=payload.session_mode)
            await scheduler.confirm(profile_id, payload.model, payload.session_mode)
            return self.residency()

    def _save_residency(self, profile_id: str, **values) -> None:
        profile = self._repository.get(profile_id)
        self._repository.update(profile_id, config_json={
            **(profile.config_json or {}), "residency": values,
        })

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
        if self._runtime.inference_scheduler.profile_id == profile_id and (
            {"kind", "base_url", "api_key"} & values.keys()
        ):
            raise ConflictError("Disable residency protection before changing the server connection.")
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
        if self._runtime.inference_scheduler.profile_id == profile_id:
            raise ConflictError("Disable residency protection before archiving this profile.")
        record = self._repository.archive(profile_id)
        self._clients.invalidate_profile(profile_id)
        return self._response(record)

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
        state = self.residency()
        if state.enabled and uses_local_inference(profile.kind, profile.base_url) and (
            state.profile_id != profile_id or not state.confirmed or state.paused
            or state.resident_model != selected
        ):
            raise ConflictError("Confirm this model's residency before running provider verification.")
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

        async def record_colour(invocation: ToolInvocation, raw_arguments: str) -> str:
            nonlocal called
            called = True
            try:
                arguments = json.loads(raw_arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}
            return f"recorded {arguments.get('colour', 'unknown')}"

        try:
            binding = self._models.resolve_agent_model(
                ModelReference(
                    provider_profile_id=profile.id,
                    model=selected,
                ),
                require_tools=True,
            )
            agent = AgentDefinition(
                id="provider-verification",
                name="Provider verification",
                instructions=(
                    self._prompts.render("provider-verification")
                    if self._prompts is not None
                    else _VERIFICATION_INSTRUCTIONS
                ),
                binding=binding,
                model_settings=ModelSettings(),
                tools=[
                    FunctionTool(
                        name="record_colour",
                        description="Record the colour from the verification request.",
                        params_json_schema={
                            "type": "object",
                            "properties": {
                                "colour": {
                                    "type": "string",
                                    "description": "The colour named by the user.",
                                }
                            },
                            "required": ["colour"],
                            "additionalProperties": False,
                        },
                        on_invoke_tool=record_colour,
                    )
                ],
            )
            result = await run_agent(
                agent,
                "The colour is teal.",
                context=ScholarWeaveContext(
                    run_id="provider-verification",
                    tool_runtime=_VerificationToolRuntime(),
                ),
                settings=RunSettings(max_turns=3, workflow_name="provider-verification"),
                max_turns=3,
            )
        except (HarnessError, OpenAIError, ProviderRuntimeError) as exc:
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
