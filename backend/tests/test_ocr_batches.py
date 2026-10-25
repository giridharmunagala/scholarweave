from __future__ import annotations

import io
import time

import anyio
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.app import create_app
from backend.bootstrap import create_services
from backend.core.errors import ConflictError, DocumentProcessingError, ValidationError
from backend.documents.models import Document
from backend.documents.ocr_batch_models import OcrBatch, OcrBatchItem
from backend.documents.ocr_batch_repository import OcrBatchRepository
from backend.documents.ocr_batches import has_complete_ocr
from backend.persistence.files import SafeStorage
from backend.utils import utcnow

HTML = '<div data-label="Text" data-bbox="0 0 1000 1000"><p>Readable research evidence.</p></div>'
ACTIVE = {"pending", "running", "stopping"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def pdf(pages=1):
    images = [Image.new("RGB", (120, 160), "white") for _ in range(pages)]
    content = io.BytesIO()
    images[0].save(content, "PDF", save_all=True, append_images=images[1:])
    return content.getvalue()


def configure(settings, stub_provider, parallel=2):
    settings.ocr_engine = "surya"
    settings.ocr_surya_base_url = stub_provider.base_url + "/v1"
    settings.ocr_surya_model = "stub-model"
    settings.ocr_surya_parallel = parallel
    stub_provider.reply = HTML
    stub_provider.completion_delay_seconds = 0.15


def paper(services, title="Paper", pages=1):
    return services.documents.create_document_from_bytes(
        pdf(pages), filename="scan.pdf", title=title,
    )


async def settled(services, batch_id):
    with anyio.fail_after(10):
        while (batch := services.ocr_batches.get(batch_id))["status"] in ACTIVE:
            await anyio.sleep(0.01)
    return batch


def artifacts(services, document_id):
    return {
        item.id: services.documents.artifact_bytes(item)
        for item in services.documents.get_document_artifacts(document_id)
        if item.kind != "ocr_page_checkpoint"
    }


@pytest.mark.parametrize(("metadata", "status", "expected"), [
    ({"extraction_mode": "embedded"}, "ready", False),
    ({"extraction_mode": "ocr"}, "ready", True),
    ({"extraction_mode": "ocr", "ocr_pages": []}, "ready", False),
    ({"extraction_mode": "ocr", "ocr_pages": [1]}, "ready", False),
    ({"ocr_pages": [1, 1]}, "ready", False),
    ({"ocr_pages": [1, 2]}, "ready", True),
    ({"extraction_mode": "ocr", "ocr_pages": [1, 2], "ocr_skipped_pages": [2]}, "ready", False),
    ({"extraction_mode": "ocr", "ocr_fallback_pages": [2]}, "ready", False),
    ({"extraction_mode": "ocr", "ocr_pages": [1, 2]}, "processing", False),
])
def test_complete_ocr_requires_full_coverage(metadata, status, expected):
    document = Document(
        id="paper", title="Paper", source_filename="paper.pdf", page_count=2,
        status=status, metadata_json=metadata,
    )
    assert has_complete_ocr(document) is expected


def test_batch_api_reports_live_progress_and_retains_history(test_settings, stub_provider):
    configure(test_settings, stub_provider)
    app = create_app(test_settings)
    services = app.state.services
    documents = [paper(services, title=f"Paper {index}", pages=2) for index in range(2)]
    with TestClient(app) as client:
        for ids in ([], [documents[0].id] * 2, [" "]):
            assert client.post("/api/documents/ocr-batches", json={"document_ids": ids}).status_code == 422
        assert client.post("/api/documents/ocr-batches", json={"document_ids": ["missing"]}).status_code == 404
        assert client.get("/api/documents/ocr-batches/missing").status_code == 404
        started = client.post("/api/documents/ocr-batches", json={"document_ids": None})
        assert started.status_code == 202, started.text
        batch_id = started.json()["id"]
        assert started.json()["parallel_requests"] == 2
        assert {item["document_id"] for item in started.json()["items"]} == {doc.id for doc in documents}
        assert client.post("/api/documents/ocr-batches", json={"document_ids": None}).status_code == 409
        saw_live = False
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            batch = client.get(f"/api/documents/ocr-batches/{batch_id}").json()
            listing = client.get("/api/documents/ocr-batches").json()
            assert listing[0]["id"] == batch_id
            saw_live |= any(
                item["status"] == "running" and item["total_pages"] == 2
                and item["completed_pages"] < 2 and item["phase_label"]
                for item in batch["items"]
            )
            if batch["status"] not in ACTIVE:
                break
            time.sleep(0.01)
        assert saw_live
        assert batch["status"] == "completed"
        assert batch["finished_at"] is not None
        assert all(item["completed_pages"] == item["total_pages"] == 2 for item in batch["items"])
        assert client.post(f"/api/documents/ocr-batches/{batch_id}/stop").json()["status"] == "completed"
        assert client.post("/api/documents/ocr-batches", json={"document_ids": None}).status_code == 400
    assert stub_provider.max_active_inference == 2
    receipts = OcrBatchRepository(SafeStorage(test_settings)).list()
    assert receipts[0].id == batch_id and receipts[0].status == "completed"
    assert all(services.documents.get_document(doc.id).status == "ready" for doc in documents)


@pytest.mark.anyio
async def test_batch_and_individual_ingestion_share_global_capacity(test_settings, stub_provider):
    configure(test_settings, stub_provider)
    services = create_services(test_settings)
    manual = paper(services, title="Individual", pages=2)
    queued = [paper(services, title=f"Batch {index}") for index in range(2)]
    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(services.documents.ingest_document, manual.id)
            assert await anyio.to_thread.run_sync(stub_provider.request_started.wait, 5)
            batch = await services.ocr_batches.start([manual.id, *(doc.id for doc in queued)])
            assert batch["items"][0]["status"] == "skipped"
            finished = await settled(services, batch["id"])
        assert finished["status"] == "completed"
        assert [item["status"] for item in finished["items"]] == ["skipped", "completed", "completed"]
        assert stub_provider.max_active_inference == 2
        assert len(stub_provider.requests) == 4
        assert services.document_ocr._surya_limiter.borrowed_tokens == 0
    finally:
        await services.close()


@pytest.mark.anyio
async def test_stop_cancels_active_and_pending_without_starting_more(test_settings, stub_provider):
    configure(test_settings, stub_provider, parallel=1)
    services = create_services(test_settings)
    documents = [paper(services, title=f"Paper {index}", pages=2) for index in range(3)]
    try:
        batch = await services.ocr_batches.start([doc.id for doc in documents])
        assert await anyio.to_thread.run_sync(stub_provider.request_started.wait, 5)
        stopping = services.ocr_batches.stop(batch["id"])
        assert stopping["status"] == "stopping" and stopping["finished_at"] is None
        assert [item["status"] for item in stopping["items"]] == ["running", "stopped", "stopped"]
        with pytest.raises(ConflictError):
            await services.ocr_batches.start(None)
        finished = await settled(services, batch["id"])
        assert finished["status"] == "stopped"
        assert all(item["status"] == "stopped" for item in finished["items"])
        assert services.document_ocr._surya_limiter.borrowed_tokens == 0
        assert not any(services.documents.ingestion_active(doc.id) for doc in documents)
        stopped = services.documents.get_document(documents[0].id)
        assert stopped.status == "uploaded"
        assert stopped.metadata_json["ingestion"]["phase"] == "stopped"
        await anyio.sleep(0.2)
        assert len(stub_provider.requests) <= 1
    finally:
        await services.close()


@pytest.mark.anyio
async def test_stopping_batch_does_not_cancel_unrelated_ingestion(test_settings, stub_provider):
    configure(test_settings, stub_provider, parallel=1)
    stub_provider.completion_delay_seconds = 0.8
    services = create_services(test_settings)
    manual, queued = paper(services, title="Individual"), paper(services, title="Batch")
    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(services.documents.ingest_document, manual.id)
            assert await anyio.to_thread.run_sync(stub_provider.request_started.wait, 5)
            batch = await services.ocr_batches.start([queued.id])
            with anyio.fail_after(5):
                while not services.documents.ingestion_active(queued.id):
                    await anyio.sleep(0.01)
            services.ocr_batches.stop(batch["id"])
            assert (await settled(services, batch["id"]))["status"] == "stopped"
        assert services.documents.get_document(manual.id).status == "ready"
        assert len(stub_provider.requests) == 1
    finally:
        await services.close()


@pytest.mark.anyio
async def test_stopped_ingestion_cannot_be_replaced_before_it_settles(test_settings, monkeypatch):
    services = create_services(test_settings)
    document = paper(services)
    started, release = anyio.Event(), anyio.Event()

    async def draining_ingestion(*_args, **_kwargs):
        try:
            started.set()
            await anyio.sleep_forever()
        finally:
            with anyio.CancelScope(shield=True):
                await release.wait()

    monkeypatch.setattr(services.document_ingestion, "ingest", draining_ingestion)
    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(services.documents.ingest_document, document.id)
            await started.wait()
            services.documents.stop_ingestion(document.id)
            try:
                with pytest.raises(DocumentProcessingError, match="already in progress"):
                    await services.documents.ingest_document(document.id)
                assert services.documents.ingestion_active(document.id)
            finally:
                release.set()
        assert not services.documents.ingestion_active(document.id)
    finally:
        await services.close()


@pytest.mark.anyio
@pytest.mark.parametrize("readable_fallback", [True, False])
async def test_page_ocr_failure_uses_embedded_text_or_preserves_previous_extraction(
    test_settings, stub_provider, monkeypatch, readable_fallback,
):
    configure(test_settings, stub_provider, parallel=1)
    services = create_services(test_settings)
    document = paper(services, pages=2)
    native_text = "Existing native evidence remains available after a failed OCR attempt."
    monkeypatch.setattr(
        services.document_ocr, "_extract_native_text",
        lambda *_: native_text,
    )
    try:
        await services.documents.ingest_document(document.id, use_layout=False)
        previous = artifacts(services, document.id)
        for path, content in [
            (f"papers/{document.id}/notes.md", "My research notes"),
            (f"papers/{document.id}/summary.md", "Saved summary"),
            (f"papers/{document.id}/summaries/old.md", "Immutable summary version"),
            ("tracks/topic/notes.md", "Track notes"),
        ]:
            services.workspace.write_file(path, content)
        protected = {
            path: services.storage.read_stored_bytes(test_settings.workspace_dir, path)
            for path in services.storage.list_workspace_files() if path.endswith(".md")
        }
        count = 0

        def partial_reply(_payload):
            nonlocal count
            count += 1
            return HTML if count == 1 else "malformed layout"

        monkeypatch.setattr(
            services.document_ocr, "_extract_native_text",
            lambda _reader, index: native_text if index == 0 or readable_fallback else "",
        )
        monkeypatch.setattr(stub_provider, "_reply_for", partial_reply)
        batch = await services.ocr_batches.start(None)
        finished = await settled(services, batch["id"])
        assert finished["status"] == "completed_with_errors"
        assert finished["items"][0]["total_pages"] == 2
        saved = services.documents.get_document(document.id)
        assert saved.status == "ready"
        assert not has_complete_ocr(saved)
        if readable_fallback:
            assert finished["items"][0]["status"] == "completed_with_errors"
            assert finished["items"][0]["completed_pages"] == 2
            assert "pages 2" in finished["items"][0]["error"]
            assert "embedded" in finished["items"][0]["error"].lower()
            assert artifacts(services, document.id) != previous
            assert saved.metadata_json["ocr_pages"] == [1]
            assert saved.metadata_json["ocr_fallback_pages"] == [2]
            assert saved.metadata_json["ocr_skipped_pages"] == []
            assert "embedded text was used instead" in saved.metadata_json["ocr_fallback_notes"]["2"]
            manifest_artifact = next(
                artifact for artifact in services.documents.get_document_artifacts(document.id)
                if artifact.kind == "extracted_manifest"
            )
            pages = services.documents.artifact_content(manifest_artifact)["pages"]
            assert pages[0]["ocr_used"]
            assert "Readable research evidence." in pages[0]["text"]
            assert not pages[1]["ocr_used"]
            assert pages[1]["text"] == native_text
            assert pages[1]["citation"] == "p.2"
            assert OcrBatchRepository(services.storage).list()[0].items[0].status == "completed_with_errors"
        else:
            assert finished["items"][0]["status"] == "failed"
            assert "1 of 2 pages" in finished["items"][0]["error"]
            assert finished["items"][0]["completed_pages"] == 1
            assert artifacts(services, document.id) == previous
        assert all(
            services.storage.read_stored_bytes(test_settings.workspace_dir, path) == content
            for path, content in protected.items()
        )
        monkeypatch.setattr(stub_provider, "_reply_for", lambda _payload: HTML)
        requests_before_retry = len(stub_provider.requests)
        retry = await services.ocr_batches.start(None)
        assert (await settled(services, retry["id"]))["status"] == "completed"
        assert len(stub_provider.requests) == requests_before_retry + 1
        assert all(
            services.storage.read_stored_bytes(test_settings.workspace_dir, path) == content
            for path, content in protected.items()
        )
        assert [item["id"] for item in services.ocr_batches.list()] == [retry["id"], batch["id"]]
        explicit = await services.ocr_batches.start([document.id])
        skipped = await settled(services, explicit["id"])
        assert skipped["items"][0]["status"] == "skipped"
        with pytest.raises(ValidationError, match="No papers"):
            await services.ocr_batches.start(None)
    finally:
        await services.close()


@pytest.mark.anyio
async def test_batch_retry_after_restart_reuses_completed_pages_and_figures(test_settings, stub_provider):
    configure(test_settings, stub_provider, parallel=1)
    stub_provider.completion_delay_seconds = 0.5
    stub_provider.reply = HTML + '<div data-label="Figure" data-bbox="100 100 900 900"></div>'
    services = create_services(test_settings)
    document = paper(services, pages=3)
    try:
        batch = await services.ocr_batches.start([document.id])
        with anyio.fail_after(10):
            while services.ocr_batches.get(batch["id"])["items"][0]["completed_pages"] < 1:
                await anyio.sleep(0.01)
        services.ocr_batches.stop(batch["id"])
        assert (await settled(services, batch["id"]))["status"] == "stopped"
        saved = services.documents.get_document_artifacts(document.id)
        checkpoints = [artifact for artifact in saved if artifact.kind == "ocr_page_checkpoint"]
        assert len(checkpoints) == 1
        assert checkpoints[0].metadata_json["page"] == 1
        assert not any(artifact.kind == "extracted_manifest" for artifact in saved)
    finally:
        await services.close()

    services = create_services(test_settings)
    try:
        requests_before_retry = len(stub_provider.requests)
        retry = await services.ocr_batches.start([document.id])
        assert (await settled(services, retry["id"]))["status"] == "completed"
        assert len(stub_provider.requests) == requests_before_retry + 2
        saved = services.documents.get_document_artifacts(document.id)
        manifest = services.documents.artifact_content(next(
            artifact for artifact in saved if artifact.kind == "extracted_manifest"
        ))
        assert [page["page"] for page in manifest["pages"]] == [1, 2, 3]
        assert all(page["ocr_used"] for page in manifest["pages"])
        figures = [artifact for artifact in saved if artifact.kind == "extracted_image"]
        assert {artifact.metadata_json["page"] for artifact in figures} == {1, 2, 3}
        assert all(services.documents.artifact_bytes(artifact).startswith(b"\x89PNG") for artifact in figures)
        requests_before_rerun = len(stub_provider.requests)
        await services.documents.ingest_document(document.id, force_ocr=True)
        assert len(stub_provider.requests) == requests_before_rerun + 3
    finally:
        await services.close()


@pytest.mark.anyio
async def test_batch_retry_reuses_legacy_manifest_and_rejects_other_source_checkpoints(
    test_settings, stub_provider,
):
    configure(test_settings, stub_provider, parallel=1)
    services = create_services(test_settings)
    document = paper(services, pages=2)
    first_page = {
        "page": 1, "text": "Previously OCRed evidence.", "raw_text": "Original OCR draft.",
        "ocr_used": True, "llm_enhanced": True,
    }
    try:
        services.document_ingestion._persist_document_content(
            document, [first_page, {
                "page": 2, "text": "Embedded text, not successful OCR.", "ocr_used": False,
                "ocr_fallback_note": "Previous OCR failed.",
            }],
            enhancement_model="previous-vision-model", extraction_mode="ocr", ocr_available=True,
        )
        source = next(
            artifact for artifact in services.documents.get_document_artifacts(document.id)
            if artifact.kind == "source_pdf"
        )
        services.document_ingestion._checkpoint_ocr_page(document.id, source, {
            **first_page, "text": "Older raw checkpoint must not replace the enhanced manifest.",
        })
        # Publishing a newer manifest must take precedence over older raw OCR checkpoints.
        services.document_ingestion._persist_document_content(
            document, [first_page, {
                "page": 2, "text": "Embedded text, not successful OCR.", "ocr_used": False,
                "ocr_fallback_note": "Previous OCR failed.",
            }],
            enhancement_model="previous-vision-model", extraction_mode="ocr", ocr_available=True,
        )
        source.sha256 = "0" * 64
        services.document_ingestion._checkpoint_ocr_page(document.id, source, {
            "page": 2, "text": "OCR from a different PDF.", "ocr_used": True,
        })
        requests_before_retry = len(stub_provider.requests)
        batch = await services.ocr_batches.start([document.id])
        assert (await settled(services, batch["id"]))["status"] == "completed"
        assert len(stub_provider.requests) == requests_before_retry + 1
        manifest = services.documents.artifact_content(next(
            artifact for artifact in services.documents.get_document_artifacts(document.id)
            if artifact.kind == "extracted_manifest"
        ))
        assert manifest["pages"][0]["text"] == first_page["text"]
        assert manifest["pages"][0]["llm_enhanced"] is True
        assert "Readable research evidence." in manifest["pages"][1]["text"]
    finally:
        await services.close()


@pytest.mark.anyio
async def test_tesseract_batch_failure_does_not_clear_previous_extraction(test_settings, monkeypatch):
    services = create_services(test_settings)
    document = paper(services)

    async def extraction(*_args, force_ocr=False, **_kwargs):
        return [{
            "page": 1, "text": "Existing native evidence.", "raw_text": "Existing native evidence.",
            "ocr_used": False, "ocr_skipped": force_ocr,
        }]

    monkeypatch.setattr(services.document_ocr, "available", lambda: True)
    monkeypatch.setattr(services.document_ocr, "extract_pages", extraction)
    try:
        await services.documents.ingest_document(document.id)
        previous = artifacts(services, document.id)
        batch = await services.ocr_batches.start(None)
        failed = await settled(services, batch["id"])
        assert failed["parallel_requests"] == 1
        assert failed["status"] == "completed_with_errors"
        assert artifacts(services, document.id) == previous
    finally:
        await services.close()


@pytest.mark.anyio
async def test_shutdown_drains_owned_ingestions_and_marks_job_interrupted(test_settings, stub_provider):
    configure(test_settings, stub_provider, parallel=1)
    services = create_services(test_settings)
    documents = [paper(services, title=f"Paper {index}", pages=2) for index in range(2)]
    batch = await services.ocr_batches.start(None)
    assert await anyio.to_thread.run_sync(stub_provider.request_started.wait, 5)
    await services.close()
    interrupted = services.ocr_batches.get(batch["id"])
    assert interrupted["status"] == "interrupted"
    assert all(item["status"] == "stopped" for item in interrupted["items"])
    assert not services.documents._active_ingestions
    recovered = create_services(test_settings)
    try:
        assert recovered.ocr_batches.get(batch["id"]) == interrupted
        assert all(recovered.documents.get_document(doc.id).status != "processing" for doc in documents)
        assert len(stub_provider.requests) <= 1
    finally:
        await recovered.close()


@pytest.mark.anyio
async def test_restart_marks_unfinished_receipts_interrupted_without_resuming(test_settings):
    now = utcnow().isoformat()
    receipt = OcrBatch(
        id="old-job", status="running", created_at=now, updated_at=now,
        finished_at=None, parallel_requests=2, items=[
            OcrBatchItem("finished", "Finished", status="completed", completed_pages=2, total_pages=2),
            OcrBatchItem("active", "Active", status="running", completed_pages=1, total_pages=2),
            OcrBatchItem("queued", "Queued"),
        ],
    )
    repository = OcrBatchRepository(SafeStorage(test_settings))
    repository.save(receipt)
    services = create_services(test_settings)
    try:
        recovered = services.ocr_batches.get("old-job")
        assert recovered["status"] == "interrupted" and recovered["finished_at"]
        assert [item["status"] for item in recovered["items"]] == ["completed", "stopped", "stopped"]
        assert recovered["items"][1]["completed_pages"] == 1
        assert repository.list()[0].status == "interrupted"
        assert services.ocr_batches._task is None
    finally:
        await services.close()
