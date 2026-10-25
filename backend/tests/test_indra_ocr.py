from __future__ import annotations

import io
from threading import Event

import anyio
import pytest
from fastapi.testclient import TestClient
from indra_ocr import Line, Page
from PIL import Image, ImageDraw

from backend.app import create_app
from backend.bootstrap import create_services
from backend.core.errors import DocumentProcessingError
from backend.documents.indra import INDRA_QUALITY_NOTE, IndraOCR
from backend.documents.ocr import DocumentOCR

requires_models = pytest.mark.skipif(
    not IndraOCR().available(), reason="Optional Indra CPU models have not been downloaded",
)
HTML = '<div data-label="Text" data-bbox="0 0 1000 1000"><p>Surya evidence.</p></div>'


def scan(pages=1):
    image = Image.new("RGB", (1000, 1000), "white")
    draw = ImageDraw.Draw(image)
    draw.text((60, 100), "Research evidence", fill="black", font_size=50)
    draw.text((60, 200), "Local papers contain useful scientific findings.", fill="black", font_size=30)
    output = io.BytesIO()
    image.save(output, "PDF", save_all=True, append_images=[image] * (pages - 1))
    return output.getvalue()


def configure(settings, provider):
    settings.ocr_engine = "surya"
    settings.ocr_surya_base_url = provider.base_url + "/v1"
    settings.ocr_surya_model = "stub-model"
    settings.ocr_surya_parallel = 1


def test_indra_layout_and_rotated_figure_are_portable():
    output = io.BytesIO()
    Image.new("RGB", (100, 200), "blue").save(output, "PNG")
    result = Page(
        lines=[Line([0, 0, 10, 10], "Evidence", 0.99)],
        markdown="## Findings\n\nEvidence\n\n![](picture-0)",
        blocks=[
            {"label": "Section-Header", "bbox": [0, 0, 100, 10], "text": "Findings"},
            {"label": "Text", "bbox": [0, 10, 100, 20], "text": "Evidence"},
            {"label": "Picture", "bbox": [20, 10, 80, 80], "text": ""},
        ],
        rotation=90,
    )
    page = IndraOCR.format_page(result, output.getvalue(), 3)
    assert page["ocr_engine"] == "indra"
    assert page["ocr_quality_note"] == INDRA_QUALITY_NOTE
    assert "figures/page-0003-003.png" in page["text"]
    assert "picture-0" not in page["text"]
    assert page["layout_blocks"][2]["bbox"] == [100, 100, 400, 800]
    with Image.open(io.BytesIO(page["_figures"][0]["png"])) as crop:
        assert crop.size == (60, 70)


@pytest.mark.anyio
async def test_cancelling_cpu_work_keeps_capacity_until_inference_finishes(monkeypatch):
    ocr = IndraOCR()
    started, release = Event(), Event()
    scope = anyio.CancelScope()
    entered = []

    def recognize(_image, page):
        entered.append(page)
        if page == 1:
            started.set()
            assert release.wait(5)
        return {"text": f"Page {page}"}

    monkeypatch.setattr(ocr, "_recognize", recognize)

    async def first():
        with scope:
            await ocr.recognize(b"image", 1)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(first)
        try:
            assert await anyio.to_thread.run_sync(started.wait, 5)
            scope.cancel()
            tasks.start_soon(ocr.recognize, b"image", 2)
            await anyio.sleep(0)
            assert entered == [1]
            assert ocr._limiter.borrowed_tokens == 1
        finally:
            release.set()
    assert entered == [1, 2]
    assert ocr._limiter.borrowed_tokens == 0


@requires_models
def test_quick_cpu_api_works_without_surya_and_does_not_change_batch_engine(
    test_settings, stub_provider, monkeypatch,
):
    configure(test_settings, stub_provider)
    app = create_app(test_settings)
    monkeypatch.setattr(app.state.services.document_ocr.surya, "available", lambda: False)
    with TestClient(app) as client:
        uploaded = client.post("/api/documents", files={"file": ("scan.pdf", scan(), "application/pdf")})
        document_id = uploaded.json()["id"]
        options = client.get(f"/api/documents/{document_id}/ingestion-options").json()
        assert options["ocr_available"] is False
        assert options["quick_ocr_available"] is True
        response = client.post(f"/api/documents/{document_id}/ingest?mode=quick")
        assert response.status_code == 200, response.text
        saved = response.json()
        assert saved["status"] == "ready"
        assert saved["metadata"]["ocr_pages"] == saved["metadata"]["ocr_indra_pages"] == [1]
        assert saved["metadata"]["ocr_engine"] == "indra"
        assert "scientific findings" in " ".join(chunk["text"] for chunk in saved["chunks"]).lower()
        assert stub_provider.requests == []
        assert app.state.services.settings.ocr_engine == "surya"
        settings = client.put("/api/settings", json={"ocr_indra_fallback_enabled": True})
        assert settings.status_code == 200
        assert settings.json()["ocr_indra_fallback_enabled"] is True
        assert client.put("/api/settings", json={"ocr_indra_fallback_enabled": None}).json()[
            "ocr_indra_fallback_enabled"
        ] is True
        monkeypatch.setattr(app.state.services.document_ocr.surya, "available", lambda: True)
        stub_provider.reply = HTML
        upgraded = client.post(f"/api/documents/{document_id}/ingest?mode=ocr")
        assert upgraded.status_code == 200, upgraded.text
        assert upgraded.json()["metadata"]["ocr_engine"] == "surya"
        assert upgraded.json()["metadata"]["ocr_indra_pages"] == []
        assert len(stub_provider.requests) == 1
    recovered = create_services(test_settings)
    try:
        assert recovered.settings.ocr_indra_fallback_enabled is True
    finally:
        anyio.run(recovered.close)


@requires_models
@pytest.mark.anyio
async def test_batch_surya_recovers_only_failed_page_with_indra_and_reuses_it(
    test_settings, stub_provider, monkeypatch,
):
    configure(test_settings, stub_provider)
    test_settings.ocr_indra_fallback_enabled = True
    count = 0

    def reply(_payload):
        nonlocal count
        count += 1
        return HTML if count == 1 else "Malformed layout"

    monkeypatch.setattr(stub_provider, "_reply_for", reply)
    services = create_services(test_settings)
    try:
        document = services.documents.create_document_from_bytes(scan(2), filename="scan.pdf", title="Research")
        batch = await services.ocr_batches.start([document.id])
        with anyio.fail_after(20):
            while services.ocr_batches.get(batch["id"])["status"] in {"pending", "running"}:
                await anyio.sleep(0.01)
        finished = services.ocr_batches.get(batch["id"])
        assert finished["status"] == "completed"
        assert "Indra CPU pages 2" in finished["items"][0]["phase_label"]
        saved = services.documents.get_document(document.id)
        assert saved.metadata_json["ocr_pages"] == [1, 2]
        assert saved.metadata_json["ocr_indra_pages"] == [2]
        assert saved.metadata_json["ocr_engine"] == "mixed"
        assert "Surya failed" in saved.metadata_json["ocr_recovery_notes"]["2"]
        assert len(stub_provider.requests) == 3
        repeat = await services.ocr_batches.start([document.id])
        assert repeat["items"][0]["status"] == "skipped"
        assert len(stub_provider.requests) == 3
    finally:
        await services.close()


@pytest.mark.anyio
async def test_indra_failure_keeps_native_fallback_and_both_errors(
    test_settings, stub_provider, monkeypatch, tmp_path,
):
    configure(test_settings, stub_provider)
    test_settings.ocr_indra_fallback_enabled = True
    stub_provider.reply = "Malformed layout"
    source = tmp_path / "scan.pdf"
    source.write_bytes(scan())
    ocr = DocumentOCR(test_settings)
    monkeypatch.setattr(ocr, "_extract_native_text", lambda *_: "Previously readable native evidence.")

    async def fail_cpu(*_args):
        raise DocumentProcessingError("CPU recognizer failed")

    monkeypatch.setattr(ocr.indra, "recognize", fail_cpu)
    pages = await ocr.extract_pages(source, force_ocr=True)
    assert not pages[0]["ocr_used"]
    assert pages[0]["text"] == "Previously readable native evidence."
    assert "Surya" in pages[0]["ocr_fallback_note"]
    assert "CPU recognizer failed" in pages[0]["ocr_fallback_note"]


@pytest.mark.anyio
async def test_quick_cpu_missing_models_fails_before_mutating_document(test_settings, monkeypatch):
    services = create_services(test_settings)
    try:
        document = services.documents.create_document_from_bytes(scan(), filename="scan.pdf", title="Research")
        before = services.documents.get_document_artifacts(document.id)
        monkeypatch.setattr(services.document_ocr.indra, "available", lambda: False)
        with pytest.raises(DocumentProcessingError, match="Download"):
            await services.documents.ingest_document(document.id, quick_ocr=True)
        assert services.documents.get_document(document.id).status == "uploaded"
        assert [item.id for item in services.documents.get_document_artifacts(document.id)] == [
            item.id for item in before
        ]
    finally:
        await services.close()
