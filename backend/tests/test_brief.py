from __future__ import annotations

import asyncio
from datetime import timedelta
import json
import time
from xml.sax.saxutils import escape

import httpx
import pytest

from backend.app import create_app
from backend.brief.catalog import SOURCES
from backend.brief.collection import parse_feed, parse_headlines
from backend.brief.schemas import BriefPreferences
from backend.brief.service import BriefService
from backend.brief.summaries import BriefSummaryService, InputBudget, MAX_INPUT_TOKENS
from backend.agents.blueprint import ModelReferenceSpec
from backend.providers.schemas import ProviderCreate, ProviderModel
from backend.core.errors import NotFoundError, ValidationError
from backend.research.sources import _ReadableHTMLParser
from backend.utils import utcnow
from sqlalchemy import event


_HIGHLIGHTS = "Abstracts point to more efficient adaptive reasoning, with preliminary results.\n\nPublisher headlines suggest new inference work, but full sources have not been reviewed."


def _brief_model(services, stub_provider, *, api_mode="chat_completions", window=65_536):
    profile = services.providers.create(ProviderCreate(
        name="Brief test model", kind="openai_compatible",
        base_url=f"{stub_provider.base_url}/v1", api_mode=api_mode,
        models=[
            ProviderModel(name="brief-model", capabilities={"chat"}, context_window_tokens=window),
            ProviderModel(name="chat-model", capabilities={"chat"}, context_window_tokens=window),
        ],
    ))
    reference = ModelReferenceSpec(provider_profile_id=profile.id, model="brief-model")
    services.settings.default_model_references = {"chat": reference.model_dump()}
    stub_provider.reply = _HIGHLIGHTS
    return reference


async def _complete_highlights(brief):
    task = brief._summary_task
    assert task is not None
    await asyncio.wait_for(task, timeout=30)
    return await brief.edition()


def test_highlight_batches_obey_exact_token_boundary_and_preserve_whole_records():
    system = "Summarize metadata."
    records = [{"title": f"Paper {index}", "abstract": "alpha " * 16_100} for index in range(2)]
    budget = InputBudget("gpt-4o", MAX_INPUT_TOKENS)
    prompt = json.dumps({"stage": "final", "records": records}, ensure_ascii=False, separators=(",", ":"))
    tokens = budget.count(system, prompt)
    assert tokens <= MAX_INPUT_TOKENS
    assert budget.method == "o200k_base"
    assert InputBudget("gpt-4o", tokens).batches(records, system) == [records]
    assert InputBudget("gpt-4o", tokens - 1).batches(records, system) == [[records[0]], [records[1]]]
    three = [*records, {**records[0], "title": "Third paper"}]
    groups = budget.batches(three, system)
    assert len(groups) == 2
    assert [record for group in groups for record in group] == three
    for group in groups:
        payload = json.dumps({"stage": "final", "records": group}, ensure_ascii=False, separators=(",", ":"))
        assert budget.count(system, payload) <= MAX_INPUT_TOKENS
    with pytest.raises(ValidationError, match="will not be cut"):
        budget.batches([{"abstract": "alpha " * MAX_INPUT_TOKENS}], system)


def test_unknown_model_uses_conservative_unicode_budget():
    budget = InputBudget("unrecognized-local-model", MAX_INPUT_TOKENS)
    prompt = json.dumps({"abstract": "\U0001f52c" * 100}, ensure_ascii=False)
    assert budget.method == "utf8-byte-upper-bound"
    assert budget.count("instructions", prompt) > len(prompt.encode("utf-8"))


@pytest.mark.anyio
@pytest.mark.parametrize("api_mode", ["chat_completions", "responses"])
async def test_highlights_use_metadata_only_and_cache_archive_across_restart_and_clear(
    brief_app, stub_provider, test_settings, api_mode,
):
    _, services, requests, client = brief_app
    reference = _brief_model(services, stub_provider, api_mode=api_mode)
    brief = services.brief
    try:
        first = await brief.refresh()
        assert first.highlights.status == "running"
        edition = await _complete_highlights(brief)
        summary = edition.highlights.summary
        assert edition.highlights.status == "ready" and summary is not None
        assert summary.text == _HIGHLIGHTS and summary.model_reference == reference
        assert summary.item_count == 2 and summary.batch_count == 1
        assert len(requests) == 2
        assert all("pdf" not in url and "/news/adaptive" not in url for url in requests)
        payload = (stub_provider.response_api_requests if api_mode == "responses" else stub_provider.requests)[0]
        serialized = json.dumps(payload)
        for item in edition.items:
            assert item.title in serialized and item.abstract in serialized
            assert item.url not in serialized
        if api_mode == "responses":
            assert payload["store"] is False
            assert 0 < payload["max_output_tokens"] <= 4096
        else:
            assert 0 < payload["max_completion_tokens"] <= 4096
        archive = test_settings.workspace_dir / summary.workspace_path
        archive_json = json.loads(archive.read_text(encoding="utf-8"))
        assert archive_json["day"] == edition.day
        assert "items" not in archive_json and "url" not in archive_json
        assert brief.summary_history(limit=25, offset=0) == [summary]
        before = len(stub_provider.requests) + len(stub_provider.response_api_requests)
        await brief.close()
        restored = BriefService(
            services.storage, services.source_downloads, services.documents,
            services.workspace, brief._summaries,
        )
        assert (await restored.edition()).highlights.summary == summary
        assert len(stub_provider.requests) + len(stub_provider.response_api_requests) == before
        cleared = await restored.clear()
        assert cleared.cleared and not cleared.items and cleared.highlights.summary == summary
        assert archive.exists() and restored.summary_history(limit=25, offset=0) == [summary]
        await restored.close()
    finally:
        await services.close()
        await client.aclose()


@pytest.mark.anyio
async def test_highlight_model_is_independent_and_defaults_to_last_chat_choice(brief_app, stub_provider):
    _, services, _, client = brief_app
    try:
        reference = _brief_model(services, stub_provider)
        chat = reference.model_copy(update={"model": "chat-model"})
        services.settings.last_chat_model_reference = chat.model_dump()
        assert services.brief.catalog().summary_model_reference == chat
        await services.brief.preferences(BriefPreferences(
            source_ids=["arxiv", "openai"], summary_model_reference=reference,
        ))
        assert services.brief.catalog().summary_model_reference == reference
        assert services.settings.last_chat_model_reference == chat.model_dump()
        await services.brief.refresh()
        assert (await _complete_highlights(services.brief)).highlights.summary.model_reference == reference
        assert stub_provider.requests[0]["model"] == "brief-model"
    finally:
        await services.close()
        await client.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("hierarchical", [False, True])
async def test_highlights_batch_complete_metadata_and_bound_every_combiner(
    brief_app, stub_provider, test_settings, monkeypatch, hierarchical,
):
    _, services, requests, client = brief_app
    brief = services.brief
    try:
        initial = await brief.refresh()
        reference = _brief_model(services, stub_provider)
        limit = 3000 if hierarchical else MAX_INPUT_TOKENS
        monkeypatch.setattr("backend.brief.summaries.MAX_INPUT_TOKENS", limit)
        brief._edition.items = [
            initial.items[0].model_copy(update={
                "id": f"source-{index}", "title": f"Complete source {index}",
                "abstract": "evidence " * (120 if hierarchical else 3000) + f"END-{index}",
            }) for index in range(5)
        ]
        if hierarchical:
            stub_provider.reply = "Concise evidence and limitations. " * 20
        collected = list(requests)
        await brief.generate_highlights(reference)
        result = await _complete_highlights(brief)
        assert result.highlights.status == "ready", result.highlights.error
        summary = result.highlights.summary
        assert summary is not None and summary.batch_count == 5
        assert len(stub_provider.requests) >= 6
        if hierarchical:
            assert len(stub_provider.requests) > 6
        budget = InputBudget("brief-model", limit)
        all_records = []
        for payload in stub_provider.requests:
            system, user = payload["messages"]
            assert budget.count(system["content"], user["content"]) <= limit
            body = json.loads(user["content"])
            if "source metadata" in system["content"]:
                all_records.extend(body["records"])
        assert all_records == BriefSummaryService.records(brief._edition)
        assert requests == collected
        folder = test_settings.workspace_dir / ".scholarweave" / "brief-temp"
        assert len(list(folder.rglob("part-*.json"))) == 5
        if hierarchical:
            assert list(folder.rglob("combine-*.json"))
        archive = test_settings.workspace_dir / summary.workspace_path
        monkeypatch.setattr(brief, "_day", lambda: "2099-01-01")
        assert (await brief.edition()).day is None
        assert archive.exists() and not folder.exists()
    finally:
        await services.close()
        await client.aclose()


@pytest.mark.anyio
async def test_oversized_source_and_failed_regeneration_do_not_save_partial_highlights(brief_app, stub_provider):
    _, services, _, client = brief_app
    brief = services.brief
    try:
        initial = await brief.refresh()
        reference = _brief_model(services, stub_provider)
        brief._edition.items[0].abstract = "evidence " * MAX_INPUT_TOKENS
        await brief.generate_highlights(reference)
        failed = await _complete_highlights(brief)
        assert failed.highlights.status == "failed" and "will not be cut" in failed.highlights.error
        assert not stub_provider.requests
        assert brief.summary_history(limit=25, offset=0) == []
        brief._edition.items = initial.items
        await brief.generate_highlights(reference)
        prior = (await _complete_highlights(brief)).highlights.summary
        assert prior is not None
        stub_provider.reply = ""
        await brief.generate_highlights(reference)
        failed = await _complete_highlights(brief)
        assert failed.highlights.status == "failed" and "complete highlights summary" in failed.highlights.error
        assert failed.highlights.summary == prior
        assert brief.summary_history(limit=25, offset=0) == [prior]
    finally:
        await services.close()
        await client.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("action", ["clear", "close", "refresh"])
async def test_slow_highlights_do_not_block_cached_reads_and_obsolete_work_is_cancelled(
    brief_app, stub_provider, action,
):
    _, services, _, client = brief_app
    brief = services.brief
    try:
        reference = _brief_model(services, stub_provider)
        def delayed_reply(_payload):
            time.sleep(0.5)
            return _HIGHLIGHTS
        stub_provider._reply_for = delayed_reply
        await brief.refresh()
        task = brief._summary_task
        assert task is not None
        assert await asyncio.to_thread(stub_provider.request_started.wait, 5)
        edition = await asyncio.wait_for(brief.edition(), timeout=0.2)
        assert len(edition.items) == 2 and edition.highlights.status == "running"
        if action == "clear":
            await brief.clear()
            assert (await brief.edition()).cleared
        elif action == "close":
            await brief.close()
            restored = BriefService(
                services.storage, services.source_downloads, services.documents,
                services.workspace, brief._summaries,
            )
            assert (await restored.edition()).highlights.status == "interrupted"
            assert restored._summary_task is None
            await restored.close()
        else:
            await brief.preferences(BriefPreferences(source_ids=["arxiv", "openai"],
                summary_model_reference=reference.model_copy(update={"model": "chat-model"})))
            await brief.refresh(force=True)
            result = await _complete_highlights(brief)
            assert result.highlights.summary.model_reference.model == "chat-model"
        with pytest.raises(asyncio.CancelledError):
            await task
        if action != "refresh":
            assert brief.summary_history(limit=25, offset=0) == []
    finally:
        await services.close()
        await client.aclose()


@pytest.mark.anyio
async def test_highlights_routes_generate_and_read_paginated_daily_archives(brief_app, stub_provider):
    app, services, _, client = brief_app
    try:
        await services.brief.refresh()
        reference = _brief_model(services, stub_provider)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
            started = await api.post("/api/brief/highlights", json={"model_reference": reference.model_dump()})
            assert started.status_code == 200 and started.json()["highlights"]["status"] == "running"
            assert started.headers["cache-control"] == "no-store"
            completed = await _complete_highlights(services.brief)
            cached = await api.get("/api/brief")
            assert cached.json()["highlights"]["summary"]["text"] == _HIGHLIGHTS
            history = await api.get("/api/brief/summaries?limit=1&offset=0")
            assert history.status_code == 200
            assert history.json() == [completed.highlights.summary.model_dump(mode="json")]
            assert (await api.get("/api/brief/summaries?limit=1&offset=1")).json() == []
            assert (await api.get("/api/brief/summaries?limit=0")).status_code == 422
            assert (await api.get("/api/brief/summaries?offset=-1")).status_code == 422
            await api.delete("/api/brief")
            assert (await api.get("/api/brief/summaries")).json() == history.json()
            assert (await api.post("/api/brief/highlights", json={})).status_code == 400
    finally:
        await services.close()
        await client.aclose()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _rss(*, paper: bool = False, duplicate: bool = False) -> bytes:
    now = utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")
    title = "Test-time reinforcement learning for tool use" if paper else "Better inference with adaptive compute"
    url = "https://arxiv.org/abs/2610.01234" if paper else "https://openai.com/news/adaptive"
    description = "arXiv:2610.01234v2 Announce Type: replace Abstract: Reinforcement learning improves agent reasoning." if paper else "A technical report about inference."
    item = (
        f"<item><title>{title}</title><link>{url}</link><pubDate>{now}</pubDate>"
        f"<description>{escape(description)}</description><category>Research</category>"
        "<arxiv:announce_type>replace</arxiv:announce_type><dc:creator>Alice, Bob</dc:creator></item>"
    )
    return (
        '<rss xmlns:arxiv="http://arxiv.org/schemas/atom" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>'
        + item + (item if duplicate else "") + "</channel></rss>"
    ).encode()


@pytest.fixture
def brief_app(test_settings, monkeypatch):
    app = create_app(test_settings)
    container = app.state.services
    downloads = container.source_downloads
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.host == "rss.arxiv.org":
            return httpx.Response(200, content=_rss(paper=True, duplicate=True))
        if request.url.path == "/news/rss.xml":
            return httpx.Response(200, content=_rss())
        if request.url.host == "arxiv.org":
            return httpx.Response(200, content=b"%PDF-1.7\nA temporary research paper")
        if request.url.path == "/news/adaptive":
            return httpx.Response(
                200, text="<html><title>Adaptive compute</title><main><p>Technical evidence about inference.</p></main></html>",
                headers={"Content-Type": "text/html"},
            )
        return httpx.Response(503)

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(downloads, "_client", client)
    monkeypatch.setattr(downloads, "_require_peer_validation", False)

    async def allow_url(_url):
        pass

    monkeypatch.setattr(downloads, "_validate_public_url", allow_url)
    container.brief._preferences = BriefPreferences(source_ids=["arxiv", "openai"])
    return app, container, requests, client


@pytest.mark.anyio
async def test_headlines_and_downloads_remain_transient_until_keep(brief_app, test_settings):
    _, services, requests, client = brief_app
    brief = services.brief
    initial_files = {path for path in test_settings.workspace_dir.rglob("*") if path.is_file()}
    try:
        edition = await brief.refresh()
        assert len(edition.items) == 2
        paper = next(item for item in edition.items if item.kind == "paper")
        page = next(item for item in edition.items if item.kind == "lab")
        assert paper.announcement == "replace"
        assert paper.arxiv_id == "2610.01234v2"
        assert paper.authors == ["Alice", "Bob"]
        assert "RL & post-training" in paper.topics
        assert not any("pdf/" in url for url in requests)
        assert not services.documents.list_documents()
        assert {path for path in test_settings.workspace_dir.rglob("*") if path.is_file()} == initial_files
        pdf = await brief.fetch(paper.id)
        web = await brief.fetch(page.id)
        assert pdf.media_type == "application/pdf"
        assert web.text == "Technical evidence about inference."
        assert await services.source_downloads.list_web_sources() == []
        assert not services.documents.list_documents()
        assert {path for path in test_settings.workspace_dir.rglob("*") if path.is_file()} == initial_files
        request_count = len(requests)
        await brief.fetch(paper.id)
        assert len(requests) == request_count
        kept_paper = await brief.keep(paper.id)
        kept_page = await brief.keep(page.id)
        assert kept_paper.saved_document_id
        assert kept_page.saved_path
        assert (await brief.keep(paper.id)).saved_document_id == kept_paper.saved_document_id
        assert (await brief.keep(page.id)).saved_path == kept_page.saved_path
        saved_files = {path for path in test_settings.workspace_dir.rglob("*") if path.is_file()}
        assert len(services.documents.list_documents()) == 1
        note = services.workspace.read_file(kept_page.saved_path)
        assert page.url in note.content
        assert web.text in note.content
        cleared = await brief.clear()
        assert cleared.cleared and not cleared.items and not cleared.sources
        assert cleared.temporary_bytes == 0
        assert len(services.documents.list_documents()) == 1
        assert {path for path in test_settings.workspace_dir.rglob("*") if path.is_file()} == saved_files
        with pytest.raises(NotFoundError):
            await brief.raw(paper.id)
        assert (await brief.refresh()).cleared
        refreshed = await brief.refresh(force=True)
        assert next(item for item in refreshed.items if item.kind == "paper").saved_document_id == kept_paper.saved_document_id
        assert next(item for item in refreshed.items if item.kind == "lab").saved_path == kept_page.saved_path
    finally:
        await services.close()
        await client.aclose()


@pytest.mark.anyio
async def test_clear_discards_unsaved_data_but_not_unrelated_web_sources(brief_app):
    _, services, _, client = brief_app
    brief = services.brief
    try:
        other = await services.source_downloads.download_web_page("https://openai.com/news/adaptive")
        edition = await brief.refresh()
        for item in edition.items:
            await brief.fetch(item.id)
        assert (await brief.edition()).temporary_bytes > 0
        assert not services.documents.list_documents()
        await brief.clear()
        assert not brief._previews
        assert not services.workspace.list_files()
        assert (await services.source_downloads.get_web_source(other.id)).id == other.id
    finally:
        await services.close()
        await client.aclose()


@pytest.mark.anyio
async def test_daily_cache_preferences_discard_and_source_failures(brief_app, test_settings):
    _, services, requests, client = brief_app
    brief = services.brief
    try:
        edition = await brief.refresh()
        count = len(requests)
        await brief.refresh()
        assert len(requests) == count
        paper = next(item for item in edition.items if item.kind == "paper")
        await brief.fetch(paper.id)
        await brief.discard(paper.id)
        assert paper.id not in brief._previews
        assert all(item.kind != "paper" for item in (await brief.refresh(force=True)).items)
        await brief.preferences(BriefPreferences(source_ids=["arxiv", "deepmind"], exclude_terms=["tool use"]))
        filtered = await brief.refresh(force=True)
        assert not filtered.items
        assert next(source for source in filtered.sources if source.source_id == "deepmind").error
        assert (test_settings.data_dir / "brief-preferences.json").exists()
        restarted = BriefService(services.storage, services.source_downloads, services.documents, services.workspace)
        assert restarted.catalog().preferences.exclude_terms == ["tool use"]
        assert (await restarted.edition()).sources == filtered.sources
        brief._edition.day = (utcnow() - timedelta(days=2)).date().isoformat()
        assert (await brief.edition()).day is None
        assert not brief._dismissed
    finally:
        await services.close()
        await client.aclose()


@pytest.mark.anyio
async def test_daily_edition_previews_and_clear_survive_server_restarts(brief_app, test_settings, monkeypatch):
    _, services, requests, client = brief_app
    brief = services.brief
    restart = lambda: BriefService(services.storage, services.source_downloads, services.documents, services.workspace)
    try:
        edition = await brief.refresh()
        for item in edition.items:
            await brief.fetch(item.id)
        previews = {item.id: await brief.raw(item.id) for item in edition.items}
        await brief.close()
        count = len(requests)
        restored = restart()
        restored_edition = await restored.refresh()
        assert [item.id for item in restored_edition.items] == [item.id for item in edition.items]
        assert restored_edition.fetched_at == edition.fetched_at
        assert restored_edition.temporary_bytes == sum(len(content) for content, _ in previews.values())
        for item in restored_edition.items:
            assert item.downloaded
            assert await restored.raw(item.id) == previews[item.id]
            await restored.fetch(item.id)
        assert len(requests) == count
        dismissed = restored_edition.items[0]
        await restored.discard(dismissed.id)
        restored = restart()
        assert dismissed.id not in {item.id for item in (await restored.edition()).items}
        assert restored._key(dismissed) in restored._dismissed
        assert not services.storage.resolve_path(test_settings.data_dir, restored._preview_path(dismissed.id)).exists()
        await restored.clear()
        await restored.close()
        restored = restart()
        assert (await restored.refresh()).cleared
        assert len(requests) == count
        cache_dir = test_settings.data_dir / "temp" / "morning-brief"
        assert list(cache_dir.glob("*.bin")) == []
        tomorrow = utcnow() + timedelta(days=1)
        monkeypatch.setattr("backend.brief.service.utcnow", lambda: tomorrow)
        restored = restart()
        assert (await restored.edition()).day is None
        assert not cache_dir.exists()
        refreshed = await restored.refresh()
        assert refreshed.day == restored._day()
        assert len(requests) > count
    finally:
        await services.close()
        await client.aclose()


@pytest.mark.anyio
async def test_cached_edition_batches_saved_checks_and_reflects_library_deletions(brief_app):
    _, services, requests, client = brief_app
    brief = services.brief
    statements: list[str] = []
    engine = services.session_factory.kw["bind"]

    def track(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    try:
        edition = await brief.refresh()
        paper = next(item for item in edition.items if item.kind == "paper")
        page = next(item for item in edition.items if item.kind == "lab")
        await brief.fetch(paper.id)
        await brief.fetch(page.id)
        kept_paper = await brief.keep(paper.id)
        kept_page = await brief.keep(page.id)
        brief._edition.items = [
            paper.model_copy(update={"id": f"paper-{index}", "saved_document_id": kept_paper.saved_document_id})
            for index in range(80)
        ] + [
            page.model_copy(update={"id": f"page-{index}", "saved_path": kept_page.saved_path})
            for index in range(64)
        ]
        count = len(requests)
        event.listen(engine, "before_cursor_execute", track)
        cached = await brief.edition()
        assert len(cached.items) == 144
        assert all(item.saved_document_id for item in cached.items if item.kind == "paper")
        assert all(item.saved_path for item in cached.items if item.kind != "paper")
        assert len(statements) == 2
        assert len(requests) == count
        event.remove(engine, "before_cursor_execute", track)
        services.documents.delete_document(kept_paper.saved_document_id)
        services.workspace.delete_file(kept_page.saved_path)
        statements.clear()
        event.listen(engine, "before_cursor_execute", track)
        cached = await brief.edition()
        assert all(not item.saved_document_id and not item.saved_path for item in cached.items)
        assert len(statements) == 2
        assert len(requests) == count
    finally:
        if event.contains(engine, "before_cursor_execute", track):
            event.remove(engine, "before_cursor_execute", track)
        await services.close()
        await client.aclose()


@pytest.mark.anyio
async def test_cached_previews_expire_at_local_midnight_and_missing_files_are_explicit(brief_app, test_settings, monkeypatch, caplog):
    _, services, requests, client = brief_app
    try:
        brief = services.brief
        edition = await brief.refresh()
        paper = next(item for item in edition.items if item.kind == "paper")
        await brief.fetch(paper.id)
        count = len(requests)
        services.storage.delete_stored_file(test_settings.data_dir, brief._preview_path(paper.id))
        restored = BriefService(services.storage, services.source_downloads, services.documents, services.workspace)
        cached = await restored.refresh()
        assert len(requests) == count
        assert not next(item for item in cached.items if item.id == paper.id).downloaded
        assert "Could not restore Morning Brief preview" in caplog.text
        await restored.fetch(paper.id)
        assert len(requests) == count + 1
        monkeypatch.setattr(restored, "_day", lambda: "2099-01-01")
        assert (await restored.edition()).day is None
        assert not restored._previews
        assert not (test_settings.data_dir / "temp" / "morning-brief").exists()
    finally:
        await services.close()
        await client.aclose()


@pytest.mark.anyio
async def test_failed_download_and_keep_without_fetch_are_explicit(brief_app, monkeypatch):
    _, services, _, client = brief_app
    try:
        edition = await services.brief.refresh()
        paper = next(item for item in edition.items if item.kind == "paper")
        with pytest.raises(ValidationError, match="Download and inspect"):
            await services.brief.keep(paper.id)

        async def fail(_url):
            raise ValueError("Not a PDF")

        monkeypatch.setattr(services.source_downloads, "fetch_pdf_bytes", fail)
        with pytest.raises(ValidationError, match="Not a PDF"):
            await services.brief.fetch(paper.id)
        assert not services.brief._previews
        assert not services.documents.list_documents()
    finally:
        await services.close()
        await client.aclose()


@pytest.mark.anyio
async def test_clear_serializes_with_download_and_invalidates_old_ids(brief_app, monkeypatch):
    _, services, _, client = brief_app
    started, finish = asyncio.Event(), asyncio.Event()

    async def delayed(_url):
        started.set()
        await finish.wait()
        return b"%PDF-1.7\nTemporary"

    try:
        paper = next(item for item in (await services.brief.refresh()).items if item.kind == "paper")
        monkeypatch.setattr(services.source_downloads, "fetch_pdf_bytes", delayed)
        download = asyncio.create_task(services.brief.fetch(paper.id))
        await started.wait()
        clear = asyncio.create_task(services.brief.clear())
        finish.set()
        await download
        await clear
        assert (await services.brief.edition()).temporary_bytes == 0
        with pytest.raises(NotFoundError):
            await services.brief.keep(paper.id)
    finally:
        await services.close()
        await client.aclose()


@pytest.mark.anyio
async def test_brief_http_contract_and_clear_protection(brief_app):
    app, services, _, remote = brief_app
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/api/brief/catalog")).status_code == 200
            invalid = await client.put("/api/brief/preferences", json={"topic_ids": ["invalid"]})
            assert invalid.status_code == 422
            response = await client.post("/api/brief/refresh", json={"force": False})
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            item = response.json()["items"][0]
            assert (await client.post(f"/api/brief/items/{item['id']}/keep")).status_code == 400
            assert (await client.get(f"/api/brief/items/{item['id']}/raw")).status_code == 404
            assert (await client.post(f"/api/brief/items/{item['id']}/fetch")).status_code == 200
            assert (await client.get(f"/api/brief/items/{item['id']}/raw")).headers["cache-control"] == "no-store"
            kept = await client.post(f"/api/brief/items/{item['id']}/keep")
            assert kept.status_code == 200
            assert (await client.delete("/api/brief")).json()["temporary_bytes"] == 0
            assert (await client.post(f"/api/brief/items/{item['id']}/fetch")).status_code == 404
    finally:
        await services.close()
        await remote.aclose()


def test_rss_atom_html_parsing_and_untrusted_links():
    paper_source = SOURCES[0]
    assert len(parse_feed(_rss(paper=True), paper_source)) == 1
    source = next(source for source in SOURCES if source.id == "vllm")
    atom = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
    <title>vLLM 3.0</title><link href="https://github.com/vllm-project/vllm/releases/tag/v3.0"/>
    <updated>2026-10-01T12:00:00Z</updated></entry></feed>'''
    assert parse_feed(atom, source)[0].title == "vLLM 3.0"
    source = next(source for source in SOURCES if source.id == "anthropic")
    html = b'''<nav><a href="/research/nav">Navigation that is not an article</a></nav>
    <a href="/research/rl"><h2>Reinforcement learning for language models</h2><time datetime="2026-10-01">October 1, 2026</time></a>
    <a href="http://127.0.0.1/research/bad">Unsafe article that must not appear</a>
    <a href="/research/undated"><h2>Understanding context compression</h2></a>'''
    items = parse_headlines(html, source)
    assert len(items) == 2
    assert items[0].published_at is not None
    assert items[1].published_at is None


def test_official_release_and_lab_adapters_exclude_noise():
    source = next(source for source in SOURCES if source.id == "pytorch")
    release = {
        "name": "PyTorch 2.14", "html_url": "https://github.com/pytorch/pytorch/releases/tag/v2.14",
        "published_at": "2026-10-01T12:00:00Z", "body": "Compiler improvements.",
    }
    assert parse_feed(json.dumps(release).encode(), source)[0].title == "PyTorch 2.14"
    release["prerelease"] = True
    assert parse_feed(json.dumps(release).encode(), source) == []
    release["prerelease"] = False
    release["html_url"] = "https://github.com/unrelated/project/releases/tag/v2.14"
    assert parse_feed(json.dumps(release).encode(), source) == []
    source = next(source for source in SOURCES if source.id == "openai")
    assert parse_feed(_rss().replace(b"<category>Research</category>", b"<category>Company</category>"), source) == []
    source = next(source for source in SOURCES if source.id == "qwen")
    qwen = b'''<article><h2>Qwen model training update</h2><footer><span title="2026-10-01 00:00:00 +0800">October 1, 2026</span></footer>
    <a aria-label="post link to Qwen model training update" href="/blog/new-model/"></a></article>'''
    result = parse_headlines(qwen, source)
    assert result[0].title == "Qwen model training update"
    assert result[0].published_at is not None
    source = next(source for source in SOURCES if source.id == "deepseek")
    deepseek = b'''<nav><a href="/news/news261009">DeepSeek model Release 2026/10/09</a></nav>'''
    assert parse_headlines(deepseek, source)[0].published_at.date().isoformat() == "2026-10-09"


def test_page_reader_prefers_article_content_without_changing_default_web_sources():
    content = """<title>Research title</title><nav>Site menu</nav><main>
    <p>Other page content</p><article><h1>Research title</h1><p>The actual evidence.</p></article>
    <aside>Subscribe to the newsletter</aside></main><footer>Copyright notice</footer>"""
    normal = _ReadableHTMLParser()
    normal.feed(content)
    reader = _ReadableHTMLParser(prefer_main=True)
    reader.feed(content)
    assert "Site menu" in normal.text
    assert reader.title == "Research title"
    assert reader.text == "Research title\nThe actual evidence."
    empty_article = _ReadableHTMLParser(prefer_main=True)
    empty_article.feed("<main><article></article><p>Useful main content</p></main>")
    assert empty_article.text == "Useful main content"


@pytest.mark.anyio
async def test_refresh_keeps_previews_but_invalidates_a_changed_arxiv_revision(brief_app, monkeypatch):
    _, services, _, client = brief_app
    try:
        brief = services.brief
        paper = next(item for item in (await brief.refresh()).items if item.kind == "paper")
        await brief.fetch(paper.id)
        refreshed = await brief.refresh(force=True)
        assert next(item for item in refreshed.items if item.kind == "paper").id == paper.id
        assert paper.id in brief._previews
        original = services.source_downloads.fetch_resource

        async def revision(url, *, max_bytes):
            content, final, headers = await original(url, max_bytes=max_bytes)
            if "rss.arxiv" in url:
                content = content.replace(b"2610.01234v2", b"2610.01234v3")
            return content, final, headers

        monkeypatch.setattr(services.source_downloads, "fetch_resource", revision)
        revised = next(item for item in (await brief.refresh(force=True)).items if item.kind == "paper")
        assert revised.id != paper.id
        assert not revised.downloaded
        assert paper.id not in brief._previews
        with pytest.raises(NotFoundError):
            await brief.raw(paper.id)
    finally:
        await services.close()
        await client.aclose()


@pytest.mark.anyio
async def test_saved_paper_reuses_local_pdf_with_explicit_revision_caveat(brief_app):
    _, services, requests, client = brief_app
    try:
        services.documents.create_document_from_bytes(
            b"%PDF-1.7\nEarlier local revision", filename="2610.01234v1.pdf", title="Saved paper",
            metadata={"source_url": "https://arxiv.org/pdf/2610.01234v1"},
        )
        paper = next(item for item in (await services.brief.refresh()).items if item.kind == "paper")
        preview = await services.brief.fetch(paper.id)
        assert "earlier revision" in preview.item.preview_note
        assert (await services.brief.raw(paper.id))[0] == b"%PDF-1.7\nEarlier local revision"
        assert not any("arxiv.org/pdf/" in url for url in requests)
    finally:
        await services.close()
        await client.aclose()


@pytest.mark.anyio
async def test_preview_budget_fails_without_leaking_content(brief_app, monkeypatch):
    _, services, _, client = brief_app
    try:
        monkeypatch.setattr("backend.brief.service._PREVIEW_BUDGET", 4)
        paper = next(item for item in (await services.brief.refresh()).items if item.kind == "paper")
        with pytest.raises(ValidationError, match="previews are full"):
            await services.brief.fetch(paper.id)
        assert not services.brief._previews
        assert not services.documents.list_documents()
    finally:
        await services.close()
        await client.aclose()
