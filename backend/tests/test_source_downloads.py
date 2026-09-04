from __future__ import annotations

import httpx
import pytest

from backend.bootstrap import create_services
from backend.research.sources import (
    SourceDownloadService,
    WebSourceUnavailable,
    _is_public_address,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _remote_client() -> httpx.AsyncClient:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/paper.pdf":
            return httpx.Response(
                200,
                content=b"%PDF-1.7\nremote paper",
                headers={"Content-Type": "application/pdf"},
            )
        if request.url.path == "/article":
            assert request.headers["user-agent"].startswith("Mozilla/5.0")
            return httpx.Response(
                200,
                text="""
                <html>
                  <head>
                    <title>Useful Article</title>
                    <style>ignored style</style>
                  </head>
                  <body>
                    <main>
                      <h1>Useful Article</h1>
                      <p>Transformers use attention to process context.</p>
                      <p>This evidence should be searchable in chat.</p>
                    </main>
                    <script>ignored script</script>
                  </body>
                </html>
                """,
                headers={"Content-Type": "text/html; charset=utf-8"},
            )
        if request.url.path == "/blocked":
            return httpx.Response(403, text="Access denied")
        if request.url.path == "/empty":
            return httpx.Response(
                200,
                text="<html><head><title>Empty</title></head><body></body></html>",
                headers={"Content-Type": "text/html; charset=utf-8"},
            )
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(respond))


@pytest.mark.anyio
async def test_remote_pdf_is_persisted_ingested_and_searchable(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = create_services(test_settings)
    client = _remote_client()
    downloads = SourceDownloadService(
        test_settings,
        services.documents,
        services.workspace,
        client=client,
    )

    async def allow_public_url(_url: str) -> None:
        return None

    async def ingestion_options(_document_id: str) -> dict[str, object]:
        return {"recommended_mode": "embedded", "ocr_available": True}

    async def ingest(document_id: str, *, force_ocr: bool = False) -> dict[str, object]:
        assert force_ocr is False
        services.retrieval.replace_document_chunks(
            document_id,
            [
                {
                    "section_title": "Abstract",
                    "page_start": 1,
                    "page_end": 1,
                    "citation": "p.1",
                    "text": "Remote PDF full text about sparse attention.",
                }
            ],
        )
        services.document_repository.mark_ready(
            document_id,
            page_count=1,
            metadata={"source": "test"},
        )
        return {"document_id": document_id}

    monkeypatch.setattr(downloads, "_validate_public_url", allow_public_url)
    monkeypatch.setattr(services.documents, "ingestion_options", ingestion_options)
    monkeypatch.setattr(services.documents, "ingest_document", ingest)

    try:
        document = await downloads.download_pdf(
            "https://papers.test/paper.pdf",
            title="Sparse Attention",
        )
        duplicate = await downloads.download_pdf(
            "https://papers.test/paper.pdf",
            title="Sparse Attention",
        )
    finally:
        await downloads.close()
        await client.aclose()
        await services.close()

    assert document.status == "ready"
    assert duplicate.id == document.id
    assert document.metadata_json["source_url"] == "https://papers.test/paper.pdf"
    assert document.metadata_json["source_kind"] == "remote_pdf"
    assert services.retrieval.keyword_search("sparse attention", document.id)[0]["citation"] == "p.1"
    assert (test_settings.documents_dir / document.id / "source" / "paper.pdf").is_file()


@pytest.mark.anyio
async def test_pdf_acquisition_reuses_local_paper_across_arxiv_versions(
    test_settings,
) -> None:
    services = create_services(test_settings)
    existing = services.documents.create_document_from_bytes(
        b"%PDF-1.7\nlocal paper",
        filename="1006.3498v1.pdf",
        title="A Local Paper",
        metadata={"source_url": "https://arxiv.org/pdf/1006.3498v1"},
    )
    client = _remote_client()
    downloads = SourceDownloadService(
        test_settings,
        services.documents,
        services.workspace,
        client=client,
    )

    try:
        reused = await downloads.download_pdf(
            "https://arxiv.org/pdf/1006.3498v2.pdf",
            title="A Local Paper",
        )
    finally:
        await downloads.close()
        await client.aclose()
        await services.close()

    assert reused.id == existing.id
    assert reused.status == "uploaded"


@pytest.mark.anyio
async def test_html_page_is_temporary_searchable_and_notes_persist(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = create_services(test_settings)
    client = _remote_client()
    downloads = SourceDownloadService(
        test_settings,
        services.documents,
        services.workspace,
        client=client,
    )

    async def allow_public_url(_url: str) -> None:
        return None

    monkeypatch.setattr(downloads, "_validate_public_url", allow_public_url)
    try:
        source = await downloads.download_web_page("https://example.com/article")
        matches = await downloads.search_web_source(source.id, "attention context")
        note = await downloads.save_web_note(
            source.id,
            name="Attention article",
            content="Attention processes context.",
            tags=["attention"],
        )
        assert await downloads.delete_web_source(source.id) is True
        with pytest.raises(ValueError, match="expired"):
            await downloads.get_web_source(source.id)
    finally:
        await downloads.close()
        await client.aclose()
        await services.close()

    assert source.title == "Useful Article"
    assert "ignored script" not in source.text
    assert matches[0]["citation"] == "https://example.com/article"
    persisted = services.workspace.read_file(note.path)
    assert "[Useful Article](https://example.com/article)" in persisted.content
    assert "Attention processes context." in persisted.content
    assert {"note", "web-source", "attention"} <= set(persisted.tags)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("blocked", "HTTP 403"),
        ("empty", "did not contain readable text"),
    ],
)
async def test_unavailable_web_pages_are_classified_without_a_system_failure(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    message: str,
) -> None:
    services = create_services(test_settings)
    client = _remote_client()
    downloads = SourceDownloadService(
        test_settings,
        services.documents,
        services.workspace,
        client=client,
    )

    async def allow_public_url(_url: str) -> None:
        return None

    monkeypatch.setattr(downloads, "_validate_public_url", allow_public_url)
    try:
        with pytest.raises(WebSourceUnavailable, match=message):
            await downloads.download_web_page(f"https://example.com/{path}")
    finally:
        await downloads.close()
        await client.aclose()
        await services.close()


@pytest.mark.anyio
async def test_private_source_urls_are_rejected(test_settings) -> None:
    services = create_services(test_settings)
    downloads = SourceDownloadService(
        test_settings,
        services.documents,
        services.workspace,
    )
    try:
        with pytest.raises(ValueError, match="private or local"):
            await downloads._validate_public_url("http://127.0.0.1/private")
    finally:
        await downloads.close()
        await services.close()


def test_ipv4_mapped_private_addresses_are_rejected() -> None:
    assert _is_public_address("::ffff:127.0.0.1") is False
    assert _is_public_address("::ffff:169.254.169.254") is False


def test_connected_private_peer_is_rejected() -> None:
    class PrivateStream:
        @staticmethod
        def get_extra_info(name: str):
            assert name == "server_addr"
            return ("127.0.0.1", 443)

    downloads = object.__new__(SourceDownloadService)
    downloads._require_peer_validation = True
    response = httpx.Response(
        200,
        extensions={"network_stream": PrivateStream()},
    )

    with pytest.raises(ValueError, match="private or local"):
        downloads._validate_connected_peer(response)
