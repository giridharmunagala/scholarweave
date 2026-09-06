from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.persistence import create_session_factory
from backend.runs.events import EventBroker, PersistedRunEventSink
from backend.runs.repository import RunRepository
from backend.utils import utcnow


def create_run(repository, conversation_id="session"):
    return repository.create(
        conversation_id=conversation_id, agent_name="Researcher",
        input_value="Research", blueprint={},
    )


def call(call_id, *, timings=None, scope="main", complete=True):
    return {
        "model_call_id": call_id,
        "context_scope": scope,
        "usage_complete": complete,
        "usage": {"input_tokens": 100, "output_tokens": 10},
        "timings": timings or {},
    }


@pytest.mark.anyio
async def test_session_history_persists_complete_spend_and_weighted_timing(test_settings):
    repository = RunRepository(create_session_factory(test_settings))
    first = create_run(repository)
    second = create_run(repository)
    child = create_run(repository, None)
    other = create_run(repository, "other-session")
    fast = {"prompt_n": 50, "prompt_ms": 100, "predicted_n": 10, "predicted_ms": 500}
    slow = {"prompt_n": 100, "prompt_ms": 1000, "predicted_n": 10, "predicted_ms": 2000}
    main = call("main", timings=fast)
    summary = call("summary", timings=slow, scope="delegate")
    for run, events in [
        (first, [
            ("model.telemetry", main),
            ("model.completed", {"usage": main["usage"]}),
            ("model.telemetry", summary),
            ("model.telemetry", summary),
            ("model.telemetry", call("compaction", scope="compaction")),
        ]),
        (second, [("model.telemetry", call("retry", complete=False))]),
        (child, [("model.telemetry", summary)]),
        (other, [("model.telemetry", call("other"))]),
    ]:
        sink = PersistedRunEventSink(run.id, repository, EventBroker())
        for kind, payload in events:
            await sink.emit(kind, payload)
        repository.complete(
            run.id, final_output="done", last_agent_name="Researcher",
            usage=repository.get_usage(run.id),
        )
    for _ in range(2):
        with TestClient(create_app(test_settings)) as client:
            response = client.get("/api/runs", params={"conversation_id": "session"})
            assert response.status_code == 200, response.text
            runs = response.json()
        assert len(runs) == 2
        performances = [run["usage"]["performance"] for run in runs]
        assert sum(run["usage"]["total_tokens"] for run in runs) == 440
        assert sum(perf["model_calls"] for perf in performances) == 4
        assert sum(perf["input_tokens"] for perf in performances) == 400
        assert sum(perf["output_tokens"] for perf in performances) == 40
        assert not all(perf["usage_complete"] for perf in performances)
        assert sum(perf["timed_prompt_tokens"] for perf in performances) == 150
        assert sum(perf["prompt_seconds"] for perf in performances) == 1.1
        assert sum(perf["timed_output_tokens"] for perf in performances) == 20
        assert sum(perf["generation_seconds"] for perf in performances) == 2.5
        assert sum(perf["prompt_timed_calls"] for perf in performances) == 2
        assert sum(perf["generation_timed_calls"] for perf in performances) == 2
        assert all(perf["rate_units"] == "tokens/s" for perf in performances)


def test_session_history_never_silently_limits_runs(test_settings):
    repository = RunRepository(create_session_factory(test_settings))
    for _ in range(105):
        run = create_run(repository)
        repository.complete(run.id, final_output="done", last_agent_name="Researcher", usage={
            "requests": 2, "input_tokens": 20, "output_tokens": 5, "total_tokens": 25,
        })
    with TestClient(create_app(test_settings)) as client:
        runs = client.get("/api/runs", params={"conversation_id": "session"}).json()
    assert len(runs) == 105
    assert sum(run["usage"]["total_tokens"] for run in runs) == 2625
    assert sum(run["usage"]["requests"] for run in runs) == 210


def test_session_history_handles_empty_sessions(test_settings):
    with TestClient(create_app(test_settings)) as client:
        response = client.get("/api/runs", params={"conversation_id": "empty"})
    assert response.status_code == 200
    assert response.json() == []


def test_automatic_retention_preserves_session_spend_until_explicit_deletion(
    test_settings, monkeypatch,
):
    app = create_app(test_settings)
    repository = RunRepository(create_session_factory(test_settings))
    session_run = create_run(repository)
    standalone = create_run(repository, None)
    usage = {"requests": 1, "input_tokens": 100, "output_tokens": 10, "total_tokens": 110}
    for run in (session_run, standalone):
        repository.complete(
            run.id, final_output="done", last_agent_name="Researcher", usage=usage,
        )
    future = utcnow() + timedelta(days=test_settings.run_retention_days + 1)
    monkeypatch.setattr("backend.runs.service.utcnow", lambda: future)
    service = app.state.services.runs
    assert service.prune_expired() == 1
    retained = service.list(conversation_id="session")
    assert [run.id for run in retained] == [session_run.id]
    assert retained[0].usage_json["total_tokens"] == 110
    assert service.clear_history() == 1
    assert service.list(conversation_id="session") == []
