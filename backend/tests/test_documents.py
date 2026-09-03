from __future__ import annotations

import base64
import shutil
from pathlib import Path

import anyio
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app import create_app
from backend.bootstrap import create_services
from backend.documents import DocumentProcessingError
from backend.documents.paper import build_paper_manifest
from backend.providers.ollama import OllamaError
from backend.documents.models import Artifact, Document, DocumentChunk


def test_paper_manifest_has_stable_reading_structure() -> None:
    manifest = build_paper_manifest(
        document_id="paper-1",
        title="Structured Paper",
        source_filename="paper.pdf",
        content_type="application/pdf",
        pages=[
            {"page": 1, "text": "Abstract text", "ocr_used": False},
            {"page": 2, "text": "Method text", "ocr_used": True},
        ],
        chunks=[
            {
                "section_title": "Method",
                "page_start": 2,
                "page_end": 2,
                "citation": "p.2",
                "text": "Method text",
            }
        ],
        figures=[],
    )

    assert manifest["schema"] == "scholarweave.paper"
    assert manifest["schema_version"] == 1
    assert manifest["paper"]["page_count"] == 2
    assert manifest["pages"][1]["citation"] == "p.2"
    assert manifest["sections"] == [
        {
            "section_index": 0,
            "title": "Method",
            "citation": "p.2",
            "page_start": 2,
            "page_end": 2,
            "chunk_index": 0,
        }
    ]
    assert manifest["content"] == {
        "char_count": 24,
        "nonempty_page_count": 2,
        "chunk_count": 1,
        "figure_count": 0,
    }


def test_artifact_records_are_idempotent_by_owned_path(test_settings) -> None:
    services = create_services(test_settings)
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


def test_delete_run_artifacts_removes_records_and_files(test_settings) -> None:
    services = create_services(test_settings)
    run_id = "run-to-delete"
    path = f"runs/{run_id}/result.md"
    stored = services.storage.write_text(
        test_settings.artifacts_dir,
        path,
        "generated result",
    )
    artifact = services.documents.create_artifact_record(
        owner_type="agent_run",
        kind="generated",
        relative_path=path,
        media_type="text/markdown",
        stored=stored,
        metadata={"agent_run_id": run_id},
    )

    services.documents.delete_run_artifacts(run_id)

    assert not stored.absolute_path.exists()
    assert services.documents.get_artifact(artifact.id) is None


def test_artifact_responses_are_not_cached(test_settings) -> None:
    services = create_services(test_settings)
    stored = services.storage.write_text(
        test_settings.artifacts_dir,
        "documents/cache-test/extracted.md",
        "fresh extraction",
    )
    artifact = services.documents.create_artifact_record(
        owner_type="document",
        kind="extracted_markdown",
        relative_path="documents/cache-test/extracted.md",
        media_type="text/markdown",
        stored=stored,
    )
    client = TestClient(create_app(test_settings))

    content = client.get(f"/api/artifacts/{artifact.id}/content")
    raw = client.get(f"/api/artifacts/{artifact.id}/raw")

    assert content.status_code == 200
    assert content.headers["cache-control"] == "no-store"
    assert raw.status_code == 200
    assert raw.headers["cache-control"] == "no-store"


def test_document_list_returns_summaries_without_loading_details(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(test_settings)
    with app.state.services.session_factory() as session:
        session.add(
            Document(
                id="summary-only",
                title="Lightweight list item",
                source_filename="paper.pdf",
                content_type="application/pdf",
                status="ready",
                page_count=12,
                metadata_json={},
            )
        )
        session.commit()

    def fail_if_details_are_loaded(_document_id: str):
        raise AssertionError("Document details should not be loaded for the list.")

    monkeypatch.setattr(
        app.state.services.documents,
        "get_document_details",
        fail_if_details_are_loaded,
    )

    response = TestClient(app).get("/api/documents")

    assert response.status_code == 200
    assert response.json()[0]["title"] == "Lightweight list item"
    assert "artifacts" not in response.json()[0]
    assert "chunks" not in response.json()[0]


def test_text_layer_inspection_recommends_an_ingestion_mode(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = create_services(test_settings)

    class FakePage:
        def __init__(self, text: str) -> None:
            self.text = text

        def extract_text(self) -> str:
            return self.text

    class FakeReader:
        pages = [
            FakePage("Embedded text " * 10),
            FakePage("Embedded text " * 10),
            FakePage(""),
        ]

    monkeypatch.setattr("backend.documents.ocr.PdfReader", lambda _: FakeReader())

    summary = services.document_ocr.inspect_text_layer(test_settings.data_dir / "paper.pdf")

    assert summary == {
        "total_pages": 3,
        "embedded_text_pages": 2,
        "embedded_text_ratio": pytest.approx(2 / 3),
        "recommended_mode": "ocr",
    }


@pytest.mark.anyio
async def test_ingestion_persists_progress_and_completion(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = create_services(test_settings)
    document_id = "progress-document"
    with services.session_factory() as session:
        session.add(
            Document(
                id=document_id,
                title="Progress",
                source_filename="progress.pdf",
                content_type="application/pdf",
                status="uploaded",
                metadata_json={},
            )
        )
        session.commit()

    async def complete_ingestion(_document_id: str, **kwargs: object) -> dict[str, object]:
        assert services.documents.get_document(document_id).status == "processing"
        progress = kwargs["progress"]
        assert callable(progress)
        await progress(
            {
                "phase": "ocr",
                "phase_label": "Rendering and OCR",
                "completed_pages": 1,
                "total_pages": 2,
            }
        )
        processing = services.documents.get_document(document_id)
        assert processing is not None
        assert processing.metadata_json["ingestion"]["completed_pages"] == 1
        services.document_repository.mark_ready(
            document_id,
            page_count=2,
            metadata={"figure_count": 0},
        )
        return {"document_id": document_id}

    monkeypatch.setattr(services.document_ingestion, "ingest", complete_ingestion)

    await services.documents.ingest_document(document_id)

    completed = services.documents.get_document(document_id)
    assert completed is not None
    assert completed.status == "ready"
    assert completed.metadata_json["ingestion"] == {
        "phase": "complete",
        "phase_label": "Ingestion complete",
        "completed_pages": 2,
        "total_pages": 2,
    }


@pytest.mark.anyio
async def test_forced_ocr_is_forwarded_to_ingestion(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = create_services(test_settings)
    document_id = "forced-ocr-document"
    with services.session_factory() as session:
        session.add(
            Document(
                id=document_id,
                title="Forced OCR",
                source_filename="forced.pdf",
                content_type="application/pdf",
                status="uploaded",
                metadata_json={},
            )
        )
        session.commit()

    async def complete_ingestion(_document_id: str, **kwargs: object) -> dict[str, object]:
        assert kwargs["force_ocr"] is True
        services.document_repository.mark_ready(
            document_id,
            page_count=1,
            metadata={"extraction_mode": "ocr"},
        )
        return {"document_id": document_id}

    monkeypatch.setattr(services.document_ocr, "available", lambda: True)
    monkeypatch.setattr(services.document_ingestion, "ingest", complete_ingestion)

    await services.documents.ingest_document(document_id, force_ocr=True)

    completed = services.documents.get_document(document_id)
    assert completed is not None
    assert completed.metadata_json["extraction_mode"] == "ocr"


@pytest.mark.anyio
async def test_forced_ocr_clears_stale_generated_outputs(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = create_services(test_settings)
    document_id = "ocr-cleanup-document"
    with services.session_factory() as session:
        session.add(
            Document(
                id=document_id,
                title="OCR Cleanup",
                source_filename="cleanup.pdf",
                content_type="application/pdf",
                status="ready",
                page_count=1,
                metadata_json={},
            )
        )
        session.add(
            DocumentChunk(
                document_id=document_id,
                chunk_index=0,
                section_title="Old",
                page_start=1,
                page_end=1,
                citation="p.1",
                text="stale text",
                metadata_json={},
            )
        )
        session.commit()
    source_file = services.storage.write_text(
        test_settings.documents_dir,
        f"{document_id}/source.pdf",
        "source",
    )
    source_artifact = services.documents.create_artifact_record(
        owner_type="document",
        kind="source_pdf",
        document_id=document_id,
        relative_path=source_file.relative_path,
        media_type="application/pdf",
        stored=source_file,
        storage_area="documents",
    )
    stale_file = services.storage.write_text(
        test_settings.artifacts_dir,
        f"documents/{document_id}/extracted.md",
        "old markdown",
    )
    stale_artifact = services.documents.create_artifact_record(
        owner_type="document",
        kind="extracted_markdown",
        document_id=document_id,
        relative_path=stale_file.relative_path,
        media_type="text/markdown",
        stored=stale_file,
    )
    orphan_file = services.storage.write_text(
        test_settings.artifacts_dir,
        f"documents/{document_id}/orphan-note.md",
        "orphan",
    )

    async def complete_ingestion(_document_id: str, **kwargs: object) -> dict[str, object]:
        assert kwargs["force_ocr"] is True
        details = services.documents.get_document_details(document_id)
        assert details is not None
        _, artifacts, chunks = details
        assert [artifact.kind for artifact in artifacts] == ["source_pdf"]
        assert chunks == []
        assert source_file.absolute_path.exists()
        assert not stale_file.absolute_path.exists()
        assert not orphan_file.absolute_path.exists()
        services.document_repository.mark_ready(
            document_id,
            page_count=1,
            metadata={"extraction_mode": "ocr"},
        )
        return {"document_id": document_id}

    monkeypatch.setattr(services.document_ocr, "available", lambda: True)
    monkeypatch.setattr(services.document_ingestion, "ingest", complete_ingestion)

    await services.documents.ingest_document(document_id, force_ocr=True)

    assert services.documents.get_artifact(source_artifact.id) is not None
    assert services.documents.get_artifact(stale_artifact.id) is None


@pytest.mark.anyio
async def test_failed_ingestion_does_not_leave_document_processing(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = create_services(test_settings)
    document_id = "failed-document"
    with services.session_factory() as session:
        session.add(
            Document(
                id=document_id,
                title="Failure",
                source_filename="failure.pdf",
                content_type="application/pdf",
                status="uploaded",
                metadata_json={},
            )
        )
        session.commit()

    async def fail_ingestion(_document_id: str, **_kwargs: object) -> dict[str, object]:
        raise DocumentProcessingError("PDF extraction failed")

    monkeypatch.setattr(services.document_ingestion, "ingest", fail_ingestion)

    with pytest.raises(DocumentProcessingError, match="PDF extraction failed"):
        await services.documents.ingest_document(document_id)

    failed = services.documents.get_document(document_id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.metadata_json["ingestion"]["phase"] == "failed"


@pytest.mark.anyio
async def test_running_ingestion_can_be_stopped(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = create_services(test_settings)
    document_id = "stoppable-document"
    with services.session_factory() as session:
        session.add(
            Document(
                id=document_id,
                title="Stoppable",
                source_filename="stoppable.pdf",
                content_type="application/pdf",
                status="uploaded",
                metadata_json={},
            )
        )
        session.commit()

    started = anyio.Event()

    async def blocked_ingestion(
        _document_id: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        started.set()
        await anyio.sleep_forever()
        raise AssertionError("cancelled ingestion resumed unexpectedly")

    monkeypatch.setattr(services.document_ingestion, "ingest", blocked_ingestion)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(services.documents.ingest_document, document_id)
        await started.wait()
        stopped = services.documents.stop_ingestion(document_id)
        assert stopped.status == "uploaded"

    document = services.documents.get_document(document_id)
    assert document is not None
    assert document.status == "uploaded"
    assert document.metadata_json["ingestion"]["phase"] == "stopped"
    assert document.metadata_json["ingestion"]["phase_label"] == "Ingestion stopped"


@pytest.mark.anyio
async def test_server_shutdown_stops_running_ingestions(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = create_services(test_settings)
    document_id = "shutdown-document"
    with services.session_factory() as session:
        session.add(
            Document(
                id=document_id,
                title="Shutdown",
                source_filename="shutdown.pdf",
                content_type="application/pdf",
                status="uploaded",
                metadata_json={},
            )
        )
        session.commit()

    started = anyio.Event()

    async def blocked_ingestion(
        _document_id: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        started.set()
        await anyio.sleep_forever()
        raise AssertionError("cancelled ingestion resumed unexpectedly")

    monkeypatch.setattr(services.document_ingestion, "ingest", blocked_ingestion)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(services.documents.ingest_document, document_id)
        await started.wait()
        await services.close()

    document = services.documents.get_document(document_id)
    assert document is not None
    assert document.status == "uploaded"
    assert document.metadata_json["ingestion"]["phase"] == "stopped"
    assert "server shutdown" in document.metadata_json["ingestion"]["phase_label"]


def test_stale_ingestion_is_recovered_for_retry(test_settings) -> None:
    services = create_services(test_settings)
    document_id = "stale-document"
    with services.session_factory() as session:
        session.add(
            Document(
                id=document_id,
                title="Stale",
                source_filename="stale.pdf",
                content_type="application/pdf",
                status="processing",
                page_count=4,
                metadata_json={
                    "ingestion": {
                        "phase": "ocr",
                        "previous_status": "ready",
                    }
                },
            )
        )
        session.commit()

    assert services.document_repository.recover_stale_ingestions() == [document_id]

    document = services.documents.get_document(document_id)
    assert document is not None
    assert document.status == "ready"
    assert document.metadata_json["ingestion"]["phase"] == "stopped"
    assert "restart" in document.metadata_json["ingestion"]["phase_label"]


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
    assert upload.status_code == 201

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


def test_cmyk_embedded_figure_is_saved_as_png(test_settings, monkeypatch) -> None:
    image_module = pytest.importorskip("PIL.Image")
    from backend.documents.figures import FigureExtractor
    from backend.documents.repository import DocumentRepository
    from backend.persistence.database import create_session_factory
    from backend.persistence.files import SafeStorage

    class EmbeddedImage:
        name = "cmyk-figure"
        image = image_module.new("CMYK", (256, 256), (0, 128, 128, 0))

    class Page:
        images = [EmbeddedImage()]

    class Reader:
        pages = [Page()]

        def __init__(self, _path: str) -> None:
            pass

    monkeypatch.setattr("backend.documents.figures.PdfReader", Reader)
    session_factory = create_session_factory(test_settings)
    storage = SafeStorage(test_settings)
    repository = DocumentRepository(session_factory, test_settings, storage)
    extractor = FigureExtractor(test_settings, storage, repository)

    try:
        figures = extractor.extract(Path("paper.pdf"), "document-id")
        assert len(figures) == 1
        artifact = repository.get_artifact(figures[0]["artifact_id"])
        assert artifact is not None
        assert repository.artifact_bytes(artifact).startswith(b"\x89PNG")
    finally:
        session_factory.kw["bind"].dispose()
@pytest.mark.anyio
async def test_good_ocr_skips_the_rewrite_model(test_settings) -> None:
    services = create_services(test_settings)

    class FakeOllama:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def generate(self, model: str, prompt: str, **kwargs: object) -> dict[str, str]:
            self.calls.append({"model": model, "prompt": prompt, **kwargs})
            return {"response": '{"quality":"good","issues":[]}'}

    fake_ollama = FakeOllama()
    services.document_vision.ollama = fake_ollama  # type: ignore[assignment]
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

    await services.document_vision.enhance_pages(
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
    services = create_services(test_settings)

    class FakeOllama:
        def __init__(self) -> None:
            self.calls = 0

        async def generate(self, model: str, prompt: str, **kwargs: object) -> dict[str, str]:
            self.calls += 1
            return {"response": '{"quality":"average","issues":["Table borders were lost."]}'}

    fake_ollama = FakeOllama()
    services.document_vision.ollama = fake_ollama  # type: ignore[assignment]
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

    await services.document_vision.enhance_pages(
        pages,
        "big-vision-model",
        triage_model="small-vision-model",
    )

    assert fake_ollama.calls == 1
    assert pages[0]["ocr_quality"] == "average"
    assert pages[0]["ocr_quality_issues"] == ["Table borders were lost."]
    assert pages[0]["text"] == "Usable OCR content"
    assert pages[0]["llm_enhanced"] is False


@pytest.mark.anyio
async def test_poor_ocr_is_rewritten_and_revalidated(test_settings) -> None:
    services = create_services(test_settings)

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
    services.document_vision.ollama = fake_ollama  # type: ignore[assignment]
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

    await services.document_vision.enhance_pages(
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
    chunks = services.document_formatter.chunk_pages(pages, title="Test")
    assert any("| A | B |\n|---|---|\n| 1 | 2 |" in chunk["text"] for chunk in chunks)
    phases = [event["phase"] for event in progress_events]
    assert phases[0] == "ocr_triage"
    assert "llm_enhancement" in phases
    assert progress_events[-1]["phase"] == "llm_validation"
    assert progress_events[-1]["eta_seconds"] == 0.0


@pytest.mark.anyio
async def test_empty_ocr_is_rewritten_without_a_triage_call(test_settings) -> None:
    services = create_services(test_settings)

    class FakeOllama:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def generate(self, model: str, prompt: str, **kwargs: object) -> dict[str, str]:
            self.calls.append(model)
            if model == "small-vision-model":
                return {"response": '{"quality":"good","issues":[]}'}
            return {"response": "# Recovered heading"}

    fake_ollama = FakeOllama()
    services.document_vision.ollama = fake_ollama  # type: ignore[assignment]
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

    await services.document_vision.enhance_pages(
        pages,
        "big-vision-model",
        triage_model="small-vision-model",
    )

    assert fake_ollama.calls == ["big-vision-model", "small-vision-model"]
    assert pages[0]["ocr_quality"] == "poor"
    assert pages[0]["ocr_quality_issues"] == ["OCR produced no text for this page."]
    assert pages[0]["text"] == "# Recovered heading"


@pytest.mark.anyio
async def test_failed_triage_request_falls_back_to_rewriting(test_settings) -> None:
    services = create_services(test_settings)

    class FlakyOllama:
        async def generate(self, model: str, prompt: str, **kwargs: object) -> dict[str, str]:
            if model == "small-vision-model":
                raise OllamaError("connection reset")
            return {"response": "# Rewritten page"}

    services.document_vision.ollama = FlakyOllama()  # type: ignore[assignment]
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

    await services.document_vision.enhance_pages(
        pages,
        "big-vision-model",
        triage_model="small-vision-model",
    )

    assert pages[0]["ocr_quality"] == "poor"
    assert "OCR quality check failed" in pages[0]["ocr_quality_issues"][0]
    assert pages[0]["llm_enhanced"] is False
    assert pages[0]["llm_validation_status"] == "failed"
    assert pages[0]["text"] == "Original OCR content"


@pytest.mark.anyio
async def test_llm_rewrite_falls_back_to_ocr_when_response_is_empty(test_settings) -> None:
    services = create_services(test_settings)

    class EmptyOllama:
        async def generate(self, model: str, prompt: str, **kwargs: object) -> dict[str, str]:
            if model == "small-vision-model":
                return {"response": '{"quality":"poor","issues":["Text is scrambled."]}'}
            return {"response": "", "thinking": "internal reasoning only"}

    services.document_vision.ollama = EmptyOllama()  # type: ignore[assignment]
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

    await services.document_vision.enhance_pages(
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
    markdown = services.document_formatter.build_markdown("Paper", pages)
    assert "**OCR note:** No LLM enhancement was applied" in markdown


@pytest.mark.anyio
async def test_validation_rejects_unfaithful_rewrite(test_settings) -> None:
    services = create_services(test_settings)

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

    services.document_vision.ollama = RejectingOllama()  # type: ignore[assignment]
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

    await services.document_vision.enhance_pages(
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
    services = create_services(test_settings)

    class FakeOllama:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def generate(self, model: str, prompt: str, **kwargs: object) -> dict[str, str]:
            self.calls.append(model)
            if model == "small-vision-model":
                return {"response": '{"quality":"good","issues":[]}'}
            return {"response": "# Manually rewritten"}

    fake_ollama = FakeOllama()
    services.document_vision.ollama = fake_ollama  # type: ignore[assignment]
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

    await services.document_vision.enhance_pages(
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
    services = create_services(test_settings)

    class FakePage:
        def extract_text(self) -> str:
            return "Reliable embedded page text"

    class FakeReader:
        pages = [FakePage()]

    async def raise_processing_error(image_png: bytes, page_number: int) -> str:
        raise DocumentProcessingError("Tesseract exited abnormally")

    monkeypatch.setattr("backend.documents.ocr.PdfReader", lambda _: FakeReader())
    monkeypatch.setattr(services.document_ocr, "available", lambda: True)
    monkeypatch.setattr(services.document_ocr, "render_page_png", lambda *_: b"page")
    monkeypatch.setattr(services.document_ocr, "ocr_image", raise_processing_error)

    pages = await services.document_ocr.extract_pages(
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

    figures = services.document_figures.extract(pdf_path, "document-id")

    assert len(figures) == 1
    figure = figures[0]
    artifact = services.documents.get_artifact(figure["artifact_id"])
    assert artifact is not None
    assert artifact.kind == "extracted_figure"
    assert artifact.media_type == "image/png"
    assert figure["path"] == f"/api/artifacts/{artifact.id}/raw?sha256={artifact.sha256}"
    assert services.documents.artifact_bytes(artifact).startswith(b"\x89PNG")
    response = TestClient(app).get(figure["path"])
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG")
