from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.agents.blueprint import FunctionToolSpec
from backend.autonomous.service import RESEARCH_TOOL_IDS, deep_work_blueprint, research_blueprint
from backend.bootstrap import create_services
from backend.documents.models import Document
from backend.research.sources import WebSourceUnavailable
from backend.runtime.context import ScholarWeaveContext
from backend.tools.catalog import APPLICATION_TOOLS, create_tool_catalog


class Runtime:
    def __init__(self) -> None:
        self.calls = []

    async def invoke(self, catalog_id, arguments, context):
        self.calls.append((catalog_id, arguments, context.run_id))
        return {"ok": True}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_research_catalog_builds_sdk_function_tools() -> None:
    catalog = create_tool_catalog()
    tool = catalog.build_function_tool(
        FunctionToolSpec(id="search", catalog_id="research.sources.search")
    )
    runtime = Runtime()
    context = ScholarWeaveContext(run_id="run-1", tool_runtime=runtime)

    output = await tool.on_invoke_tool(
        SimpleNamespace(context=context),
        '{"provider":"arxiv","query":"small language models"}',
    )

    assert output == {"ok": True}
    assert runtime.calls == [
        (
            "research.sources.search",
            {"provider": "arxiv", "query": "small language models"},
            "run-1",
        )
    ]
    assert tool.name == "search_research_sources"


def test_catalog_contains_only_research_tools_and_result_reader() -> None:
    expected = {catalog_id for _, catalog_id in RESEARCH_TOOL_IDS}
    assert {item[0] for item in APPLICATION_TOOLS} == {
        *expected,
        "tool.results.read",
    }
    for definition in create_tool_catalog().definitions():
        schema = definition.parameters_schema
        assert schema["additionalProperties"] is False
        assert all(
            "description" in property_schema
            for property_schema in schema["properties"].values()
        )


def test_web_capability_removes_external_tools_from_both_chat_modes() -> None:
    for blueprint in (
        research_blueprint({}, web_enabled=False),
        deep_work_blueprint({}, web_enabled=False),
    ):
        assert {
            tool.catalog_id for tool in blueprint.tools
        }.isdisjoint({"research.sources.search", "research.sources.acquire"})
        assert all(
            "search-sources" not in agent.tool_ids
            and "acquire-source" not in agent.tool_ids
            for agent in blueprint.agents
        )


@pytest.mark.anyio
async def test_fast_answer_limits_searches_and_page_acquisition(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = create_services(test_settings)

    async def search_web(query: str, limit: int):
        return {
            "query": query,
            "provider": "duckduckgo",
            "results": [
                {"title": "Allowed", "url": "https://example.com/allowed", "snippet": "Evidence"}
            ],
        }

    async def download_web_page(_arguments, _context):
        return {"source_id": "allowed-source"}

    try:
        runtime = services.runs._tool_runtime
        monkeypatch.setattr(services.research_search, "search_web", search_web)
        context = ScholarWeaveContext(
            run_id="fast-run",
            tool_runtime=runtime,
            metadata={"fast_answer": True, "web_search_limit": 1},
        )

        result = await runtime.invoke(
            "research.sources.search",
            {"provider": "web", "query": "bounded search"},
            context,
        )

        assert result["search_budget"] == {"used": 1, "limit": 1, "remaining": 0}
        with pytest.raises(ValueError, match="limit was reached"):
            await runtime.invoke(
                "research.sources.search",
                {"provider": "web", "query": "second search"},
                context,
            )
        with pytest.raises(ValueError, match="only permits web"):
            await runtime.invoke(
                "research.sources.search",
                {"provider": "arxiv", "query": "not permitted"},
                context,
            )
        with pytest.raises(ValueError, match="returned by its web search"):
            await runtime.invoke(
                "research.sources.acquire",
                {
                    "kind": "web_page",
                    "url": "https://example.com/not-in-results",
                    "title": None,
                },
                context,
            )
        monkeypatch.setattr(runtime, "_download_web_page", download_web_page)
        acquired = await runtime.invoke(
            "research.sources.acquire",
            {
                "kind": "web_page",
                "url": "https://example.com/allowed",
                "title": None,
            },
            context,
        )
        assert acquired == {"source_id": "allowed-source"}
        with pytest.raises(ValueError, match="after acquiring"):
            await runtime.invoke(
                "research.sources.search",
                {"provider": "web", "query": "after acquisition"},
                context,
            )
    finally:
        await services.close()


@pytest.mark.anyio
async def test_unavailable_web_page_is_a_successful_research_result(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = create_services(test_settings)

    async def unavailable(url: str):
        raise WebSourceUnavailable(url, "The remote page returned HTTP 403.")

    monkeypatch.setattr(services.source_downloads, "download_web_page", unavailable)
    try:
        runtime = services.runs._tool_runtime
        result = await runtime.invoke(
            "research.sources.acquire",
            {
                "kind": "web_page",
                "url": "https://example.com/blocked",
                "title": None,
            },
            ScholarWeaveContext(run_id="web-run", tool_runtime=runtime),
        )
    finally:
        await services.close()

    assert result["status"] == "unavailable"
    assert result["url"] == "https://example.com/blocked"
    assert result["reason"] == "The remote page returned HTTP 403."


@pytest.mark.anyio
async def test_research_source_tool_accepts_a_public_pdf(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = create_services(test_settings)
    calls: list[tuple[str, str | None, bool]] = []

    async def download_pdf(
        url: str,
        *,
        title: str | None = None,
        arxiv_only: bool = False,
    ):
        calls.append((url, title, arxiv_only))
        return SimpleNamespace(id="downloaded-document")

    try:
        runtime = services.runs._tool_runtime
        monkeypatch.setattr(services.source_downloads, "download_pdf", download_pdf)
        monkeypatch.setattr(runtime, "_inspect_paper", lambda _arguments, _context: {"ok": True})
        result = await runtime.invoke(
            "research.sources.acquire",
            {
                "kind": "paper",
                "url": "https://papers.example/research.pdf",
                "title": "Research paper",
            },
            ScholarWeaveContext(run_id="paper-run", tool_runtime=runtime),
        )
    finally:
        await services.close()

    assert result == {"ok": True}
    assert calls == [
        ("https://papers.example/research.pdf", "Research paper", False),
    ]


@pytest.mark.anyio
async def test_research_note_tools_create_search_read_and_append(test_settings) -> None:
    services = create_services(test_settings)
    try:
        runtime = services.runs._tool_runtime
        context = ScholarWeaveContext(run_id="note-run", tool_runtime=runtime)
        created = await runtime.invoke(
            "research.notes.save",
            {
                "target": "new_note",
                "mode": "overwrite",
                "document_id": None,
                "path": None,
                "name": "Attention ideas",
                "content": "Test grouped-query attention.",
                "tags": ["attention"],
            },
            context,
        )
        found = await runtime.invoke(
            "research.notes.search",
            {
                "query": "grouped-query",
                "kinds": ["note"],
                "tags": ["attention"],
                "limit": 10,
            },
            context,
        )
        await runtime.invoke(
            "research.notes.save",
            {
                "target": "path",
                "mode": "append",
                "document_id": None,
                "path": created["path"],
                "name": None,
                "content": "Compare paged KV caches.",
                "tags": ["attention", "systems"],
            },
            context,
        )
        read = await runtime.invoke(
            "research.notes.read",
            {"path": created["path"]},
            context,
        )

        assert found[0]["path"] == created["path"]
        assert "Compare paged KV caches." in read["content"]
        assert read["tags"] == ["attention", "systems"]
    finally:
        await services.close()


@pytest.mark.anyio
async def test_research_library_reports_unprepared_paper(test_settings) -> None:
    services = create_services(test_settings)
    try:
        document_id = "metadata-only-paper"
        with services.session_factory() as session:
            session.add(
                Document(
                    id=document_id,
                    title="Metadata Only",
                    source_filename="paper.pdf",
                    content_type="application/pdf",
                    status="ready",
                    page_count=22,
                    metadata_json={},
                )
            )
            session.commit()
        source = services.storage.write_text(
            test_settings.documents_dir,
            f"{document_id}/source/paper.pdf",
            "source",
        )
        services.documents.create_artifact_record(
            owner_type="document",
            kind="source_pdf",
            document_id=document_id,
            relative_path=source.relative_path,
            media_type="application/pdf",
            stored=source,
            storage_area="documents",
        )
        runtime = services.runs._tool_runtime
        context = ScholarWeaveContext(run_id="paper-run", tool_runtime=runtime)

        listed = await runtime.invoke(
            "research.library.search",
            {"query": None, "document_id": None, "limit": 10},
            context,
        )
        inspected = await runtime.invoke(
            "research.library.search",
            {"query": None, "document_id": document_id, "limit": 10},
            context,
        )

        assert listed[0]["readable"] is False
        assert inspected["source_available"] is True
        assert inspected["readable"] is False
        with pytest.raises(ValueError, match="Inspect and ingest"):
            await runtime.invoke(
                "research.paper.read",
                {
                    "document_id": document_id,
                    "action": "chunks",
                    "start": 0,
                    "limit": 10,
                },
                context,
            )
    finally:
        await services.close()
