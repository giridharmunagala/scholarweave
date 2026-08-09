from __future__ import annotations

from urllib.parse import parse_qs

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
        if request.url.path == "/searx/search":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "Open <b>result</b>",
                            "url": "https://example.test/result",
                            "content": "Useful &amp; public",
                            "engine": "example",
                        }
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
        searxng_base_url="https://search.test/searx",
        arxiv_api_url="https://search.test/arxiv",
        wikipedia_api_url="https://search.test/wikipedia",
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
        headers={"User-Agent": settings.search_user_agent},
    )
    service = ResearchSearchService(settings, client=client)
    try:
        web = await service.search_web("open source", 1)
        arxiv = await service.search_arxiv("agent research", 1)
        wikipedia = await service.search_wikipedia("research", 1)
    finally:
        await client.aclose()

    assert web["results"][0] == {
        "title": "Open result",
        "url": "https://example.test/result",
        "snippet": "Useful & public",
        "engine": "example",
        "published_at": None,
    }
    assert arxiv["results"][0]["arxiv_id"] == "2601.00001v1"
    assert arxiv["results"][0]["authors"] == ["Ada Researcher"]
    assert arxiv["results"][0]["pdf_url"] == "https://arxiv.org/pdf/2601.00001v1"
    assert wikipedia["results"][0]["url"] == "https://en.wikipedia.org/wiki/Research"
    assert requests[0].method == "POST"
    assert parse_qs(requests[0].content.decode()) == {
        "q": ["open source"],
        "format": ["json"],
        "categories": ["general"],
        "language": ["auto"],
    }
    assert requests[1].url.params["max_results"] == "1"
    assert requests[2].url.params["gsrlimit"] == "1"
    assert [request.method for request in requests[1:]] == ["GET", "GET"]
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
async def test_searxng_json_format_error_is_actionable(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        searxng_base_url="https://search.test",
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(403, request=request)),
    )
    service = ResearchSearchService(settings, client=client)
    try:
        with pytest.raises(RuntimeError, match="Enable 'json' in its search.formats"):
            await service.search_web("open source", 1)
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
    assert "Recursively refine queries" in autonomous.agents[0].instructions
    assert autonomous.run.max_turns == 50
