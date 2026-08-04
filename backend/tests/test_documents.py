from __future__ import annotations

import base64
import shutil

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app import create_app, create_backend_services
from backend.documents import DocumentProcessingError
from backend.ollama import OllamaError
from backend.models import Artifact


def test_artifact_records_are_idempotent_by_owned_path(test_settings) -> None:
    services = create_backend_services(test_settings)
    path = "workspace/result.md"
    first_file = services.storage.write_text(test_settings.artifacts_dir, path, "first")
    first = services.documents.create_artifact_record(
        owner_type="workspace",
        kind="markdown",
        relative_path=path,
        media_type="text/markdown",
        stored=first_file,
    )

    updated_file = services.storage.write_text(test_settings.artifacts_dir, path, "updated")
    updated = services.documents.create_artifact_record(
        owner_type="workspace",
        kind="markdown",
        relative_path=path,
        media_type="text/markdown",
        stored=updated_file,
        metadata={"revision": 2},
    )

    assert updated.id == first.id
    assert updated.sha256 == updated_file.sha256
    assert updated.metadata_json == {"revision": 2, "storage_area": "artifacts"}
    with services.session_factory() as session:
        assert len(list(session.scalars(select(Artifact).where(Artifact.relative_path == path)))) == 1

    with pytest.raises(ValueError, match="already owned by another resource"):
        services.documents.create_artifact_record(
            owner_type="different-owner",
            kind="markdown",
            relative_path=path,
            media_type="text/markdown",
            stored=updated_file,
        )


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="Tesseract is not installed")
def test_image_only_pdf_uses_ocr(test_settings, tmp_path) -> None:
    image_module = pytest.importorskip("PIL.Image")
    draw_module = pytest.importorskip("PIL.ImageDraw")
    pytest.importorskip("pypdfium2")
    pytest.importorskip("pytesseract")

    image = image_module.new("RGB", (1400, 500), "white")
    draw = draw_module.Draw(image)
    draw.text((80, 180), "Local OCR research paper verification", fill="black", font_size=48)
    pdf_path = tmp_path / "scanned.pdf"
    image.save(pdf_path, "PDF", resolution=150)

    client = TestClient(create_app(test_settings))
    with pdf_path.open("rb") as pdf:
        upload = client.post(
            "/api/documents",
            files={"file": ("scanned.pdf", pdf, "application/pdf")},
        )
    assert upload.status_code == 200

    document_id = upload.json()["id"]
    ingested = client.post(f"/api/documents/{document_id}/ingest")

    assert ingested.status_code == 200
    payload = ingested.json()
    assert payload["status"] == "ready"
    assert payload["metadata"]["ocr_pages"] == [1]
    assert payload["metadata"]["figure_count"] == 1
    extracted = "\n".join(chunk["text"] for chunk in payload["chunks"]).lower()
    assert "local ocr research paper" in extracted
    assert "/api/artifacts/" in extracted

    reingested = client.post(f"/api/documents/{document_id}/ingest")
    assert reingested.status_code == 200
    assert len(reingested.json()["artifacts"]) == 4


@pytest.mark.anyio
async def test_good_ocr_skips_the_rewrite_model(test_settings) -> None:
    services = create_backend_services(test_settings)

    class FakeOllama:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def generate(self, model: str, prompt: str, **kwargs: object) -> dict[str, str]:
            self.calls.append({"model": model, "prompt": prompt, **kwargs})
            return {"response": '{"quality":"good","issues":[]}'}

    fake_ollama = FakeOllama()
    services.documents.ollama = fake_ollama  # type: ignore[assignment]
    image = b"rendered-page"
    figure_path = "/api/artifacts/figure-id/raw"
    pages = [
        {
            "page": 1,
            "raw_text": "A B\n1 2",
            "text": "A B\n1 2",
            "ocr_used": True,
            "llm_enhanced": False,
            "_image_png": image,
            "figures": [{"path": figure_path, "alt": "Extracted figure 1 from page 1"}],
        }
    ]

    progress_events: list[dict[str, object]] = []

    async def collect_progress(payload: dict[str, object]) -> None:
        progress_events.append(payload)

    await services.documents._enhance_pages(
        pages,
        "big-vision-model",
        triage_model="small-vision-model",
        progress=collect_progress,
    )

    assert len(fake_ollama.calls) == 1
    assert fake_ollama.calls[0]["model"] == "small-vision-model"
    assert fake_ollama.calls[0]["format_"]["properties"]["quality"]["enum"] == [
        "good",
        "average",
        "poor",
    ]
    assert fake_ollama.calls[0]["images"] == [base64.b64encode(image).decode("ascii")]
    assert "A B\n1 2" in str(fake_ollama.calls[0]["prompt"])
    assert pages[0]["ocr_quality"] == "good"
    assert pages[0]["llm_enhanced"] is False
    assert pages[0]["text"].startswith("A B\n1 2")
    assert figure_path in pages[0]["text"]
    assert "llm_validation_status" not in pages[0]
    assert progress_events[0] == {
        "phase": "ocr_triage",
        "phase_label": "Checking OCR quality",
        "completed_pages": 0,
        "total_pages": 1,
    }
    assert progress_events[-1]["phase"] == "ocr_triage"
    assert progress_events[-1]["page_quality"] == "good"
    assert progress_events[-1]["pages_needing_repair"] == 0


@pytest.mark.anyio
async def test_average_ocr_keeps_the_original_text(test_settings) -> None:
    services = create_backend_services(test_settings)

    class FakeOllama:
        def __init__(self) -> None:
            self.calls = 0

        async def generate(self, model: str, prompt: str, **kwargs: object) -> dict[str, str]:
            self.calls += 1
            return {"response": '{"quality":"average","issues":["Table borders were lost."]}'}

    fake_ollama = FakeOllama()
    services.documents.ollama = fake_ollama  # type: ignore[assignment]
    pages = [
        {
            "page": 1,
            "raw_text": "Usable OCR content",
            "text": "Usable OCR content",
            "ocr_used": True,
            "llm_enhanced": False,
            "_image_png": b"rendered-page",
            "figures": [],
        }
    ]

    await services.documents._enhance_pages(pages, "big-vision-model", triage_model="small-vision-model")

    assert fake_ollama.calls == 1
    assert pages[0]["ocr_quality"] == "average"
    assert pages[0]["ocr_quality_issues"] == ["Table borders were lost."]
    assert pages[0]["text"] == "Usable OCR content"
    assert pages[0]["llm_enhanced"] is False


@pytest.mark.anyio
async def test_poor_ocr_is_rewritten_and_revalidated(test_settings) -> None:
    services = create_backend_services(test_settings)

    class FakeOllama:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def generate(self, model: str, prompt: str, **kwargs: object) -> dict[str, str]:
            self.calls.append({"model": model, "prompt": prompt, **kwargs})
            if model == "small-vision-model":
                if "CANDIDATE MARKDOWN" in prompt:
                    return {"response": '{"quality":"good","issues":[]}'}
                return {"response": '{"quality":"poor","issues":["Columns are interleaved."]}'}
            return {"response": "## Results\n\n| A | B |\n|---|---|\n| 1 | 2 |"}

    fake_ollama = FakeOllama()
    services.documents.ollama = fake_ollama  # type: ignore[assignment]
    image = b"rendered-page"
    figure_path = "/api/artifacts/figure-id/raw"
    pages = [
        {
            "page": 1,
            "raw_text": "A B\n1 2",
            "text": "A B\n1 2",
            "ocr_used": True,
            "llm_enhanced": False,
            "_image_png": image,
            "figures": [{"path": figure_path, "alt": "Extracted figure 1 from page 1"}],
        }
    ]

    progress_events: list[dict[str, object]] = []

    async def collect_progress(payload: dict[str, object]) -> None:
        progress_events.append(payload)

    await services.documents._enhance_pages(
        pages,
        "big-vision-model",
        triage_model="small-vision-model",
        progress=collect_progress,
    )

    assert [call["model"] for call in fake_ollama.calls] == [
        "small-vision-model",
        "big-vision-model",
        "small-vision-model",
    ]
    assert "Columns are interleaved." in str(fake_ollama.calls[1]["prompt"])
    assert "format_" not in fake_ollama.calls[1]
    assert fake_ollama.calls[1]["images"] == [base64.b64encode(image).decode("ascii")]
    assert pages[0]["ocr_quality"] == "poor"
    assert pages[0]["llm_enhanced"] is True
    assert pages[0]["llm_validation_status"] == "passed"
    assert "| A | B |" in pages[0]["text"]
    assert figure_path in pages[0]["text"]
    chunks = services.documents._chunk_pages(pages, title="Test")
    assert any("| A | B |\n|---|---|\n| 1 | 2 |" in chunk["text"] for chunk in chunks)
    phases = [event["phase"] for event in progress_events]
    assert phases[0] == "ocr_triage"
    assert "llm_enhancement" in phases
    assert progress_events[-1]["phase"] == "llm_validation"
    assert progress_events[-1]["eta_seconds"] == 0.0


@pytest.mark.anyio
async def test_empty_ocr_is_rewritten_without_a_triage_call(test_settings) -> None:
    services = create_backend_services(test_settings)

    class FakeOllama:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def generate(self, model: str, prompt: str, **kwargs: object) -> dict[str, str]:
            self.calls.append(model)
            if model == "small-vision-model":
                return {"response": '{"quality":"good","issues":[]}'}
            return {"response": "# Recovered heading"}

    fake_ollama = FakeOllama()
    services.documents.ollama = fake_ollama  # type: ignore[assignment]
    pages = [
        {
            "page": 1,
            "raw_text": "",
            "text": "",
            "ocr_used": True,
            "llm_enhanced": False,
            "_image_png": b"rendered-page",
            "figures": [],
        }
    ]

    await services.documents._enhance_pages(pages, "big-vision-model", triage_model="small-vision-model")

    assert fake_ollama.calls == ["big-vision-model", "small-vision-model"]
    assert pages[0]["ocr_quality"] == "poor"
    assert pages[0]["ocr_quality_issues"] == ["OCR produced no text for this page."]
    assert pages[0]["text"] == "# Recovered heading"


@pytest.mark.anyio
async def test_failed_triage_request_falls_back_to_rewriting(test_settings) -> None:
    services = create_backend_services(test_settings)

    class FlakyOllama:
        async def generate(self, model: str, prompt: str, **kwargs: object) -> dict[str, str]:
            if model == "small-vision-model":
                raise OllamaError("connection reset")
            return {"response": "# Rewritten page"}

    services.documents.ollama = FlakyOllama()  # type: ignore[assignment]
    pages = [
        {
            "page": 1,
            "raw_text": "Original OCR content",
            "text": "Original OCR content",
            "ocr_used": True,
            "llm_enhanced": False,
            "_image_png": b"rendered-page",
            "figures": [],
        }
    ]

    await services.documents._enhance_pages(pages, "big-vision-model", triage_model="small-vision-model")

    assert pages[0]["ocr_quality"] == "poor"
    assert "OCR quality check failed" in pages[0]["ocr_quality_issues"][0]
    assert pages[0]["llm_enhanced"] is False
    assert pages[0]["llm_validation_status"] == "failed"
    assert pages[0]["text"] == "Original OCR content"


@pytest.mark.anyio
async def test_llm_rewrite_falls_back_to_ocr_when_response_is_empty(test_settings) -> None:
    services = create_backend_services(test_settings)

    class EmptyOllama:
        async def generate(self, model: str, prompt: str, **kwargs: object) -> dict[str, str]:
            if model == "small-vision-model":
                return {"response": '{"quality":"poor","issues":["Text is scrambled."]}'}
            return {"response": "", "thinking": "internal reasoning only"}

    services.documents.ollama = EmptyOllama()  # type: ignore[assignment]
    pages = [
        {
            "page": 1,
            "raw_text": "Original OCR content",
            "text": "Original OCR content",
            "ocr_used": True,
            "llm_enhanced": False,
            "_image_png": b"rendered-page",
            "figures": [],
        }
    ]

    await services.documents._enhance_pages(
        pages,
        "thinking-vision-model",
        triage_model="small-vision-model",
    )

    assert pages[0]["text"] == "Original OCR content"
    assert pages[0]["llm_enhanced"] is False
    assert pages[0]["llm_enhancement_note"] == (
        "No LLM enhancement was applied because the model returned only an internal "
        "thinking trace. The original OCR output is used directly."
    )
    markdown = services.documents._build_markdown("Paper", pages)
    assert "**OCR note:** No LLM enhancement was applied" in markdown


@pytest.mark.anyio
async def test_validation_rejects_unfaithful_rewrite(test_settings) -> None:
    services = create_backend_services(test_settings)

    class RejectingOllama:
        async def generate(self, model: str, prompt: str, **kwargs: object) -> dict[str, str]:
            if model == "small-vision-model":
                if "CANDIDATE MARKDOWN" in prompt:
                    return {
                        "response": (
                            '{"quality":"poor","issues":'
                            '["Invented accuracy claim not present on the page."]}'
                        )
                    }
                return {"response": '{"quality":"poor","issues":["Text is scrambled."]}'}
            return {"response": "# Invented Results\n\nAccuracy was 100%."}

    services.documents.ollama = RejectingOllama()  # type: ignore[assignment]
    pages = [
        {
            "page": 1,
            "raw_text": "Original OCR content",
            "text": "Original OCR content",
            "ocr_used": True,
            "llm_enhanced": False,
            "_image_png": b"rendered-page",
            "figures": [],
        }
    ]

    await services.documents._enhance_pages(
        pages,
        "big-vision-model",
        triage_model="small-vision-model",
    )

    assert pages[0]["text"] == "Original OCR content"
    assert pages[0]["llm_enhanced"] is False
    assert pages[0]["llm_validation_status"] == "failed"
    assert pages[0]["llm_validation_issues"] == [
        "Invented accuracy claim not present on the page."
    ]
    assert "validation check rejected it" in pages[0]["llm_enhancement_note"]


@pytest.mark.anyio
async def test_forced_repair_skips_triage(test_settings) -> None:
    services = create_backend_services(test_settings)

    class FakeOllama:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def generate(self, model: str, prompt: str, **kwargs: object) -> dict[str, str]:
            self.calls.append(model)
            if model == "small-vision-model":
                return {"response": '{"quality":"good","issues":[]}'}
            return {"response": "# Manually rewritten"}

    fake_ollama = FakeOllama()
    services.documents.ollama = fake_ollama  # type: ignore[assignment]
    pages = [
        {
            "page": 1,
            "raw_text": "Original OCR content",
            "text": "Original OCR content",
            "ocr_used": True,
            "llm_enhanced": False,
            "_image_png": b"rendered-page",
            "figures": [],
        }
    ]

    await services.documents._enhance_pages(
        pages,
        "big-vision-model",
        triage_model="small-vision-model",
        force_repair=True,
    )

    assert fake_ollama.calls == ["big-vision-model", "small-vision-model"]
    assert pages[0]["llm_enhanced"] is True
    assert pages[0]["llm_validation_status"] == "passed"
    assert pages[0]["text"] == "# Manually rewritten"


@pytest.mark.anyio
async def test_forced_ocr_uses_embedded_text_when_tesseract_fails(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = create_backend_services(test_settings)

    class FakePage:
        def extract_text(self) -> str:
            return "Reliable embedded page text"

    class FakeReader:
        pages = [FakePage()]

    async def raise_processing_error(image_png: bytes, page_number: int) -> str:
        raise DocumentProcessingError("Tesseract exited abnormally")

    monkeypatch.setattr("backend.documents.PdfReader", lambda _: FakeReader())
    monkeypatch.setattr(services.documents, "ocr_available", lambda: True)
    monkeypatch.setattr(services.documents, "_render_page_png", lambda *_: b"page")
    monkeypatch.setattr(services.documents, "_ocr_image", raise_processing_error)

    pages = await services.documents._extract_pages(
        test_settings.data_dir / "unused.pdf",
        force_ocr=True,
        retain_page_images=True,
    )

    assert pages[0]["raw_text"] == "Reliable embedded page text"
    assert pages[0]["ocr_used"] is False
    assert "embedded text was used instead" in pages[0]["ocr_fallback_note"]


def test_extracted_figures_are_local_raw_artifacts(test_settings, tmp_path) -> None:
    image_module = pytest.importorskip("PIL.Image")
    app = create_app(test_settings)
    services = app.state.services
    image = image_module.new("RGB", (500, 300), "blue")
    pdf_path = tmp_path / "figure.pdf"
    image.save(pdf_path, "PDF")

    figures = services.documents._extract_figures(pdf_path, "document-id")

    assert len(figures) == 1
    figure = figures[0]
    artifact = services.documents.get_artifact(figure["artifact_id"])
    assert artifact is not None
    assert artifact.kind == "extracted_figure"
    assert artifact.media_type == "image/png"
    assert figure["path"] == f"/api/artifacts/{artifact.id}/raw"
    assert services.documents.artifact_bytes(artifact).startswith(b"\x89PNG")
    response = TestClient(app).get(figure["path"])
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG")
