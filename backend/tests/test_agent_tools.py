from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from backend.agents.blueprint import FunctionToolSpec, ModelReferenceSpec
from backend.conversations.turns import (
    RESEARCH_TOOL_IDS,
    deep_work_blueprint,
    research_blueprint,
    validate_paper_work_completion,
)
from backend.core.errors import ValidationError
from backend.bootstrap import create_services
from backend.documents.models import Document
from backend.research.sources import WebSourceUnavailable
from backend.agents.context import ScholarWeaveContext
from backend.tools.catalog import APPLICATION_TOOLS, create_tool_catalog
from backend.tools.runtime import (
    ApplicationToolRuntime,
    create_work_plan,
    update_work_item,
    work_plan,
)


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


def test_catalog_contains_research_and_persistence_tools() -> None:
    expected = {catalog_id for _, catalog_id in RESEARCH_TOOL_IDS}
    assert {item[0] for item in APPLICATION_TOOLS} == {
        *expected,
        "conversation.title.set",
        "tool.results.read",
        "research.summary.save",
        "work.plan.create",
        "work.plan.update",
        "work.plan.read",
    }
    assert all(hasattr(ApplicationToolRuntime, item[5]) for item in APPLICATION_TOOLS)
    for definition in create_tool_catalog().definitions():
        schema = definition.parameters_schema
        assert schema["additionalProperties"] is False
        assert all(
            "description" in property_schema
            for property_schema in schema["properties"].values()
        )


@pytest.mark.anyio
async def test_conversation_title_tool_is_first_turn_only(test_settings) -> None:
    services = create_services(test_settings)
    conversation = services.conversation_turns.create_conversation(
        title="New research",
        model_reference=ModelReferenceSpec(),
    )
    runtime = services.runs._tool_runtime
    context = ScholarWeaveContext(
        run_id="title-run",
        conversation_id=conversation.id,
        tool_runtime=runtime,
        metadata={"allow_conversation_title_update": True},
    )
    try:
        result = await runtime.invoke(
            "conversation.title.set",
            {"title": "  Evidence-Based Retrieval   Review  "},
            context,
        )

        assert result["title"] == "Evidence-Based Retrieval Review"
        assert services.conversations.get(conversation.id).title == result["title"]
        with pytest.raises(ValueError, match="already been set"):
            await runtime.invoke(
                "conversation.title.set",
                {"title": "A second title"},
                context,
            )
        with pytest.raises(ValueError, match="first turn"):
            await runtime.invoke(
                "conversation.title.set",
                {"title": "A later title"},
                ScholarWeaveContext(
                    run_id="later-run",
                    conversation_id=conversation.id,
                    tool_runtime=runtime,
                ),
            )
    finally:
        await services.close()


def test_only_main_agent_receives_title_tool_on_first_turn() -> None:
    first_research = research_blueprint({}, first_turn=True)
    later_research = research_blueprint({}, first_turn=False)
    first_deep_work = deep_work_blueprint({}, first_turn=True)

    assert "set-title" in first_research.agents[0].tool_ids
    assert "set-title" not in later_research.agents[0].tool_ids
    assert "set-title" in first_deep_work.agents[0].tool_ids
    assert "set-title" not in first_deep_work.agents[1].tool_ids


@pytest.mark.anyio
async def test_tool_results_can_be_read_across_runs_in_one_conversation(
    test_settings,
) -> None:
    services = create_services(test_settings)
    repository = services.runs._repository
    previous = repository.create(
        conversation_id="conversation-1",
        agent_name="Previous",
        input_value="previous",
        blueprint={},
    )
    active = repository.create(
        conversation_id="conversation-1",
        agent_name="Active",
        input_value="active",
        blueprint={},
    )
    unrelated = repository.create(
        conversation_id="conversation-2",
        agent_name="Unrelated",
        input_value="unrelated",
        blueprint={},
    )
    previous_result = services.storage.write_text(
        test_settings.artifacts_dir,
        f"runs/{previous.id}/tool-results/result.json",
        '{"evidence":"retained"}',
    )
    unrelated_result = services.storage.write_text(
        test_settings.artifacts_dir,
        f"runs/{unrelated.id}/tool-results/result.json",
        '{"evidence":"private"}',
    )
    context = ScholarWeaveContext(
        run_id=active.id,
        conversation_id="conversation-1",
        tool_runtime=services.runs._tool_runtime,
    )

    try:
        assert previous_result.relative_path == (
            f"runs/{previous.id}/tool-results/result.json"
        )
        result = services.runs._tool_runtime._read_tool_result(
            {"result_ref": previous_result.relative_path, "offset": 0, "limit": 100},
            context,
        )
        doubled_separators = previous_result.relative_path.replace("/", "\\\\")
        normalized = services.runs._tool_runtime._read_tool_result(
            {"result_ref": doubled_separators, "offset": 0, "limit": 100},
            context,
        )
        assert "retained" in result["content"]
        assert normalized["result_ref"] == previous_result.relative_path
        with pytest.raises(ValueError, match="active run or its conversation"):
            services.runs._tool_runtime._read_tool_result(
                {
                    "result_ref": unrelated_result.relative_path,
                    "offset": 0,
                    "limit": 100,
                },
                context,
            )
        for unsafe_ref in (
            f"runs/{active.id}/../{unrelated.id}/tool-results/result.json",
            f"/runs/{active.id}/tool-results/result.json",
            f"C:\\runs\\{active.id}\\tool-results\\result.json",
            f"\\\\server\\runs\\{active.id}\\tool-results\\result.json",
        ):
            with pytest.raises(ValueError, match="result_ref"):
                services.runs._tool_runtime._read_tool_result(
                    {"result_ref": unsafe_ref, "offset": 0, "limit": 100},
                    context,
                )
    finally:
        await services.close()


@pytest.mark.anyio
async def test_paper_chunk_reads_return_a_non_overlapping_cursor(
    test_settings,
) -> None:
    services = create_services(test_settings)
    document_id = "paged-paper"
    try:
        with services.session_factory() as session:
            session.add(
                Document(
                    id=document_id,
                    title="Paged Paper",
                    source_filename="paged.pdf",
                    content_type="application/pdf",
                    status="ready",
                    page_count=3,
                    metadata_json={},
                )
            )
            session.commit()

        services.retrieval.replace_document_chunks(
            document_id,
            [
                {
                    "text": f"Chunk {index}",
                    "page_start": index + 1,
                    "page_end": index + 1,
                    "citation": f"p.{index + 1}",
                }
                for index in range(3)
            ],
        )
        context = ScholarWeaveContext(
            run_id="chunk-run",
            tool_runtime=services.runs._tool_runtime,
        )

        result = await services.runs._tool_runtime.invoke(
            "research.paper.read",
            {
                "document_id": document_id,
                "action": "chunks",
                "start": 1,
                "limit": 1,
            },
            context,
        )

        assert result["chunk_count"] == 3
        assert result["start"] == 1
        assert result["has_more"] is True
        assert result["next_start"] == 2
        assert [chunk["chunk_index"] for chunk in result["chunks"]] == [1]
        empty_context = ScholarWeaveContext(
            run_id="empty-chunk-run",
            tool_runtime=services.runs._tool_runtime,
        )
        empty = await services.runs._tool_runtime.invoke(
            "research.paper.read",
            {
                "document_id": document_id,
                "action": "chunks",
                "start": 3,
                "limit": 1,
            },
            empty_context,
        )
        assert empty["chunks"] == []
        assert empty["next_start"] is None
        assert "paper_activity" not in empty_context.metadata
    finally:
        await services.close()


@pytest.mark.anyio
async def test_startup_reconciles_existing_papers_into_workspace(
    test_settings,
) -> None:
    services = create_services(test_settings)
    document_id = "legacy-paper"
    summary_path = f"papers/{document_id}/summary.md"
    notes_path = f"papers/{document_id}/notes.md"
    try:
        with services.session_factory() as session:
            session.add(
                Document(
                    id=document_id,
                    title="Legacy Paper",
                    source_filename="legacy.pdf",
                    content_type="application/pdf",
                    status="ready",
                    page_count=3,
                    metadata_json={},
                )
            )
            session.commit()
        services.storage.write_workspace_file(
            summary_path,
            "# Legacy Paper\n\nPreserve this summary.\n",
        )
        services.workspace.write_file(
            notes_path,
            "# Notes: Legacy Paper\n\nPreserve this note.\n",
            tags=["custom"],
        )

        await services.start()

        summary = services.workspace.read_file(summary_path)
        notes = services.workspace.read_file(notes_path)
        assert summary.content == "# Legacy Paper\n\nPreserve this summary.\n"
        assert summary.paper_id == document_id
        assert summary.paper_name == "Legacy Paper"
        assert summary.tags == ("paper", f"paper:{document_id}", "summary")
        assert notes.content == "# Notes: Legacy Paper\n\nPreserve this note.\n"
        assert notes.tags == (
            "custom",
            "paper",
            f"paper:{document_id}",
            "notes",
        )
        assert [item.path for item in services.workspace.search(query="Preserve this")] == [
            notes_path,
            summary_path,
        ]
    finally:
        await services.close()


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


def test_work_plan_tracks_pending_items() -> None:
    context = ScholarWeaveContext(run_id="work-run", tool_runtime=Runtime())

    created = create_work_plan(
        {
            "items": [
                {"id": "sources", "title": "Collect sources"},
                {"id": "synthesis", "title": "Write synthesis"},
            ]
        },
        context,
    )
    assert [item["id"] for item in created["pending"]] == ["sources", "synthesis"]

    update_work_item(
        {
            "id": "sources",
            "status": "completed",
            "summary": "Collected two primary sources.",
        },
        context,
    )
    assert [item["id"] for item in work_plan(context)["pending"]] == ["synthesis"]

    finished = update_work_item(
        {
            "id": "synthesis",
            "status": "blocked",
            "summary": "The requested dataset is private.",
        },
        context,
    )
    assert finished["complete"] is True


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
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF",
        filename="research.pdf",
        title="Research paper",
    )

    async def download_pdf(
        url: str,
        *,
        title: str | None = None,
        arxiv_only: bool = False,
    ):
        calls.append((url, title, arxiv_only))
        return document

    try:
        runtime = services.runs._tool_runtime
        monkeypatch.setattr(services.source_downloads, "download_pdf", download_pdf)
        result = await runtime.invoke(
            "research.sources.acquire",
            {
                "kind": "paper",
                "url": "https://papers.example/research.pdf",
                "title": "Research paper",
            },
            ScholarWeaveContext(run_id="paper-run", tool_runtime=runtime),
        )
        notes = services.workspace.read_file(result["notes_path"]).content
    finally:
        await services.close()

    assert result["document_id"] == document.id
    assert result["summary_path"] == f"papers/{document.id}/summary.md"
    assert result["notes_path"] == f"papers/{document.id}/notes.md"
    assert "https://papers.example/research.pdf" in notes
    assert calls == [
        ("https://papers.example/research.pdf", "Research paper", False),
    ]


@pytest.mark.anyio
async def test_paper_summary_updates_canonical_summary_only_through_save_tool(
    test_settings,
) -> None:
    services = create_services(test_settings)
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF",
        filename="durable-summary.pdf",
        title="Durable summary",
    )
    runtime = services.runs._tool_runtime
    context = ScholarWeaveContext(
        run_id="summary-run",
        tool_runtime=runtime,
        metadata={
            "paper_activity": [
                {
                    "action": "read",
                    "document_id": document.id,
                    "title": document.title,
                }
            ]
        },
    )
    try:
        saved = await runtime.invoke(
            "research.summary.save",
            {
                "document_id": document.id,
                "content": (
                    "# Summary\n\n"
                    "This deliberately uncited summary is long enough to represent substantive "
                    "content and verifies that saving no longer rejects a result based on a minimum "
                    "citation count. The prompt still asks the model for citations, but persistence "
                    "does not impose a separate citation-format gate on the completed summary."
                ),
                "review_summary": "Self-reviewed without a citation-count gate.",
            },
            context,
        )
        await runtime.invoke(
            "research.notes.save",
            {
                "target": "paper_notes",
                "mode": "append",
                "document_id": document.id,
                "path": None,
                "name": None,
                "content": "A durable specialist finding with [p.2].",
                "tags": ["paper"],
            },
            context,
        )
        canonical = services.workspace.read_file(saved["canonical_path"]).content
        notes = services.workspace.read_file(f"papers/{document.id}/notes.md").content
    finally:
        await services.close()

    assert saved["canonical_path"] == f"papers/{document.id}/summary.md"
    assert "deliberately uncited summary" in canonical
    assert saved["citation_count"] == 0
    assert "A durable specialist finding with [p.2]." in notes
    assert {
        item["action"] for item in context.metadata["paper_activity"]
    } >= {"read", "summary_saved", "notes_saved"}
    validate_paper_work_completion(context)
    assert not hasattr(runtime, "persist_run_output")


@pytest.mark.anyio
async def test_paper_summary_checkpoint_survives_context_and_is_run_scoped(
    test_settings,
) -> None:
    services = create_services(test_settings)
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF",
        filename="large-paper.pdf",
        title="Large paper",
    )
    context = ScholarWeaveContext(
        run_id="large-summary-run",
        tool_runtime=services.runs._tool_runtime,
        metadata={
            "paper_activity": [
                {
                    "action": "read",
                    "document_id": document.id,
                    "title": document.title,
                }
            ]
        },
    )
    first_entry = "Pages 1-3: contribution evidence [p.1]. " * 10
    second_entry = "Pages 4-6: evaluation evidence [p.5]. " * 10
    try:
        first = await services.runs._tool_runtime.invoke(
            "research.summary.checkpoint",
            {
                "document_id": document.id,
                "action": "append",
                "content": first_entry,
                "offset": None,
                "limit": None,
            },
            context,
        )
        await services.runs._tool_runtime.invoke(
            "research.summary.checkpoint",
            {
                "document_id": document.id,
                "action": "append",
                "content": second_entry,
                "offset": None,
                "limit": None,
            },
            context,
        )
        page = await services.runs._tool_runtime.invoke(
            "research.summary.checkpoint",
            {
                "document_id": document.id,
                "action": "read",
                "content": None,
                "offset": 0,
                "limit": 256,
            },
            context,
        )
        checkpoint_path = test_settings.artifacts_dir / first["checkpoint_path"]
        assert checkpoint_path.is_file()
        assert page["status"] == "available"
        assert page["has_more"] is True
        assert page["next_offset"] == 256
        assert first_entry[:80] in page["content"]

        services.documents.delete_run_artifacts(context.run_id)
        assert not checkpoint_path.exists()
    finally:
        await services.close()


@pytest.mark.anyio
async def test_paper_summary_reader_caps_five_overlapping_page_batches(
    test_settings,
    monkeypatch,
) -> None:
    services = create_services(test_settings)
    runtime = services.runs._tool_runtime
    delegated_calls: list[dict[str, Any]] = []

    async def read_batch(arguments, _context):
        delegated_calls.append(arguments)
        start = int(arguments["start"])
        limit = int(arguments["limit"])
        return {
            "pages": [],
            "has_more": True,
            "next_page": start + limit,
        }

    monkeypatch.setattr(runtime, "_read_research_paper", read_batch)
    context = ScholarWeaveContext(run_id="bounded-summary-run", tool_runtime=runtime)
    try:
        results = []
        starts = [1, 10, 20, 30, 40]
        for start in starts:
            results.append(
                await runtime._read_paper_summary_batch(
                    {
                        "document_id": "paper-1",
                        "action": "pages",
                        "start": start,
                    },
                    context,
                )
            )
            runtime._paper_summary_state(context, "paper-1").pop(
                "pending_checkpoint",
                None,
            )
        with pytest.raises(ValueError, match="capped at five"):
            await runtime._read_paper_summary_batch(
                {
                    "document_id": "paper-1",
                    "action": "pages",
                    "start": 51,
                },
                context,
            )
    finally:
        await services.close()

    assert [result["summary_batch"] for result in results] == [1, 2, 3, 4, 5]
    assert [call["limit"] for call in delegated_calls] == [10, 11, 11, 11, 11]
    assert [result["next_start"] for result in results] == [10, 20, 30, 40, 50]
    assert len(delegated_calls) == 5


@pytest.mark.anyio
async def test_paper_summary_reader_requires_checkpoint_before_next_batch(
    test_settings,
    monkeypatch,
) -> None:
    services = create_services(test_settings)
    runtime = services.runs._tool_runtime

    async def read_batch(arguments, _context):
        start = int(arguments["start"])
        return {
            "pages": [
                {
                    "page_number": page,
                    "citation": f"p.{page}",
                    "text": f"page {page}",
                }
                for page in range(start, start + 10)
            ],
            "has_more": True,
            "next_page": start + 10,
        }

    monkeypatch.setattr(runtime, "_read_research_paper", read_batch)
    context = ScholarWeaveContext(run_id="gated-summary-run", tool_runtime=runtime)
    try:
        first = await runtime._read_paper_summary_batch(
            {"document_id": "paper-1", "action": "pages", "start": 1},
            context,
        )
        with pytest.raises(ValueError, match="must be appended"):
            await runtime._read_paper_summary_batch(
                {"document_id": "paper-1", "action": "pages", "start": 11},
                context,
            )
    finally:
        await services.close()

    assert first["checkpoint_required"] is True
    assert first["coverage"] == {"kind": "pages", "start": 1, "end": 10}
    assert first["next_start"] == 10
    assert first["checkpoint_path"].endswith("/paper-summary/paper-1/checkpoint.md")


@pytest.mark.anyio
async def test_final_paper_summary_checkpoint_is_returned_without_reread(
    test_settings,
    monkeypatch,
) -> None:
    services = create_services(test_settings)
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF",
        filename="short-paper.pdf",
        title="Short paper",
    )
    runtime = services.runs._tool_runtime
    context = ScholarWeaveContext(run_id="final-checkpoint-run", tool_runtime=runtime)

    async def read_batch(arguments, active_context):
        runtime._record_paper_activity(
            active_context,
            document,
            "read",
        )
        return {
            "pages": [
                {
                    "page_number": 1,
                    "citation": "p.1",
                    "text": "paper evidence",
                }
            ],
            "has_more": False,
            "next_page": 2,
        }

    monkeypatch.setattr(runtime, "_read_research_paper", read_batch)
    try:
        await runtime._read_paper_summary_batch(
            {"document_id": document.id, "action": "pages", "start": 1},
            context,
        )
        appended = await runtime._paper_summary_checkpoint(
            {
                "document_id": document.id,
                "action": "append",
                "content": "Contribution and evidence [p.1].",
                "offset": None,
                "limit": None,
            },
            context,
        )
    finally:
        await services.close()

    assert appended["has_more_paper"] is False
    assert appended["final_checkpoint"] == "Contribution and evidence [p.1].\n"
    assert "without rereading" in appended["instruction"]


@pytest.mark.anyio
async def test_parallel_paper_summary_batch_reads_leave_one_pending_batch(
    test_settings,
    monkeypatch,
) -> None:
    services = create_services(test_settings)
    runtime = services.runs._tool_runtime

    async def read_batch(arguments, _context):
        await asyncio.sleep(0)
        start = int(arguments["start"])
        return {
            "pages": [{"page_number": start, "text": "page"}],
            "has_more": True,
            "next_page": start + 10,
        }

    monkeypatch.setattr(runtime, "_read_research_paper", read_batch)
    context = ScholarWeaveContext(run_id="parallel-summary-run", tool_runtime=runtime)
    try:
        results = await asyncio.gather(
            runtime._read_paper_summary_batch(
                {"document_id": "paper-1", "action": "pages", "start": 1},
                context,
            ),
            runtime._read_paper_summary_batch(
                {"document_id": "paper-1", "action": "pages", "start": 11},
                context,
            ),
            return_exceptions=True,
        )
    finally:
        await services.close()

    assert sum(isinstance(result, dict) for result in results) == 1
    errors = [result for result in results if isinstance(result, Exception)]
    assert len(errors) == 1
    assert "must be appended" in str(errors[0])


@pytest.mark.anyio
async def test_reading_substantive_canonical_summary_reuses_it(test_settings) -> None:
    services = create_services(test_settings)
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF",
        filename="existing-summary.pdf",
        title="Existing summary",
    )
    paper = services.workspace.ensure_paper_folder(document.id, document.title)
    substantive = (
        "# Existing summary\n\n"
        "## Contribution\n\n"
        "The paper introduces a durable contribution supported by primary evidence. "
        "It describes the mechanism, assumptions, and intended operating conditions in enough "
        "detail to distinguish this reviewed summary from the generated placeholder. "
        "The evaluation reports another result and its limitations without requiring a fixed "
        "number of specially formatted citations for this existing summary to remain reusable."
    )
    services.workspace.write_file(
        paper["summary_path"],
        substantive,
        tags=["paper", f"paper:{document.id}", "summary"],
    )
    context = ScholarWeaveContext(
        run_id="reuse-summary-run",
        tool_runtime=services.runs._tool_runtime,
    )
    try:
        result = await services.runs._tool_runtime.invoke(
            "research.notes.read",
            {"path": paper["summary_path"]},
            context,
        )
    finally:
        await services.close()

    assert result["summary_check"]["status"] == "reusable"
    assert result["summary_check"]["needs_regeneration"] is False
    assert context.metadata["paper_activity"][-1]["action"] == "summary_reused"
    context.metadata["paper_activity"].extend(
        [
            {
                "action": "read",
                "document_id": document.id,
                "title": document.title,
            },
            {
                "action": "notes_saved",
                "document_id": document.id,
                "title": document.title,
            },
        ]
    )
    validate_paper_work_completion(context)


@pytest.mark.anyio
async def test_reading_summary_template_requires_regeneration(test_settings) -> None:
    services = create_services(test_settings)
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF",
        filename="summary-template.pdf",
        title="Summary template",
    )
    paper = services.workspace.ensure_paper_folder(document.id, document.title)
    context = ScholarWeaveContext(
        run_id="incomplete-summary-run",
        tool_runtime=services.runs._tool_runtime,
    )
    try:
        result = await services.runs._tool_runtime.invoke(
            "research.notes.read",
            {"path": paper["summary_path"]},
            context,
        )
    finally:
        await services.close()

    assert result["summary_check"]["status"] == "incomplete"
    assert result["summary_check"]["needs_regeneration"] is True
    assert "paper_activity" not in context.metadata


def test_chat_agent_can_use_persistence_tools_directly() -> None:
    blueprint = research_blueprint({})
    researcher_tools = set(blueprint.agents[0].tool_ids)

    assert {
        "save-note",
        "summary-read",
        "summary-checkpoint",
        "save-summary",
    }.issubset(researcher_tools)
    assert blueprint.agent_tools == []


def test_paper_completion_gate_is_activated_by_model_selected_activity() -> None:
    validate_paper_work_completion(
        ScholarWeaveContext(
            run_id="non-paper-run",
            tool_runtime=Runtime(),
        )
    )

    context = ScholarWeaveContext(
        run_id="paper-run",
        tool_runtime=Runtime(),
        metadata={
            "paper_activity": [
                {
                    "action": "read",
                    "document_id": "paper-1",
                    "title": "Required Paper",
                },
                {
                    "action": "summary_saved",
                    "document_id": "paper-1",
                    "title": "Required Paper",
                },
            ],
        },
    )

    with pytest.raises(ValidationError) as raised:
        validate_paper_work_completion(context)

    assert raised.value.issues == (
        "Required Paper (paper-1): populate notes.md through save_research_note",
    )
    context.metadata["paper_activity"].append(
        {
            "action": "notes_saved",
            "document_id": "paper-1",
            "title": "Required Paper",
        }
    )
    validate_paper_work_completion(context)


@pytest.mark.anyio
async def test_paper_persistence_rejects_writes_before_extracted_content_is_read(
    test_settings,
) -> None:
    services = create_services(test_settings)
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF",
        filename="unread.pdf",
        title="Unread Paper",
    )
    context = ScholarWeaveContext(
        run_id="unread-run",
        tool_runtime=services.runs._tool_runtime,
    )
    try:
        with pytest.raises(ValueError, match="after extracted pages or chunks"):
            await services.runs._tool_runtime.invoke(
                "research.notes.save",
                {
                    "target": "paper_notes",
                    "mode": "append",
                    "document_id": document.id,
                    "path": None,
                    "name": None,
                    "content": "Unsupported note.",
                    "tags": [],
                },
                context,
            )
    finally:
        await services.close()


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
        title_matches = await runtime.invoke(
            "research.library.search",
            {"query": "Metadata Only", "document_id": None, "limit": 10},
            context,
        )
        filename_matches = await runtime.invoke(
            "research.library.search",
            {"query": "paper.pdf", "document_id": None, "limit": 10},
            context,
        )
        literal_null_matches = await runtime.invoke(
            "research.library.search",
            {"query": "Metadata Only", "document_id": "null", "limit": 10},
            context,
        )
        literal_null_list = await runtime.invoke(
            "research.library.search",
            {"query": "", "document_id": "null", "limit": 10},
            context,
        )
        literal_null_query = await runtime.invoke(
            "research.library.search",
            {"query": "null", "document_id": None, "limit": 10},
            context,
        )
        missing = await runtime.invoke(
            "research.library.search",
            {"query": None, "document_id": "missing-paper", "limit": 10},
            context,
        )

        assert listed[0]["readable"] is False
        assert inspected["source_available"] is True
        assert inspected["readable"] is False
        assert title_matches[0]["document_id"] == document_id
        assert title_matches[0]["matched_fields"] == ["title"]
        assert filename_matches[0]["document_id"] == document_id
        assert filename_matches[0]["matched_fields"] == ["source_filename"]
        assert literal_null_matches[0]["document_id"] == document_id
        assert literal_null_list[0]["document_id"] == document_id
        assert literal_null_query == []
        assert missing["found"] is False
        assert missing["requested_document_id"] == "missing-paper"
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


@pytest.mark.anyio
async def test_research_library_ranks_metadata_words_and_can_ignore_results(
    test_settings,
) -> None:
    services = create_services(test_settings)
    try:
        documents = [
            ("doc-both", "Neural Attention Systems"),
            ("doc-neural", "Neural Retrieval"),
            ("doc-attention", "Efficient Attention"),
            ("doc-partial", "Attentional Models"),
            ("doc-author", "Database Indexes"),
        ]
        with services.session_factory() as session:
            session.add_all(
                Document(
                    id=document_id,
                    title=title,
                    source_filename=f"{document_id}.pdf",
                    content_type="application/pdf",
                    status="ready",
                    metadata_json=(
                        {"authors": ["Ada Lovelace"]}
                        if document_id == "doc-author"
                        else {}
                    ),
                )
                for document_id, title in documents
            )
            session.commit()

        runtime = services.runs._tool_runtime
        context = ScholarWeaveContext(run_id="ranked-library-search", tool_runtime=runtime)
        first = await runtime.invoke(
            "research.library.search",
            {
                "query": "the neural and attention",
                "document_id": None,
                "ignore_document_ids": None,
                "limit": 3,
            },
            context,
        )
        second = await runtime.invoke(
            "research.library.search",
            {
                "query": "the neural and attention",
                "document_id": None,
                "ignore_document_ids": [item["document_id"] for item in first],
                "limit": 3,
            },
            context,
        )

        assert [item["document_id"] for item in first] == [
            "doc-both",
            "doc-attention",
            "doc-neural",
        ]
        assert first[0]["matched_words"] == ["neural", "attention"]
        assert first[1]["matched_words"] == ["attention"]
        assert second == []
        author = await runtime.invoke(
            "research.library.search",
            {
                "query": "a paper by lovelace",
                "document_id": None,
                "ignore_document_ids": None,
                "limit": 3,
            },
            context,
        )
        assert [item["document_id"] for item in author] == ["doc-author"]
        assert author[0]["matched_fields"] == ["authors"]
        assert author[0]["authors"] == ["Ada Lovelace"]
    finally:
        await services.close()


@pytest.mark.anyio
async def test_research_library_organization_tool_creates_and_assigns_folders(
    test_settings,
) -> None:
    services = create_services(test_settings)
    try:
        document = services.documents.create_document_from_bytes(
            b"%PDF-1.4\n%%EOF",
            filename="organized.pdf",
            title="Organized Paper",
        )
        runtime = services.runs._tool_runtime
        context = ScholarWeaveContext(run_id="organize-library", tool_runtime=runtime)

        created = await runtime.invoke(
            "research.library.organize",
            {
                "action": "create_folder",
                "folder_name": "Methods",
                "document_id": None,
                "folder_id": None,
            },
            context,
        )
        folder_id = created["folder"]["id"]
        moved = await runtime.invoke(
            "research.library.organize",
            {
                "action": "move_paper",
                "folder_name": None,
                "document_id": document.id,
                "folder_id": folder_id,
            },
            context,
        )
        listing = await runtime.invoke(
            "research.library.organize",
            {
                "action": "list",
                "folder_name": None,
                "document_id": None,
                "folder_id": None,
            },
            context,
        )

        assert moved["paper"]["folder_id"] == folder_id
        assert listing["folders"] == [{"id": folder_id, "name": "Methods"}]
        assert listing["papers"] == [
            {
                "document_id": document.id,
                "title": "Organized Paper",
                "folder_id": folder_id,
            }
        ]
    finally:
        await services.close()
