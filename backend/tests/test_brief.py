from __future__ import annotations

import asyncio
from datetime import timedelta
import json
from xml.sax.saxutils import escape

import httpx
import pytest

from backend.app import create_app
from backend.brief.catalog import SOURCES
from backend.brief.collection import parse_feed, parse_headlines
from backend.brief.schemas import BriefPreferences
from backend.core.errors import NotFoundError, ValidationError
from backend.research.sources import _ReadableHTMLParser
from backend.utils import utcnow


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
        from backend.brief.service import BriefService
        restarted = BriefService(services.storage, services.source_downloads, services.documents, services.workspace)
        assert restarted.catalog().preferences.exclude_terms == ["tool use"]
        assert not (await restarted.edition()).items
        brief._edition.day = (utcnow() - timedelta(days=2)).date().isoformat()
        assert (await brief.edition()).day is None
        assert not brief._dismissed
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
