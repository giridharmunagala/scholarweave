from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.core.settings_service import SettingsService, SettingsUpdate
from backend.persistence.database import create_session_factory


def test_working_context_settings_persist_and_ignore_null_updates(test_settings):
    factory = create_session_factory(test_settings)
    service = SettingsService(test_settings, factory)
    updated = service.update(SettingsUpdate(
        agent_working_context_tokens=10_000,
        agent_context_response_reserve_tokens=1_024,
    ))
    assert updated.agent_working_context_tokens == 10_000
    assert updated.agent_context_response_reserve_tokens == 1_024
    service.update(SettingsUpdate(agent_working_context_tokens=None))
    assert test_settings.agent_working_context_tokens == 10_000

    reloaded = test_settings.model_copy(update={
        "agent_working_context_tokens": 12_000,
        "agent_context_response_reserve_tokens": 2_048,
    })
    SettingsService(reloaded, factory).load()
    assert reloaded.agent_working_context_tokens == 10_000
    assert reloaded.agent_context_response_reserve_tokens == 1_024


def test_working_context_settings_reject_unusable_limits():
    with pytest.raises(ValidationError):
        SettingsUpdate(agent_working_context_tokens=1)
    with pytest.raises(ValidationError):
        SettingsUpdate(agent_context_response_reserve_tokens=0)
