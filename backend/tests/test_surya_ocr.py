from __future__ import annotations

import base64
import io
import json

import anyio
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from backend.app import create_app
from backend.bootstrap import create_services
from backend.core.config import Settings
from backend.core.errors import DocumentProcessingError
from backend.documents.formatting import DocumentFormatter
from backend.documents.ocr import DocumentOCR
from backend.documents.surya import BLOCK_PROMPT, LAYOUT_PROMPT, PAGE_PROMPT, SuryaOCR, assemble_page, parse_page

HTML = """<div data-label="Page-Header" data-bbox="0 0 1000 40"><p>Journal header</p></div>
<div data-label="Section-Header" data-bbox="50 60 900 110"><h1>Research paper</h1></div>
<div data-label="Section-Header" data-bbox="50 120 450 160"><h2>Abstract</h2></div>
<div data-label="Text" data-bbox="50 170 450 350"><p>We preserve the reading order of two-column research papers.</p><p>These findings are part of the abstract.</p></div>
<div data-label="Section-Header" data-bbox="520 120 950 160"><h2>1 Introduction</h2></div>
<div data-label="Text" data-bbox="520 170 950 350"><p>This is body text, not the abstract.</p></div>
<div data-label="Table" data-bbox="50 400 950 550"><table><tr><th>Method</th><th>Score</th></tr><tr><td>Surya</td><td>95</td></tr></table></div>
<div data-label="Equation-Block" data-bbox="50 570 950 650"><math display="block">E = mc^2</math></div>
<div data-label="Figure" data-bbox="100 700 900 900"></div>
<div data-label="Caption" data-bbox="100 910 900 950"><p>Figure 1: Measured improvement.</p></div>
<div data-label="Page-Footer" data-bbox="400 960 600 990"><p>1</p></div>"""


@pytest.fixture
def anyio_backend():
    return "asyncio"


def png() -> bytes:
    image = Image.new("RGB", (800, 1000), "white")
    ImageDraw.Draw(image).rectangle((80, 700, 720, 900), fill="blue")
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def pdf(*colors: str) -> bytes:
    pages = [Image.new("RGB", (400, 500), color) for color in colors or ("white",)]
    output = io.BytesIO()
    pages[0].save(output, "PDF", save_all=True, append_images=pages[1:])
    return output.getvalue()


def configure(settings, provider):
    settings.ocr_engine = "surya"
    settings.ocr_surya_base_url = provider.base_url + "/v1"
    settings.ocr_surya_model = "stub-model"
    provider.reply = HTML


def test_markdown_layout_math_table_and_source_figure(test_settings):
    page = assemble_page(parse_page(HTML), png(), 1)
    assert "Journal header" not in page["text"]
    assert "# Research paper" in page["text"]
    assert "## Abstract" in page["text"]
    assert "| Method | Score |" in page["text"]
    assert "$$\nE = mc^2\n$$" in page["text"]
    assert "![Figure on page 1](figures/page-0001-009.png)" in page["text"]
    assert page["layout_blocks"][0]["excluded_from_text"]
    assert page["bbox_coordinate_system"] == "normalized_0_1000"
    with Image.open(io.BytesIO(page["_figures"][0]["png"])) as crop:
        assert crop.size == (640, 200)
        assert crop.getpixel((100, 100)) == (0, 0, 255)
    chunks = DocumentFormatter(test_settings).chunk_pages([{**page, "page": 1}], title="Paper")
    abstract = next(chunk for chunk in chunks if chunk["section_title"] == "Abstract")
    assert abstract["text"] == (
        "We preserve the reading order of two-column research papers.\n\n"
        "These findings are part of the abstract."
    )
    assert abstract["citation"] == "p.1"
    assert any("| Method | Score |\n| --- | --- |" in chunk["text"] for chunk in chunks)


def test_html_sanitization_and_spanning_cells():
    html = """<div data-label="Table" data-bbox="0 0 1000 1000">
      <script>alert(1)</script><img src="https://tracker.invalid/a"/>
      <table onclick="alert(1)"><tr><th colspan="2">Combined</th></tr>
      <tr><td><math>x_1</math></td><td><a href="javascript:alert(1)">value</a></td></tr></table>
      </div>"""
    result = assemble_page(parse_page(html), png(), 1)["text"]
    assert '<th colspan="2">Combined</th>' in result
    assert "alert" not in result and "tracker" not in result and "<img" not in result
    assert "value" in result


@pytest.mark.parametrize("html", [
    '<div data-label="Text" data-bbox="0 0 1000 1000"></div>',
    '<p>Lost text</p><div data-label="Text" data-bbox="0 0 1000 1000">Body</div>',
])
def test_malformed_layout_cannot_silently_discard_text(html):
    with pytest.raises(DocumentProcessingError):
        assemble_page(parse_page(html), png(), 1)


@pytest.mark.parametrize("box", ["NaN 0 100 100", "0 0 1001 900", "50 50 40 60", "0 0 0 0", "0 1 2", None])
def test_invalid_geometry_is_not_used_to_crop(box):
    with pytest.raises(DocumentProcessingError):
        parse_page(f'<div data-label="Figure" data-bbox="{box}"></div>')


@pytest.mark.anyio
async def test_surya_ingestion_persists_markdown_images_and_abstract(test_settings, stub_provider):
    configure(test_settings, stub_provider)
    services = create_services(test_settings)
    try:
        source = pdf("white")
        document = services.documents.create_document_from_bytes(source, filename="paper.pdf", title="Paper")
        await services.documents.ingest_document(document.id)
        saved = services.documents.get_document(document.id)
        assert saved.status == "ready"
        assert services.documents.document_abstracts([saved])[document.id] == (
            "We preserve the reading order of two-column research papers. These findings are part of the abstract."
        )
        artifacts = services.documents.get_document_artifacts(document.id)
        figures = [artifact for artifact in artifacts if artifact.kind == "extracted_image"]
        assert len(figures) == 1
        assert figures[0].metadata_json["markdown_path"] == "figures/page-0001-009.png"
        manifest = services.documents.artifact_content(next(a for a in artifacts if a.kind == "extracted_manifest"))
        assert manifest["pages"][0]["layout_blocks"][8]["artifact_id"] == figures[0].id
        assert "_figures" not in manifest["pages"][0]
        assert services.documents.artifact_bytes(next(a for a in artifacts if a.kind == "source_pdf")) == source
        request = stub_provider.requests[0]
        assert request["model"] == "stub-model"
        assert request["temperature"] == 0 and request["top_p"] == 0.1
        assert request["max_tokens"] == 8192
        assert request["messages"][0]["content"][1]["text"] == PAGE_PROMPT
        assert request["messages"][0]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")

        # Reprocessing replaces generated figures without deleting user notes or summaries.
        old_path = services.documents.repository.artifact_path(figures[0])
        stub_provider.reply = HTML.replace(
            '<div data-label="Figure" data-bbox="100 700 900 900"></div>', "",
        )
        await services.documents.ingest_document(document.id, force_ocr=True)
        assert not old_path.exists()
        assert not any(a.kind == "extracted_image" for a in services.documents.get_document_artifacts(document.id))
    finally:
        await services.close()


@pytest.mark.anyio
async def test_truncated_full_page_recovers_with_layout_and_blocks(test_settings, stub_provider, monkeypatch):
    configure(test_settings, stub_provider)
    original = stub_provider.responses

    def respond(payload):
        prompt = payload["messages"][0]["content"][1]["text"]
        response = original(payload)
        choice = response["choices"][0]
        if prompt == PAGE_PROMPT:
            choice["finish_reason"] = "length"
        elif prompt == LAYOUT_PROMPT:
            choice["message"]["content"] = json.dumps([
                {"label": "Section-Header", "bbox": "20 20 900 100", "count": 50},
                {"label": "Text", "bbox": "20 100 900 800", "count": 500},
                {"label": "Figure", "bbox": "20 800 900 990", "count": 0},
            ])
        else:
            assert prompt == BLOCK_PROMPT
            choice["message"]["content"] = "<p>Recovered content</p>"
        return response

    monkeypatch.setattr(stub_provider, "responses", respond)
    result = await SuryaOCR(test_settings).recognize(png(), 1)
    assert result["text"].startswith("## Recovered content")
    assert "length" in result["ocr_recovery_note"]
    assert len(result["_figures"]) == 1
    assert len(stub_provider.requests) == 4
    assert stub_provider.requests[1]["response_format"]["type"] == "json_schema"


@pytest.mark.anyio
async def test_small_papers_share_surya_capacity_and_keep_their_page_order(test_settings, stub_provider, tmp_path, monkeypatch):
    configure(test_settings, stub_provider)
    test_settings.ocr_surya_parallel = 3
    stub_provider.completion_delay_seconds = 0.2
    sources = [tmp_path / "red-green.pdf", tmp_path / "blue-red.pdf"]
    sources[0].write_bytes(pdf("red", "green"))
    sources[1].write_bytes(pdf("blue", "red"))

    def reply(payload):
        encoded = payload["messages"][0]["content"][0]["image_url"]["url"].split(",")[1]
        with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
            color = max(range(3), key=lambda i: image.getpixel((0, 0))[i])
        return f'<div data-label="Text" data-bbox="0 0 1000 1000"><p>Color {color}</p></div>'

    monkeypatch.setattr(stub_provider, "_reply_for", reply)
    ocr = DocumentOCR(test_settings)
    results = {}
    progress = {source: [] for source in sources}

    async def collect(source):
        async def report(event):
            progress[source].append(event["completed_pages"])
        results[source] = await ocr.extract_pages(source, progress=report)

    async with anyio.create_task_group() as tasks:
        for source in sources:
            tasks.start_soon(collect, source)
    assert stub_provider.max_active_inference == test_settings.ocr_surya_parallel
    assert len(stub_provider.requests) == 4
    assert ocr._surya_limiter.borrowed_tokens == 0
    for source, colors in zip(sources, [(0, 1), (2, 0)], strict=True):
        pages = results[source]
        assert len(pages) < test_settings.ocr_surya_parallel
        assert [p["page"] for p in pages] == [1, 2]
        assert [p["text"] for p in pages] == [f"Color {color}" for color in colors]
        assert progress[source] == [0, 1, 2]


@pytest.mark.anyio
async def test_surya_reads_selectable_pages_unless_embedded_mode_is_explicit(test_settings, stub_provider, tmp_path, monkeypatch):
    configure(test_settings, stub_provider)
    source = tmp_path / "selectable.pdf"
    source.write_bytes(pdf())
    native = "Native PDF text in the wrong reading order. " * 5
    ocr = DocumentOCR(test_settings)
    monkeypatch.setattr(ocr, "_extract_native_text", lambda *_: native)
    embedded = await ocr.extract_pages(source, use_layout=False)
    assert embedded[0]["text"] == native and not embedded[0]["ocr_used"]
    assert stub_provider.requests == []
    structured = await ocr.extract_pages(source)
    assert structured[0]["ocr_used"]
    assert structured[0]["content_format"] == "markdown"

    stub_provider.reply = "invalid layout"
    fallback = await ocr.extract_pages(source)
    assert fallback[0]["text"] == native
    assert not fallback[0]["ocr_used"]
    assert "embedded text was used instead" in fallback[0]["ocr_fallback_note"]


@pytest.mark.anyio
async def test_cancelling_ocr_stops_queued_pages_and_releases_capacity(test_settings, stub_provider, tmp_path):
    configure(test_settings, stub_provider)
    test_settings.ocr_surya_parallel = 1
    stub_provider.completion_delay_seconds = 0.5
    source = tmp_path / "cancel.pdf"
    source.write_bytes(pdf("red", "green", "blue"))
    ocr = DocumentOCR(test_settings)
    completed = False
    with anyio.CancelScope() as scope:
        async def cancel():
            assert await anyio.to_thread.run_sync(stub_provider.request_started.wait, 5)
            scope.cancel()
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(cancel)
            await ocr.extract_pages(source)
            completed = True
    assert not completed
    assert ocr._surya_limiter.borrowed_tokens == 0
    await anyio.sleep(0.6)
    assert len(stub_provider.requests) <= 1


@pytest.mark.anyio
async def test_failed_surya_reprocessing_keeps_previous_extraction(test_settings, stub_provider):
    configure(test_settings, stub_provider)
    services = create_services(test_settings)
    try:
        document = services.documents.create_document_from_bytes(pdf(), filename="scan.pdf", title="Scan")
        await services.documents.ingest_document(document.id)
        old = {a.id: services.documents.artifact_bytes(a) for a in services.documents.get_document_artifacts(document.id)}
        stub_provider.reply = "malformed response"
        with pytest.raises(DocumentProcessingError, match="No readable text"):
            await services.documents.ingest_document(document.id, force_ocr=True)
        assert services.document_ocr._surya_limiter.borrowed_tokens == 0
        assert {a.id: services.documents.artifact_bytes(a) for a in services.documents.get_document_artifacts(document.id)} == old
    finally:
        await services.close()


def test_surya_parallel_configuration_accepts_server_capacity(test_settings):
    settings = Settings.model_validate({**test_settings.model_dump(), "ocr_surya_parallel": 48})
    assert settings.ocr_surya_parallel == 48


def test_settings_and_ingestion_options_use_surya(test_settings, stub_provider, monkeypatch):
    stub_provider.reply = HTML
    monkeypatch.setattr(
        DocumentOCR, "_extract_native_text",
        staticmethod(lambda *_: "Selectable PDF text with enough readable characters for native extraction."),
    )
    with TestClient(create_app(test_settings)) as client:
        settings = {
            "ocr_engine": "surya", "ocr_surya_base_url": stub_provider.base_url + "/v1",
            "ocr_surya_model": "stub-model", "ocr_surya_parallel": 48, "ocr_surya_max_tokens": 4096,
            "ocr_llm_model": None, "ocr_llm_triage_model": None,
        }
        for parallel in (16, 48):
            settings["ocr_surya_parallel"] = parallel
            response = client.put("/api/settings", json=settings)
            assert response.status_code == 200, response.text
            assert all(response.json()[key] == value for key, value in settings.items())
        for invalid in (
            {"ocr_surya_base_url": "https://external.invalid/v1"}, {"ocr_surya_parallel": 0},
            {"ocr_surya_parallel": -1}, {"ocr_surya_parallel": 1.5},
            {"ocr_surya_base_url": "http://localhost:invalid/v1"},
            {"ocr_surya_max_tokens": 0}, {"ocr_engine": "unknown"},
        ):
            assert client.put("/api/settings", json=invalid).status_code == 422
        uploaded = client.post("/api/documents", files={"file": ("scan.pdf", pdf(), "application/pdf")}).json()
        options = client.get(f"/api/documents/{uploaded['id']}/ingestion-options").json()
        assert options["ocr_engine"] == "surya" and options["recommended_mode"] == "ocr"
        assert options["ocr_available"]
        ingested = client.post(f"/api/documents/{uploaded['id']}/ingest?mode=embedded")
        assert ingested.status_code == 200, ingested.text
        assert stub_provider.requests == []
        ingested = client.post(f"/api/documents/{uploaded['id']}/ingest")
        assert ingested.status_code == 200, ingested.text
        assert ingested.json()["metadata"]["ocr_pages"] == [1]
        assert ingested.json()["abstract"].startswith("We preserve the reading order")
    with TestClient(create_app(test_settings)) as client:
        assert client.get("/api/settings").json()["ocr_surya_parallel"] == 48
