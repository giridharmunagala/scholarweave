from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.core.models import AppSetting
from backend.core.settings_service import SettingsService, SettingsUpdate
from backend.persistence.database import create_session_factory


def test_working_context_settings_persist_and_ignore_null_updates(test_settings):
    factory = create_session_factory(test_settings)
    service = SettingsService(test_settings, factory)
    updated = service.update(SettingsUpdate(
        agent_working_context_tokens=10_000,
        agent_context_response_reserve_tokens=1_024,
        agent_context_use_model_window=False,
    ))
    assert updated.agent_working_context_tokens == 10_000
    assert updated.agent_context_response_reserve_tokens == 1_024
    assert updated.agent_context_use_model_window is False
    service.update(SettingsUpdate(
        agent_working_context_tokens=None, agent_context_use_model_window=None,
    ))
    assert test_settings.agent_working_context_tokens == 10_000
    assert test_settings.agent_context_use_model_window is False

    reloaded = test_settings.model_copy(update={
        "agent_working_context_tokens": 12_000,
        "agent_context_response_reserve_tokens": 2_048,
        "agent_context_use_model_window": True,
    })
    SettingsService(reloaded, factory).load()
    assert reloaded.agent_working_context_tokens == 10_000
    assert reloaded.agent_context_response_reserve_tokens == 1_024
    assert reloaded.agent_context_use_model_window is False


def test_legacy_small_caps_do_not_disable_automatic_model_window(test_settings):
    factory = create_session_factory(test_settings)
    with factory() as session:
        session.add_all([
            AppSetting(key="agent_working_context_tokens", value_json=12_000),
            AppSetting(key="agent_context_compaction_target_tokens", value_json=8_192),
        ])
        session.commit()
    service = SettingsService(test_settings, factory)
    service.load()
    assert service.response().agent_context_use_model_window is True
    assert test_settings.agent_working_context_tokens == 12_000
    assert test_settings.agent_context_high_water_ratio == 0.85


def test_automatic_target_is_not_limited_by_fallback_window(test_settings):
    factory = create_session_factory(test_settings)
    service = SettingsService(test_settings, factory)
    service.update(SettingsUpdate(
        agent_context_window_tokens=4096,
        agent_context_compaction_target_tokens=8192,
    ))
    assert service.response().agent_context_use_model_window is True
    service.update(SettingsUpdate(agent_context_use_model_window=False))
    assert service.response().agent_context_use_model_window is False
    reloaded = test_settings.model_copy()
    SettingsService(reloaded, factory).load()
    assert reloaded.agent_context_window_tokens == 4096


def test_working_context_settings_reject_unusable_limits():
    with pytest.raises(ValidationError):
        SettingsUpdate(agent_working_context_tokens=1)
    with pytest.raises(ValidationError):
        SettingsUpdate(agent_context_response_reserve_tokens=0)


def test_decision_parallelism_default_validation_and_persistence(test_settings):
    assert test_settings.decision_parallel_requests == 8
    factory = create_session_factory(test_settings)
    service = SettingsService(test_settings, factory)
    assert service.update(SettingsUpdate(decision_parallel_requests=2)).decision_parallel_requests == 2
    reloaded = test_settings.model_copy(update={"decision_parallel_requests": 8})
    SettingsService(reloaded, factory).load()
    assert reloaded.decision_parallel_requests == 2
    assert service.update(SettingsUpdate(decision_parallel_requests=None)).decision_parallel_requests == 2
    for value in (0, 33):
        with pytest.raises(ValidationError):
            SettingsUpdate(decision_parallel_requests=value)


@pytest.mark.parametrize("capability", ["compaction", "decision", "summary", "small_tasks"])
def test_optional_model_default_persists_and_can_be_unset(test_settings, capability):
    factory = create_session_factory(test_settings)
    service = SettingsService(test_settings, factory)
    reference = {"provider_profile_id": "local-profile", "model": "gemma4-12b"}
    response = service.update(SettingsUpdate(default_model_references={
        capability: reference, "chat": {"model": "main-model"},
    }))
    assert response.default_model_references[capability].model == "gemma4-12b"
    reloaded = test_settings.model_copy(update={"default_model_references": {}})
    SettingsService(reloaded, factory).load()
    assert reloaded.default_model_references[capability] == reference
    service.update(SettingsUpdate(default_model_references={"chat": {"model": "main-model"}}))
    SettingsService(reloaded, factory).load()
    assert capability not in reloaded.default_model_references
    assert reloaded.default_model_references["chat"]["model"] == "main-model"


def test_optional_ocr_models_can_be_cleared_repeatedly(test_settings):
    factory = create_session_factory(test_settings)
    service = SettingsService(test_settings, factory)
    service.update(SettingsUpdate(
        ocr_llm_model="legacy-vision", ocr_llm_triage_model="legacy-triage",
    ))
    for parallel in (2, 3):
        response = service.update(SettingsUpdate(
            ocr_surya_parallel=parallel, ocr_llm_model=None, ocr_llm_triage_model=None,
        ))
        assert response.ocr_surya_parallel == parallel
        assert response.ocr_llm_model is None
        assert response.ocr_llm_triage_model is None
        with factory() as session:
            assert session.get(AppSetting, "ocr_llm_model") is None
            assert session.get(AppSetting, "ocr_llm_triage_model") is None
    reloaded = test_settings.model_copy()
    SettingsService(reloaded, factory).load()
    assert reloaded.ocr_surya_parallel == 3
    assert reloaded.ocr_llm_model is None
    assert reloaded.ocr_llm_triage_model is None


def test_failed_settings_save_keeps_runtime_values(test_settings, monkeypatch):
    factory = create_session_factory(test_settings)
    service = SettingsService(test_settings, factory)
    original_parallel = test_settings.ocr_surya_parallel
    with factory() as session:
        def fail_commit():
            raise RuntimeError("Settings could not be committed")

        monkeypatch.setattr(session, "commit", fail_commit)
        monkeypatch.setattr(service, "_sessions", lambda: session)
        with pytest.raises(RuntimeError, match="Settings could not be committed"):
            service.update(SettingsUpdate(ocr_surya_parallel=original_parallel + 1))
    assert test_settings.ocr_surya_parallel == original_parallel
    with factory() as session:
        assert session.get(AppSetting, "ocr_surya_parallel") is None
