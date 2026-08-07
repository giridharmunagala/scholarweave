"""Provider-neutral model resolution and request helpers.

Profile credentials are deliberately consumed only in this module.  Callers receive
resolved metadata and model outputs, never the persisted API key.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Literal

import anyio
from openai import AsyncOpenAI, OpenAIError
from sqlalchemy import select

from backend.core.config import Settings
from backend.observability.llm_logging import LLMCallLogger, logged_http_client
from backend.providers.errors import ProviderDiscoveryError, ProviderRuntimeError
from backend.providers.models import ProviderProfile as ProviderProfileRecord
from backend.providers.ollama import OllamaClient, OllamaError
from backend.providers.schemas import ProviderModel as ProviderModelEntry
from backend.providers.types import AgentModelDefaults, ModelReference

Capability = Literal["chat", "embedding", "vision", "tools"]
OLLAMA_PLACEHOLDER_KEY = "ollama"
OPENAI_COMPATIBLE_PLACEHOLDER_KEY = "not-required"
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    profile_id: str
    profile_name: str
    kind: str
    base_url: str
    api_key: str | None
    model: str

    @property
    def agent_base_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/v1" if self.kind == "ollama" else self.base_url


class ModelRuntime:
    def __init__(
        self,
        session_factory: Any,
        settings: Settings,
        ollama: OllamaClient,
        ollama_gpu_lock: anyio.Lock,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.ollama = ollama
        self.ollama_gpu_lock = ollama_gpu_lock
        self.llm_logger = LLMCallLogger(settings.llm_log_path)

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
        return None

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
        model_reference: ModelReference | None = None,
        agent_model_defaults: AgentModelDefaults | None = None,
    ) -> ResolvedModel:
        agent_ref = getattr(agent_model_defaults, capability, None) if agent_model_defaults else None
        app_ref = self._app_reference(capability)
        references = [
            reference
            for reference in (model_reference, agent_ref, app_ref)
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
        declared_model = next(
            (
                item
                for item in (profile.models_json or [])
                if item.get("name") == model
            ),
            None,
        )
        if declared_model is not None and not bool(declared_model.get("enabled", True)):
            raise ProviderRuntimeError(
                f"Model '{model}' is disabled for provider profile '{profile.name}'."
            )
        return ResolvedModel(
            profile_id=profile.id,
            profile_name=profile.name,
            kind=profile.kind,
            base_url=profile.base_url,
            api_key=profile.api_key,
            model=model,
        )

    def client(self, resolved: ResolvedModel) -> AsyncOpenAI:
        http_client = logged_http_client(
            self.settings,
            resolved.kind,
            self.ollama_gpu_lock if resolved.kind == "ollama" else None,
        )
        if resolved.kind == "ollama":
            return AsyncOpenAI(
                api_key=OLLAMA_PLACEHOLDER_KEY,
                base_url=resolved.agent_base_url,
                timeout=self.settings.request_timeout_seconds,
                http_client=http_client,
            )
        if not resolved.api_key and resolved.kind != "openai_compatible":
            raise ProviderRuntimeError(f"Provider profile '{resolved.profile_name}' needs an API key.")
        return AsyncOpenAI(
            api_key=resolved.api_key or OPENAI_COMPATIBLE_PLACEHOLDER_KEY,
            base_url=resolved.base_url,
            timeout=self.settings.request_timeout_seconds,
            http_client=http_client,
        )

    async def discover(self, profile: ProviderProfileRecord) -> list[ProviderModelEntry]:
        manual = [ProviderModelEntry.model_validate(item) for item in (profile.models_json or [])]
        try:
            if profile.kind == "ollama":
                client = OllamaClient(self.settings, profile.base_url)
                discovered = []
                for item in await client.list_models():
                    name = str(item.get("name") or item.get("model") or "")
                    if not name:
                        continue
                    reported = set(item.get("capabilities") or [])
                    capabilities: set[Capability] = set()
                    try:
                        details = await client.show_model(name)
                        reported.update(details.get("capabilities") or [])
                    except OllamaError as exc:
                        logger.warning(
                            "Could not inspect capabilities for Ollama model %s: %s",
                            name,
                            exc,
                        )
                    if "completion" in reported:
                        capabilities.add("chat")
                    if "tools" in reported:
                        capabilities.add("tools")
                    if "vision" in reported:
                        capabilities.add("vision")
                    if "embedding" in reported:
                        capabilities.add("embedding")
                    if not reported:
                        family = str((item.get("details") or {}).get("family") or "")
                        capabilities.add(
                            "embedding"
                            if "embed" in f"{name} {family}".lower()
                            else "chat"
                        )
                    discovered.append(
                        ProviderModelEntry(name=name, capabilities=capabilities)
                    )
            else:
                resolved = ResolvedModel(
                    profile.id,
                    profile.name,
                    profile.kind,
                    profile.base_url,
                    profile.api_key,
                    "discovery",
                )
                result = await self.client(resolved).models.list()
                discovered = [ProviderModelEntry(name=item.id, capabilities=set()) for item in result.data if item.id]
        except (OllamaError, OpenAIError, ProviderRuntimeError) as exc:
            raise ProviderDiscoveryError(
                str(exc),
                manual_models=manual,
            ) from exc
        if profile.kind == "ollama":
            manual_by_name = {entry.name: entry for entry in manual}
            merged = {
                entry.name: ProviderModelEntry(
                    name=entry.name,
                    capabilities=entry.capabilities
                    | manual_by_name.get(
                        entry.name,
                        ProviderModelEntry(name=entry.name),
                    ).capabilities,
                )
                for entry in discovered
            }
        else:
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
        request = {
            "prompt": prompt,
            "messages": messages,
            "temperature": temperature,
            "system": system,
            "format": format_,
            "images": images,
            "think": think,
        }
        if resolved.kind == "ollama":
            client = (
                self.ollama
                if self.ollama.base_url.rstrip("/") == resolved.base_url.rstrip("/")
                else OllamaClient(
                    self.settings,
                    resolved.base_url,
                    request_lock=self.ollama_gpu_lock,
                )
            )
            try:
                if messages is not None:
                    response = await client.chat(
                        resolved.model, messages, stream=on_token is not None, options=_options(temperature),
                        format_=format_, on_token=on_token
                    )
                else:
                    response = await client.generate(
                        resolved.model, prompt, stream=on_token is not None, options=_options(temperature), system=system,
                        format_=format_, images=images, think=think, on_token=on_token
                    )
            except OllamaError as exc:
                self.llm_logger.write(
                    provider=resolved.kind, model=resolved.model, operation="ollama.chat" if messages else "ollama.generate",
                    request=request, error=exc
                )
                raise
            self.llm_logger.write(
                provider=resolved.kind, model=resolved.model, operation="ollama.chat" if messages else "ollama.generate",
                request=request, response=response
            )
            return response
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
                else OllamaClient(
                    self.settings,
                    resolved.base_url,
                    request_lock=self.ollama_gpu_lock,
                )
            )
            try:
                response = await client.embed(resolved.model, inputs)
            except OllamaError as exc:
                self.llm_logger.write(
                    provider=resolved.kind, model=resolved.model, operation="ollama.embed", request={"input": inputs}, error=exc
                )
                raise
            self.llm_logger.write(
                provider=resolved.kind, model=resolved.model, operation="ollama.embed", request={"input": inputs}, response=response
            )
            return response
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
