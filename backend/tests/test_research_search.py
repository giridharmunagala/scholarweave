from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import httpx
import pytest
from ddgs.exceptions import DDGSException, RatelimitException
from ddgs.engines.duckduckgo import Duckduckgo

from backend.conversations.turns import RESEARCH_TOOL_IDS, autonomous_blueprint
from backend.prompting.registry import default_prompt_registry
from backend.bootstrap import create_services
from backend.core.config import Settings
from backend.research import AsyncRateLimiter, ResearchSearchService
from backend.agents.context import ScholarWeaveContext
from backend.tools.catalog import create_tool_catalog


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class StubDuckDuckGo:
    def __init__(self, results: list[dict[str, Any]]) -> None:
        self.results = results
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def text(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append((query, kwargs))
        return self.results


@pytest.mark.anyio
async def test_duckduckgo_returns_normalized_results(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
    )
    duckduckgo = StubDuckDuckGo(
        [
            {
                "title": "Open <b>result</b>",
                "href": "https://example.test/result",
                "body": "Useful &amp; public",
            }
        ]
    )
    service = ResearchSearchService(
        settings,
        duckduckgo_client=duckduckgo,
    )
    web = await service.search_web("open source", 1)

    assert web["results"][0] == {
        "title": "Open result",
        "url": "https://example.test/result",
        "snippet": "Useful & public",
        "engine": "duckduckgo",
        "published_at": None,
        "image_url": None,
    }
    assert web["provider"] == "duckduckgo"
    assert duckduckgo.calls == [
        (
            "open source",
            {"max_results": 1, "backend": "duckduckgo", "region": "wt-wt"},
        )
    ]


@pytest.mark.anyio
async def test_arxiv_and_wikipedia_return_direct_normalized_results(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/arxiv":
            return httpx.Response(
                200,
                text="""<?xml version="1.0" encoding="UTF-8"?>
                <feed xmlns="http://www.w3.org/2005/Atom">
                  <entry>
                    <id>https://arxiv.org/abs/2601.00001v1</id>
                    <updated>2026-01-02T00:00:00Z</updated>
                    <published>2026-01-01T00:00:00Z</published>
                    <title>  Test   Paper </title>
                    <summary> Evidence-backed abstract. </summary>
                    <author><name>Ada Researcher</name></author>
                    <link href="https://arxiv.org/abs/2601.00001v1" rel="alternate"/>
                    <link title="pdf" href="https://arxiv.org/pdf/2601.00001v1"/>
                    <category term="cs.AI"/>
                  </entry>
                </feed>""",
            )
        if request.url.path == "/wikipedia":
            return httpx.Response(
                200,
                json={
                    "query": {
                        "pages": [
                            {
                                "pageid": 42,
                                "index": 1,
                                "title": "Research",
                                "fullurl": "https://en.wikipedia.org/wiki/Research",
                                "extract": "Systematic inquiry.",
                            }
                        ]
                    }
                },
            )
        return httpx.Response(404)

    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        arxiv_api_url="https://search.test/arxiv",
        wikipedia_api_url="https://search.test/wikipedia",
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
        headers={"User-Agent": settings.search_user_agent},
    )
    service = ResearchSearchService(settings, client=client)
    try:
        arxiv = await service.search_arxiv("agent research", 1)
        wikipedia = await service.search_wikipedia("research", 1)
    finally:
        await client.aclose()

    assert arxiv["results"][0]["arxiv_id"] == "2601.00001v1"
    assert arxiv["results"][0]["authors"] == ["Ada Researcher"]
    assert arxiv["results"][0]["pdf_url"] == "https://arxiv.org/pdf/2601.00001v1"
    assert wikipedia["results"][0] == {
        "page_id": 42,
        "title": "Research",
        "url": "https://en.wikipedia.org/wiki/Research",
        "extract": "Systematic inquiry.",
    }
    assert requests[0].url.params["max_results"] == "1"
    assert requests[1].url.params["gsrlimit"] == "1"
    assert all(request.headers["user-agent"] == settings.search_user_agent for request in requests)


@pytest.mark.anyio
async def test_rate_limiter_spaces_requests() -> None:
    now = [100.0]
    delays: list[float] = []

    async def advance(delay: float) -> None:
        delays.append(delay)
        now[0] += delay

    limiter = AsyncRateLimiter(20, clock=lambda: now[0], sleep=advance)

    await limiter.acquire()
    await limiter.acquire()
    await limiter.acquire()

    assert delays == [3.0, 3.0]


@pytest.mark.anyio
async def test_duckduckgo_uses_stable_user_agent(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace")
    service = ResearchSearchService(settings)
    try:
        assert Duckduckgo.headers == {"User-Agent": "Mozilla/5.0"}
    finally:
        await service.close()


@pytest.mark.anyio
async def test_duckduckgo_rate_limits_fail_without_retry(tmp_path) -> None:
    now = [100.0]
    delays: list[float] = []

    async def advance(delay: float) -> None:
        delays.append(delay)
        now[0] += delay

    class RateLimitedDuckDuckGo:
        def __init__(self) -> None:
            self.attempts = 0

        def text(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            del query, kwargs
            self.attempts += 1
            raise RatelimitException("HTTP 429: too many requests")

    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
    )
    duckduckgo = RateLimitedDuckDuckGo()
    service = ResearchSearchService(
        settings,
        duckduckgo_client=duckduckgo,
        clock=lambda: now[0],
        sleep=advance,
    )
    try:
        with pytest.raises(
            RuntimeError,
            match="HTTP 429: too many requests.*Automatic retries are disabled",
        ):
            await service.search_web("open source", 1)
    finally:
        await service.close()

    assert duckduckgo.attempts == 1
    assert delays == []


@pytest.mark.anyio
async def test_duckduckgo_no_results_is_a_successful_empty_result(tmp_path) -> None:
    now = [100.0]
    delays: list[float] = []

    async def advance(delay: float) -> None:
        delays.append(delay)
        now[0] += delay

    class EventuallyAvailableDuckDuckGo:
        def __init__(self) -> None:
            self.attempts = 0

        def text(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            del query, kwargs
            self.attempts += 1
            raise DDGSException("No results found.")

    duckduckgo = EventuallyAvailableDuckDuckGo()
    settings = Settings(data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace")
    service = ResearchSearchService(
        settings,
        duckduckgo_client=duckduckgo,
        clock=lambda: now[0],
        sleep=advance,
    )
    try:
        result = await service.search_web("open source", 1)
    finally:
        await service.close()

    assert result == {
        "query": "open source",
        "provider": "duckduckgo",
        "results": [],
        "warning": "DuckDuckGo reported no results for this query.",
    }
    assert duckduckgo.attempts == 1
    assert delays == []


@pytest.mark.anyio
async def test_duckduckgo_non_rate_limit_errors_fail_without_retry(tmp_path) -> None:
    delays: list[float] = []

    async def record_delay(delay: float) -> None:
        delays.append(delay)

    class BrokenDuckDuckGo:
        def __init__(self) -> None:
            self.attempts = 0

        def text(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            del query, kwargs
            self.attempts += 1
            raise DDGSException("response parser failed")

    duckduckgo = BrokenDuckDuckGo()
    settings = Settings(data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace")
    service = ResearchSearchService(
        settings,
        duckduckgo_client=duckduckgo,
        sleep=record_delay,
    )
    try:
        with pytest.raises(RuntimeError, match="search request failed"):
            await service.search_web("open source", 1)
    finally:
        await service.close()

    assert duckduckgo.attempts == 1
    assert delays == []


@pytest.mark.anyio
async def test_duckduckgo_searches_are_serial_and_spaced_one_second(tmp_path) -> None:
    now = [100.0]
    delays: list[float] = []

    async def advance(delay: float) -> None:
        delays.append(delay)
        now[0] += delay

    class ConcurrencyTrackingDuckDuckGo:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0
            self.lock = threading.Lock()

        def text(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            del kwargs
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.01)
            with self.lock:
                self.active -= 1
            return [{"title": query, "href": f"https://example.test/{query}", "body": ""}]

    duckduckgo = ConcurrencyTrackingDuckDuckGo()
    settings = Settings(data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace")
    service = ResearchSearchService(
        settings,
        duckduckgo_client=duckduckgo,
        clock=lambda: now[0],
        sleep=advance,
    )
    try:
        await asyncio.gather(
            service.search_web("first", 1),
            service.search_web("second", 1),
        )
    finally:
        await service.close()

    assert duckduckgo.max_active == 1
    assert delays == [1.0]


@pytest.mark.anyio
async def test_web_search_session_budget_reuses_cached_queries(
    test_settings,
    monkeypatch,
) -> None:
    test_settings.web_search_max_requests_per_session = 2
    services = create_services(test_settings)
    calls: list[tuple[str, int]] = []

    async def search_web(query: str, limit: int) -> dict[str, Any]:
        calls.append((query, limit))
        return {
            "query": query,
            "provider": "duckduckgo",
            "results": [
                {
                    "title": f"Result {index}",
                    "url": f"https://example.test/{index}",
                    "snippet": "",
                }
                for index in range(limit)
            ],
        }

    monkeypatch.setattr(services.research_search, "search_web", search_web)
    context = ScholarWeaveContext(
        run_id="search-budget-run",
        tool_runtime=services.runs._tool_runtime,
    )
    runtime = services.runs._tool_runtime
    try:
        first = await runtime.invoke(
            "research.sources.search",
            {"provider": "web", "query": '  "broad"   research topic  '},
            context,
        )
        cached = await runtime.invoke(
            "research.sources.search",
            {"provider": "web", "query": "BROAD RESEARCH TOPIC"},
            context,
        )
        second = await runtime.invoke(
            "research.sources.search",
            {"provider": "web", "query": "specific evidence gap"},
            context,
        )
        with pytest.raises(ValueError, match="session limit was reached"):
            await runtime.invoke(
                "research.sources.search",
                {"provider": "web", "query": "unnecessary third query"},
                context,
            )
    finally:
        await services.close()

    assert calls == [
        ("broad research topic", 10),
        ("specific evidence gap", 10),
    ]
    assert first["cached"] is False
    assert first["search_budget"] == {"used": 1, "limit": 2, "remaining": 1}
    assert cached["cached"] is True
    assert len(cached["results"]) == 10
    assert cached["search_budget"] == {"used": 1, "limit": 2, "remaining": 1}
    assert second["search_budget"] == {"used": 2, "limit": 2, "remaining": 0}

def test_research_tools_are_cataloged_and_bound_to_researchers() -> None:
    assert Settings.model_fields["web_search_max_requests_per_session"].default == 100
    definitions = create_tool_catalog().definitions()
    catalog_ids = {definition.catalog_id for definition in definitions}
    expected = {catalog_id for _, catalog_id in RESEARCH_TOOL_IDS}
    assert catalog_ids == {
        *expected,
        "conversation.title.set",
        "tool.results.read",
        "research.summary.save",
        "work.plan.create",
        "work.plan.update",
        "work.plan.read",
    }

    autonomous = autonomous_blueprint({})
    autonomous_catalog_ids = {tool.catalog_id for tool in autonomous.tools}
    assert autonomous_catalog_ids == expected
    assert autonomous.agents[0].instructions == (
        default_prompt_registry().render("research") + "\n\nSelected research mode: review."
    )
    assert {agent.id for agent in autonomous.agents} == {"researcher"}
    assert {"save-note", "save-summary"}.issubset(autonomous.agents[0].tool_ids)
    assert autonomous.run.max_turns is None
