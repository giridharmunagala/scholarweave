from __future__ import annotations

import httpx
import pytest

from backend.agents.templates import starter_blueprints
from backend.autonomous.service import autonomous_blueprint
from backend.core.config import Settings
from backend.research import AsyncRateLimiter, ResearchSearchService
from backend.tools.catalog import create_tool_catalog


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_search_providers_return_normalized_cited_results(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "duckduckgo.com":
            return httpx.Response(
                200,
                text=(
                    '<link id="deep_preload_link" rel="preload" as="script" '
                    'href="https://links.duckduckgo.com/d.js?q=open%20source'
                    '&amp;vqd=4-test&amp;dp=token">'
                ),
            )
        if request.url.host == "links.duckduckgo.com":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "t": f"Open <b>result {index}</b>",
                            "u": f"https://example.test/result-{index}",
                            "a": "Useful &amp; public",
                        }
                        for index in range(12)
                    ]
                },
            )
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
        web = await service.search_web("open source")
        arxiv = await service.search_arxiv("agent research", 1)
        wikipedia = await service.search_wikipedia("research", 1)
    finally:
        await client.aclose()

    assert web["results"][0] == {
        "title": "Open result 0",
        "url": "https://example.test/result-0",
        "snippet": "Useful & public",
        "engine": "duckduckgo",
        "published_at": None,
        "image_url": None,
    }
    assert web["provider"] == "duckduckgo"
    assert len(web["results"]) == 10
    assert arxiv["results"][0]["arxiv_id"] == "2601.00001v1"
    assert arxiv["results"][0]["authors"] == ["Ada Researcher"]
    assert arxiv["results"][0]["pdf_url"] == "https://arxiv.org/pdf/2601.00001v1"
    assert wikipedia["results"][0]["url"] == "https://en.wikipedia.org/wiki/Research"
    assert requests[0].url.host == "duckduckgo.com"
    assert requests[0].url.params["q"] == "open source"
    assert requests[1].url.host == "links.duckduckgo.com"
    assert requests[1].url.params["o"] == "json"
    assert requests[2].url.params["max_results"] == "1"
    assert requests[3].url.params["gsrlimit"] == "1"
    assert [request.method for request in requests] == ["GET", "GET", "GET", "GET"]
    assert requests[0].headers["user-agent"].startswith("Mozilla/5.0")
    assert all(
        request.headers["user-agent"] == settings.search_user_agent
        for request in requests[2:]
    )
    assert service._limiters["web"]._interval == 2.0


@pytest.mark.anyio
async def test_rate_limiter_spaces_web_requests_by_two_seconds() -> None:
    now = [100.0]
    delays: list[float] = []

    async def advance(delay: float) -> None:
        delays.append(delay)
        now[0] += delay

    limiter = AsyncRateLimiter(30, clock=lambda: now[0], sleep=advance)

    await limiter.acquire()
    await limiter.acquire()
    await limiter.acquire()

    assert delays == [2.0, 2.0]


@pytest.mark.anyio
async def test_duckduckgo_error_is_actionable(tmp_path) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                text="<html>Unfortunately, bots use DuckDuckGo too.</html>",
                request=request,
            )
        ),
    )
    service = ResearchSearchService(
        Settings(data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace"),
        client=client,
    )
    try:
        with pytest.raises(RuntimeError, match="DuckDuckGo.*rate-limited"):
            await service.search_web("open source")
    finally:
        await client.aclose()


def test_research_tools_are_cataloged_and_bound_to_researchers() -> None:
    catalog_ids = {definition.catalog_id for definition in create_tool_catalog().definitions()}
    source_tools = {
        "documents.download",
        "webpage.download",
        "webpage.list",
        "webpage.read",
        "webpage.search",
        "webpage.notes.save",
    }
    assert {"web.search", "arxiv.search", "wikipedia.search", *source_tools} <= catalog_ids

    for blueprint in starter_blueprints():
        researcher = next(agent for agent in blueprint.agents if agent.id == "researcher")
        bound_catalog_ids = {
            tool.catalog_id
            for tool in blueprint.tools
            if tool.id in researcher.tool_ids and tool.kind == "function"
        }
        assert {"web.search", "arxiv.search", "wikipedia.search", *source_tools} <= bound_catalog_ids
        assert blueprint.run.max_turns == 30

    autonomous = autonomous_blueprint({})
    autonomous_catalog_ids = {tool.catalog_id for tool in autonomous.tools}
    assert {"web.search", "arxiv.search", "wikipedia.search", *source_tools} <= autonomous_catalog_ids
    assert "recursively refine queries" in autonomous.agents[0].instructions
    assert autonomous.run.max_turns == 96
