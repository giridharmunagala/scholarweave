from __future__ import annotations

import pytest

from backend.bootstrap import create_services
from backend.providers.schemas import ProviderCreate, ProviderModel
from backend.providers.types import AgentModelDefaults, ModelReference


@pytest.mark.anyio
async def test_legacy_ocr_enhancement_is_fallback_not_a_vision_default_override(test_settings):
    services = create_services(test_settings)
    try:
        vision = services.document_vision
        services.settings.ocr_llm_model = "legacy-vision"
        services.settings.default_model_references = {}
        fallback = vision.resolve_enhancement_model(
            llm_model=None, model_reference=None, agent_model_defaults=None,
        )
        assert fallback.kind == "ollama"
        assert fallback.profile_name == "Default Ollama"
        assert fallback.model == "legacy-vision"

        profile = services.providers.create(ProviderCreate(
            name="Remote vision", kind="openai_compatible", base_url="http://provider.invalid/v1",
            models=[ProviderModel(name="default-vision", capabilities={"vision"})],
        ))
        reference = ModelReference(profile.id, "default-vision")
        services.settings.default_model_references = {
            "vision": {"provider_profile_id": profile.id, "model": "default-vision"},
        }
        configured = vision.resolve_enhancement_model(
            llm_model=None, model_reference=None, agent_model_defaults=None,
        )
        assert configured.profile_id == profile.id
        assert configured.model == "default-vision"
        agent = vision.resolve_enhancement_model(
            llm_model=None, model_reference=None,
            agent_model_defaults=AgentModelDefaults(vision=ModelReference(profile.id, "agent-vision")),
        )
        assert agent.model == "agent-vision"
        explicit = vision.resolve_enhancement_model(
            llm_model="ignored-legacy-name", model_reference=reference,
            agent_model_defaults=AgentModelDefaults(vision=ModelReference(profile.id, "agent-vision")),
        )
        assert explicit.model == "default-vision"
    finally:
        await services.close()


@pytest.mark.anyio
async def test_legacy_ocr_triage_name_applies_only_to_same_provider_ollama(test_settings):
    services = create_services(test_settings)
    try:
        vision = services.document_vision
        services.settings.ocr_llm_triage_model = "small-vision"
        local = services.model_runtime.resolve("vision", model_reference=ModelReference(model="large-vision"))
        local_triage = vision.resolve_quality_model(local, None)
        assert local_triage.profile_id == local.profile_id
        assert local_triage.model == "small-vision"
        assert vision.resolve_quality_model(local, "explicit-triage").model == "explicit-triage"

        profile = services.providers.create(ProviderCreate(
            name="Remote vision", kind="openai_compatible", base_url="http://provider.invalid/v1",
            models=[ProviderModel(name="remote-vision", capabilities={"vision"})],
        ))
        remote = services.model_runtime.resolve(
            "vision", model_reference=ModelReference(profile.id, "remote-vision"),
        )
        assert vision.resolve_quality_model(remote, None) is remote
        explicit = vision.resolve_quality_model(remote, "remote-triage")
        assert explicit.profile_id == profile.id
        assert explicit.model == "remote-triage"
    finally:
        await services.close()
