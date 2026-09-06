from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from backend.agents.context import ScholarWeaveContext
from backend.agents.harness import ToolInvocation
from backend.tools.failures import (
    classify_tool_error,
    consume_tool_failure,
    recoverable_tool_invoker,
    restore_tool_failure_state_from_attempts,
    serialize_tool_failure_state,
    tool_enabled_after_failures,
)
from backend.tools.policy import ToolInputError, operation_policy, retry_delay


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.parametrize(
    ("catalog_id", "action", "safe_retry", "mutating"),
    [
        ("research.paper.read", "pages", True, False),
        ("research.paper.read", "search", True, False),
        ("research.paper.read", "prepare", False, True),
        ("research.summary.read", "inspect", True, False),
        ("research.summary.read", "pages", False, True),
        ("research.summary.checkpoint", "read", True, False),
        ("research.summary.checkpoint", "append", False, True),
        ("research.notes.save", None, False, True),
        ("research.sources.acquire", None, False, True),
        ("research.sources.search", None, True, False),
        ("unknown.read", None, False, True),
    ],
)
def test_operation_safety_depends_on_action(catalog_id, action, safe_retry, mutating):
    policy = operation_policy(catalog_id, {"action": action})
    assert policy.safe_retry is safe_retry
    assert policy.mutating is mutating


@pytest.mark.parametrize(
    ("error", "category", "transient"),
    [
        (FileNotFoundError("missing"), "not_found", False),
        (PermissionError("read only"), "access_denied", False),
        (OSError("disk full"), "tool_error", False),
        (httpx.ConnectError("offline"), "transport", True),
        (httpx.ReadTimeout("slow"), "timeout", True),
        (ToolInputError("bad arguments"), "invalid_input", False),
    ],
)
def test_error_categories_do_not_retry_permanent_file_errors(error, category, transient):
    assert classify_tool_error(error) == (category, transient)


def test_retry_after_is_not_shortened():
    response = httpx.Response(
        429, headers={"Retry-After": "120"}, request=httpx.Request("GET", "http://localhost/")
    )
    error = httpx.HTTPStatusError("rate limit", request=response.request, response=response)
    assert retry_delay(error, 1) == 120
    assert classify_tool_error(error) == ("rate_limited", True)
    response.headers["Retry-After"] = "Sun, 06 Sep 2026 09:00:00 GMT"
    now = datetime(2026, 9, 6, 8, 59, tzinfo=timezone.utc)
    assert retry_delay(error, 1, now=now) == 60
    response.headers["Retry-After"] = "invalid"
    assert 0.25 <= retry_delay(error, 1) <= 0.5


class _Runtime:
    async def invoke(self, catalog_id, arguments, context, *, tool_call_id=None):
        raise NotImplementedError


def _invocation():
    return ToolInvocation(
        context=ScholarWeaveContext(run_id="test", tool_runtime=_Runtime()),
        tool_call_id="call",
        tool_name="read_research_paper",
        agent_name="researcher",
    )


@pytest.mark.anyio
async def test_bad_source_does_not_disable_reading_other_papers():
    calls = []

    async def read(invocation, raw):
        arguments = json.loads(raw)
        calls.append(arguments["document_id"])
        if arguments["document_id"] == "bad":
            raise FileNotFoundError("Paper missing")
        return {"text": "evidence"}

    invocation = _invocation()
    invoke = recoverable_tool_invoker("read_research_paper", read, catalog_id="research.paper.read")
    bad = json.dumps({"document_id": "bad", "action": "pages"})
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await invoke(invocation, bad)
    assert tool_enabled_after_failures("research.paper.read")(invocation.context)
    with pytest.raises(RuntimeError, match="source is paused"):
        await invoke(invocation, bad)
    result = await invoke(invocation, json.dumps({"document_id": "good", "action": "pages"}))
    assert result == {"text": "evidence"}
    assert calls == ["bad", "bad", "bad", "good"]


@pytest.mark.anyio
async def test_pre_dispatch_validation_has_known_outcome_and_does_not_trip_breaker():
    async def invalid(invocation, raw):
        raise ToolInputError("Required field is missing")

    invocation = _invocation()
    invoke = recoverable_tool_invoker("save_research_note", invalid, catalog_id="research.notes.save")
    for _ in range(4):
        with pytest.raises(RuntimeError) as error:
            await invoke(invocation, "{}")
        payload = json.loads(str(error.value))
        assert payload["status"] == "error"
        assert payload["unknown_outcome"] is False
        assert payload["retryable"] is False
    failure = consume_tool_failure(invocation.context, "call")
    assert failure["category"] == "invalid_input"
    assert serialize_tool_failure_state(invocation.context.metadata) == {"counts": {}, "disabled": []}


@pytest.mark.anyio
async def test_prepare_failure_never_advises_blind_write_retry():
    async def prepare(invocation, raw):
        raise httpx.ReadTimeout("preparation timed out")

    invocation = _invocation()
    invoke = recoverable_tool_invoker("read_research_paper", prepare, catalog_id="research.paper.read")
    with pytest.raises(RuntimeError) as error:
        await invoke(invocation, '{"document_id":"paper","action":"prepare"}')
    payload = json.loads(str(error.value))
    assert payload["retryable"] is False
    assert payload["unknown_outcome"] is True
    assert "Do not retry this write" in payload["next_action"]


def test_recovery_restores_source_scoped_failure_limit():
    attempts = [
        SimpleNamespace(
            catalog_id="research.paper.read",
            tool_call_id=f"call-{index}",
            arguments_json={"document_id": "bad", "action": "pages"},
            status="failed",
            failure_category="not_found",
        )
        for index in range(3)
    ]
    metadata = {}
    restore_tool_failure_state_from_attempts(metadata, attempts)
    disabled = serialize_tool_failure_state(metadata)["disabled"]
    assert len(disabled) == 1
    assert disabled[0].startswith("research.paper.read::")
    assert "research.paper.read" not in disabled
