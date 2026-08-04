"""Provider-neutral model resolution and request helpers.

Profile credentials are deliberately consumed only in this module.  Callers receive
resolved metadata and model outputs, never the persisted API key.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from openai import AsyncAzureOpenAI, AsyncOpenAI
from sqlalchemy import select

from backend.config import Settings
from backend.models import ProviderProfile as ProviderProfileRecord
from backend.ollama import OllamaClient
from backend.schemas import ModelReference, ProviderModelEntry, WorkflowModelDefaults

Capability = Literal["chat", "embedding", "vision", "tools"]
OLLAMA_PLACEHOLDER_KEY = "ollama"


class ProviderRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    profile_id: str
    profile_name: str
    kind: str
    base_url: str
    api_version: str | None
    api_key: str | None
    model: str

    @property
    def agent_base_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/v1" if self.kind == "ollama" else self.base_url


class ModelRuntime:
    def __init__(self, session_factory: Any, settings: Settings, ollama: OllamaClient) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.ollama = ollama

    def profiles(self, *, include_archived: bool = False) -> list[ProviderProfileRecord]:
        with self.session_factory() as session:
            stmt = select(ProviderProfileRecord).order_by(ProviderProfileRecord.created_at.asc())
            if not include_archived:
                stmt = stmt.where(ProviderProfileRecord.state == "active")
            return list(session.scalars(stmt))

    def profile(self, profile_id: str, *, include_archived: bool = False) -> ProviderProfileRecord:
        with self.session_factory() as session:
            profile = session.get(ProviderProfileRecord, profile_id)
            if profile is None:
                raise ProviderRuntimeError("Provider profile was not found.")
            if profile.state != "active" and not include_archived:
                raise ProviderRuntimeError("Provider profile is archived.")
            return profile

    def _app_reference(self, capability: Capability) -> ModelReference | None:
        raw = self.settings.default_model_references.get(capability)
        if raw:
            return ModelReference.model_validate(raw)
        legacy = (
            self.settings.default_embedding_model
            if capability == "embedding"
            else self.settings.default_generation_model
        )
        return ModelReference(model=legacy) if legacy else None

    def _fallback_profile(self) -> ProviderProfileRecord:
        profiles = self.profiles()
        default = next((item for item in profiles if item.name == "Default Ollama"), None)
        if default:
            return default
        if not profiles:
            raise ProviderRuntimeError("No active provider profiles are configured.")
        return profiles[0]

    def resolve(
        self,
        capability: Capability,
        *,
        node_reference: ModelReference | None = None,
        workflow_defaults: WorkflowModelDefaults | None = None,
    ) -> ResolvedModel:
        workflow_ref = getattr(workflow_defaults, capability, None) if workflow_defaults else None
        app_ref = self._app_reference(capability)
        references = [
            reference
            for reference in (node_reference, workflow_ref, app_ref)
            if reference is not None and (reference.provider_profile_id or reference.model)
        ]
        selected = references[0] if references else None
        if selected and selected.provider_profile_id and not selected.model:
            raise ProviderRuntimeError(
                f"The selected {capability} reference specifies a provider profile but no model name."
            )
        profile_id = selected.provider_profile_id if selected else None
        if not profile_id:
            profile_id = next(
                (reference.provider_profile_id for reference in references[1:] if reference.provider_profile_id),
                None,
            )
        profile = self.profile(profile_id) if profile_id else self._fallback_profile()
        model = selected.model if selected else None
        if not model:
            raise ProviderRuntimeError(f"No {capability} model is configured.")
        return ResolvedModel(
            profile_id=profile.id,
            profile_name=profile.name,
            kind=profile.kind,
            base_url=profile.base_url,
            api_version=profile.api_version,
            api_key=profile.api_key,
            model=model,
        )

    def client(self, resolved: ResolvedModel) -> AsyncOpenAI | AsyncAzureOpenAI:
        if resolved.kind == "ollama":
            return AsyncOpenAI(
                api_key=OLLAMA_PLACEHOLDER_KEY,
                base_url=resolved.agent_base_url,
                timeout=self.settings.request_timeout_seconds,
            )
        if not resolved.api_key:
            raise ProviderRuntimeError(f"Provider profile '{resolved.profile_name}' needs an API key.")
        if resolved.kind == "azure_openai":
            if not resolved.api_version:
                raise ProviderRuntimeError("Azure OpenAI profiles require api_version.")
            return AsyncAzureOpenAI(
                api_key=resolved.api_key,
                azure_endpoint=resolved.base_url,
                api_version=resolved.api_version,
                timeout=self.settings.request_timeout_seconds,
            )
        return AsyncOpenAI(
            api_key=resolved.api_key,
            base_url=resolved.base_url,
            timeout=self.settings.request_timeout_seconds,
        )

    async def discover(self, profile: ProviderProfileRecord) -> list[ProviderModelEntry]:
        manual = [ProviderModelEntry.model_validate(item) for item in (profile.models_json or [])]
        try:
            if profile.kind == "ollama":
                discovered = [
                    ProviderModelEntry(name=str(item.get("name")), capabilities=set())
                    for item in await OllamaClient(self.settings, profile.base_url).list_models()
                    if item.get("name")
                ]
            else:
                resolved = ResolvedModel(
                    profile.id, profile.name, profile.kind, profile.base_url, profile.api_version, profile.api_key, "discovery"
                )
                result = await self.client(resolved).models.list()
                discovered = [ProviderModelEntry(name=item.id, capabilities=set()) for item in result.data if item.id]
        except Exception as exc:  # callers retain manual entries on best-effort failures
            setattr(exc, "manual_models", manual)
            raise
        merged = {entry.name: entry for entry in discovered}
        merged.update({entry.name: entry for entry in manual})
        return list(merged.values())

    async def generate(
        self,
        resolved: ResolvedModel,
        prompt: str,
        *,
        messages: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        system: str | None = None,
        format_: dict[str, Any] | str | None = None,
        images: list[str] | None = None,
        image_media_type: str = "image/png",
        think: bool | str | None = None,
        on_token: Any = None,
    ) -> dict[str, Any]:
        if resolved.kind == "ollama":
            client = (
                self.ollama
                if self.ollama.base_url.rstrip("/") == resolved.base_url.rstrip("/")
                else OllamaClient(self.settings, resolved.base_url)
            )
            if messages is not None:
                return await client.chat(
                    resolved.model, messages, stream=on_token is not None, options=_options(temperature),
                    format_=format_, on_token=on_token
                )
            return await client.generate(
                resolved.model, prompt, stream=on_token is not None, options=_options(temperature), system=system,
                format_=format_, images=images, think=think, on_token=on_token
            )
        payload = list(messages or [])
        if not payload:
            if system:
                payload.append({"role": "system", "content": system})
            content: Any = prompt
            if images:
                content = [
                    {"type": "text", "text": prompt},
                    *[
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{image_media_type};base64,{image}"},
                        }
                        for image in images
                    ],
                ]
            payload.append({"role": "user", "content": content})
        kwargs: dict[str, Any] = {"model": resolved.model, "messages": payload, "temperature": temperature}
        if format_ is not None:
            kwargs["response_format"] = _openai_response_format(format_)
        response = await self.client(resolved).chat.completions.create(**kwargs)
        text = response.choices[0].message.content or ""
        if on_token and text:
            await on_token(text)
        return {"message": {"content": text}, "response": text, "raw": response.model_dump(mode="json")}

    async def embed(self, resolved: ResolvedModel, inputs: str | list[str]) -> list[list[float]]:
        if resolved.kind == "ollama":
            client = (
                self.ollama
                if self.ollama.base_url.rstrip("/") == resolved.base_url.rstrip("/")
                else OllamaClient(self.settings, resolved.base_url)
            )
            return await client.embed(resolved.model, inputs)
        response = await self.client(resolved).embeddings.create(model=resolved.model, input=inputs)
        return [item.embedding for item in response.data]


def _options(temperature: float | None) -> dict[str, Any] | None:
    return {"temperature": temperature} if temperature is not None else None


def _openai_response_format(format_: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(format_, dict) and format_.get("type") in {"json_object", "json_schema", "text"}:
        return format_
    if isinstance(format_, dict):
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "structured_output",
                "schema": format_,
                "strict": True,
            },
        }
    if format_ == "json":
        return {"type": "json_object"}
    raise ProviderRuntimeError(f"Unsupported OpenAI response format '{format_}'.")
