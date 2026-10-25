from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Sequence
from dataclasses import asdict
from typing import Any

import anyio

from backend.core.errors import ConflictError, NotFoundError, ValidationError
from backend.documents.models import Document
from backend.documents.ocr_batch_models import OcrBatch, OcrBatchItem
from backend.documents.ocr_batch_repository import OcrBatchRepository
from backend.documents.service import DocumentService
from backend.utils import utcnow

_ACTIVE = {"pending", "running", "stopping"}
_logger = logging.getLogger(__name__)


def has_complete_ocr(document: Document) -> bool:
    metadata = document.metadata_json if isinstance(document.metadata_json, dict) else {}
    if document.status != "ready" or metadata.get("ocr_skipped_pages") or metadata.get("ocr_fallback_pages"):
        return False
    pages = metadata.get("ocr_pages")
    if isinstance(pages, list):
        completed = {page for page in pages if isinstance(page, int) and page > 0}
        return bool(completed) and (
            not document.page_count or set(range(1, document.page_count + 1)).issubset(completed)
        )
    # Older successful extractions did not store explicit page coverage.
    return metadata.get("extraction_mode") == "ocr"


class OcrBatchService:
    def __init__(self, repository: OcrBatchRepository, documents: DocumentService) -> None:
        self.repository = repository
        self.documents = documents
        self._batches = {batch.id: batch for batch in repository.list()}
        self._task: asyncio.Task[None] | None = None
        self._item_scopes: dict[str, anyio.CancelScope] = {}
        self._closing = False
        for batch in self._batches.values():
            if batch.status in _ACTIVE:
                self._finish_interrupted(batch)

    def list(self) -> list[dict[str, Any]]:
        return [
            asdict(batch)
            for batch in sorted(self._batches.values(), key=lambda batch: batch.created_at, reverse=True)
        ]

    def get(self, batch_id: str) -> dict[str, Any]:
        return asdict(self._get(batch_id))

    def _get(self, batch_id: str) -> OcrBatch:
        batch = self._batches.get(batch_id)
        if batch is None:
            raise NotFoundError(f"OCR batch {batch_id} was not found.")
        return batch

    def _save(self, batch: OcrBatch) -> None:
        batch.updated_at = utcnow().isoformat()
        self.repository.save(batch)

    async def start(self, document_ids: Sequence[str] | None) -> dict[str, Any]:
        if self._closing:
            raise ConflictError("The server is shutting down.")
        if any(batch.status in _ACTIVE for batch in self._batches.values()):
            raise ConflictError("An OCR batch is already active. Stop it or wait for it to finish.")
        if document_ids is not None:
            if not document_ids or any(not value.strip() or value != value.strip() for value in document_ids):
                raise ValidationError("Supply nonempty paper IDs without surrounding whitespace.")
            if len(set(document_ids)) != len(document_ids):
                raise ValidationError("Each paper may appear only once in an OCR batch.")
            documents = []
            for document_id in document_ids:
                document = self.documents.get_document(document_id)
                if document is None:
                    raise NotFoundError(f"Paper {document_id} was not found.")
                documents.append(document)
        else:
            documents = [
                document for document in self.documents.list_documents()
                if not has_complete_ocr(document) and not self._processing(document)
            ]
        if not documents:
            raise ValidationError("No papers currently need OCR.")
        items = [self._item(document) for document in documents]
        settings = self.documents.ocr.settings
        now = utcnow().isoformat()
        batch = OcrBatch(
            id=str(uuid.uuid4()), status="pending", created_at=now, updated_at=now,
            finished_at=None,
            parallel_requests=settings.ocr_surya_parallel if settings.ocr_engine == "surya" else 1,
            items=items,
        )
        self.repository.save(batch)
        self._batches[batch.id] = batch
        self._task = asyncio.create_task(self._run(batch), name=f"ocr-batch-{batch.id}")
        return asdict(batch)

    def _processing(self, document: Document) -> bool:
        return document.status == "processing" or self.documents.ingestion_active(document.id)

    def _item(self, document: Document) -> OcrBatchItem:
        item = OcrBatchItem(
            document_id=document.id, title=document.title, total_pages=document.page_count,
        )
        if self._processing(document):
            item.status, item.phase_label = "skipped", "Already being ingested"
        elif has_complete_ocr(document):
            item.status, item.phase_label = "skipped", "OCR already complete"
            item.completed_pages = document.page_count or 0
        return item

    def stop(self, batch_id: str) -> dict[str, Any]:
        batch = self._get(batch_id)
        if batch.status not in _ACTIVE:
            return asdict(batch)
        batch.status = "stopping"
        for item in batch.items:
            if item.status == "pending":
                item.status, item.phase_label = "stopped", "Stopped before starting"
        self._save(batch)
        # These scopes belong only to this batch, never to unrelated ingestions.
        for scope in tuple(self._item_scopes.values()):
            scope.cancel()
        return asdict(batch)

    async def _run(self, batch: OcrBatch) -> None:
        try:
            if batch.status == "pending":
                batch.status = "running"
                self._save(batch)
            pending = iter(item for item in batch.items if item.status == "pending")

            async def worker() -> None:
                for item in pending:
                    if batch.status != "running":
                        break
                    await self._process(batch, item)

            async with anyio.create_task_group() as tasks:
                for _ in range(min(batch.parallel_requests, len(batch.items))):
                    tasks.start_soon(worker)
        except Exception:
            _logger.exception("OCR batch %s was interrupted", batch.id)
            self._finish_interrupted(batch)
            return
        if self._closing:
            self._finish_interrupted(batch)
            return
        if batch.status == "stopping":
            batch.status = "stopped"
        else:
            batch.status = "completed_with_errors" if any(
                item.status in {"failed", "stopped", "completed_with_errors"} for item in batch.items
            ) else "completed"
        batch.finished_at = utcnow().isoformat()
        self._save(batch)

    async def _process(self, batch: OcrBatch, item: OcrBatchItem) -> None:
        document = self.documents.get_document(item.document_id)
        if document is None:
            item.status, item.error = "failed", "Paper was deleted before OCR started."
            self._save(batch)
            return
        fresh = self._item(document)
        if fresh.status == "skipped":
            item.status, item.phase_label = fresh.status, fresh.phase_label
            item.completed_pages, item.total_pages = fresh.completed_pages, fresh.total_pages
            self._save(batch)
            return
        item.status, item.phase_label = "running", "Starting OCR"
        self._save(batch)

        async def progress(payload: dict[str, Any]) -> None:
            for key in ("completed_pages", "total_pages"):
                value = payload.get(key)
                if isinstance(value, int) and value >= 0:
                    setattr(item, key, value)
            if isinstance(payload.get("phase_label"), str):
                item.phase_label = payload["phase_label"]
            self._save(batch)

        with anyio.CancelScope() as scope:
            self._item_scopes[item.document_id] = scope
            try:
                result = await self.documents.ingest_document(
                    item.document_id, force_ocr=True, require_readable_pages=True,
                    reuse_ocr_pages=True, progress=progress,
                )
                if result.get("stopped"):
                    item.status, item.phase_label = "stopped", "OCR stopped"
                else:
                    document = self.documents.get_document(item.document_id)
                    if (
                        document is None or document.status != "ready"
                        or document.metadata_json.get("ocr_skipped_pages")
                    ):
                        raise RuntimeError("OCR coverage is incomplete. This paper can be retried.")
                    fallback_pages = document.metadata_json.get("ocr_fallback_pages")
                    if fallback_pages:
                        item.status, item.phase_label = "completed_with_errors", "Saved with embedded-text fallback"
                        item.error = (
                            f"OCR failed on pages {', '.join(str(page) for page in fallback_pages)}. "
                            "Embedded PDF text was saved instead; these pages are not OCR-verified "
                            "and this paper remains retryable."
                        )
                    elif has_complete_ocr(document):
                        item.status, item.phase_label = "completed", "OCR complete"
                        if cpu_pages := document.metadata_json.get("ocr_indra_pages"):
                            item.phase_label = f"OCR complete; Indra CPU pages {', '.join(map(str, cpu_pages))}"
                    else:
                        raise RuntimeError("OCR coverage is incomplete. This paper can be retried.")
                    item.total_pages = document.page_count
                    item.completed_pages = document.page_count or 0
            except Exception as exc:
                document = self.documents.get_document(item.document_id)
                if document is not None and self._processing(document):
                    item.status, item.phase_label = "skipped", "Already being ingested"
                else:
                    item.status, item.error = "failed", str(exc) or type(exc).__name__
                    item.phase_label = "OCR failed; paper can be retried"
            finally:
                self._item_scopes.pop(item.document_id, None)
                if scope.cancel_called:
                    item.status, item.phase_label = "stopped", "OCR stopped"
                self._save(batch)

    def _finish_interrupted(self, batch: OcrBatch) -> None:
        batch.status = "interrupted"
        for item in batch.items:
            if item.status in {"pending", "running"}:
                item.status, item.phase_label = "stopped", "Interrupted by server shutdown or restart"
        batch.finished_at = utcnow().isoformat()
        self._save(batch)

    async def close(self) -> None:
        self._closing = True
        for batch in self._batches.values():
            if batch.status in _ACTIVE:
                self.stop(batch.id)
        if self._task is not None:
            with anyio.CancelScope(shield=True):
                await self._task
