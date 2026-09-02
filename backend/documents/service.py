from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import anyio

from backend.documents.errors import DocumentProcessingError, ProgressCallback
from backend.documents.ingestion import DocumentIngestion
from backend.documents.models import Artifact, Document, DocumentChunk
from backend.documents.ocr import DocumentOCR
from backend.documents.repository import DocumentRepository
from backend.persistence.files import StoredFile
from backend.providers.types import AgentModelDefaults, ModelReference


class DocumentService:
    """Public document API composed from persistence and ingestion components."""

    def __init__(
        self,
        repository: DocumentRepository,
        ingestion: DocumentIngestion,
        ocr: DocumentOCR,
    ) -> None:
        self.repository = repository
        self.ingestion = ingestion
        self.ocr = ocr
        self._active_ingestions: dict[str, anyio.CancelScope] = {}
        self._ingestion_finished: dict[str, anyio.Event] = {}
        self._ingestion_subscribers: dict[str, set[asyncio.Queue[None]]] = {}
        self._closing = False

    def ocr_available(self) -> bool:
        return self.ocr.available()

    async def create_document_from_upload(
        self,
        upload: Any,
        title: str | None = None,
    ) -> Document:
        return await self.repository.create_from_upload(upload, title)

    def create_document_from_bytes(
        self,
        content: bytes,
        *,
        filename: str,
        title: str,
        metadata: dict[str, Any] | None = None,
    ) -> Document:
        return self.repository.create_from_bytes(
            content,
            filename=filename,
            title=title,
            metadata=metadata,
        )

    def create_artifact_record(
        self,
        *,
        owner_type: str,
        kind: str,
        relative_path: str,
        media_type: str,
        stored: StoredFile,
        document_id: str | None = None,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        storage_area: str = "artifacts",
    ) -> Artifact:
        return self.repository.create_artifact(
            owner_type=owner_type,
            kind=kind,
            relative_path=relative_path,
            media_type=media_type,
            stored=stored,
            document_id=document_id,
            run_id=run_id,
            metadata=metadata,
            storage_area=storage_area,
        )

    def list_documents(self) -> list[Document]:
        return self.repository.list()

    def get_document(self, document_id: str) -> Document | None:
        return self.repository.get(document_id)

    def get_document_details(
        self,
        document_id: str,
    ) -> tuple[Document, list[Artifact], list[DocumentChunk]] | None:
        return self.repository.get_details(document_id)

    def get_artifact(self, artifact_id: str) -> Artifact | None:
        return self.repository.get_artifact(artifact_id)

    def delete_document(self, document_id: str) -> bool:
        return self.repository.delete(document_id)

    def delete_run_artifacts(self, run_id: str) -> None:
        self.repository.delete_run_artifacts(run_id)

    def artifact_bytes(self, artifact: Artifact) -> bytes:
        return self.repository.artifact_bytes(artifact)

    def artifact_content(self, artifact: Artifact) -> Any:
        return self.repository.artifact_content(artifact)

    async def ingestion_options(self, document_id: str) -> dict[str, Any]:
        return await self.ingestion.ingestion_options(document_id)

    async def ingest_document(
        self,
        document_id: str,
        *,
        force_ocr: bool = False,
        enhance_with_llm: bool | None = None,
        llm_model: str | None = None,
        model_reference: ModelReference | None = None,
        agent_model_defaults: AgentModelDefaults | None = None,
        triage_model: str | None = None,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        if self._closing:
            raise DocumentProcessingError("The server is shutting down.")
        if force_ocr and not self.ocr.available():
            raise DocumentProcessingError(
                "The Tesseract OCR runtime is unavailable."
            )
        previous_status = self.repository.mark_processing(document_id)
        try:
            if force_ocr:
                self.repository.update_ingestion_progress(
                    document_id,
                    {
                        "mode": "ocr",
                        "phase": "cleanup",
                        "phase_label": "Clearing old generated files",
                    },
                )
                self.repository.clear_generated_document_outputs(document_id)
            self.repository.update_ingestion_progress(
                document_id,
                {
                    "mode": "ocr" if force_ocr else "embedded",
                    "phase": "starting",
                    "phase_label": "Starting OCR" if force_ocr else "Reading embedded text",
                },
            )
        except Exception:
            self.repository.finish_failed_ingestion(document_id, previous_status)
            raise

        async def track_progress(payload: dict[str, Any]) -> None:
            self.repository.update_ingestion_progress(document_id, payload)
            self._notify_ingestion_subscribers(document_id)
            if progress is not None:
                await progress(payload)

        finished = anyio.Event()
        with anyio.CancelScope() as cancel_scope:
            self._active_ingestions[document_id] = cancel_scope
            self._ingestion_finished[document_id] = finished
            try:
                result = await self.ingestion.ingest(
                    document_id,
                    force_ocr=force_ocr,
                    enhance_with_llm=enhance_with_llm,
                    llm_model=llm_model,
                    model_reference=model_reference,
                    agent_model_defaults=agent_model_defaults,
                    triage_model=triage_model,
                    progress=track_progress,
                )
            finally:
                self._active_ingestions.pop(document_id, None)
                self._ingestion_finished.pop(document_id, None)
                self.repository.finish_failed_ingestion(document_id, previous_status)
                self._notify_ingestion_subscribers(document_id)
                finished.set()
        if cancel_scope.cancel_called:
            return {"document_id": document_id, "stopped": True}
        return result

    def stop_ingestion(self, document_id: str) -> Document:
        cancel_scope = self._active_ingestions.get(document_id)
        document = self.repository.stop_ingestion(document_id)
        if cancel_scope is not None:
            cancel_scope.cancel()
        self._notify_ingestion_subscribers(document_id)
        return document

    @asynccontextmanager
    async def subscribe_to_ingestion(self, document_id: str) -> AsyncIterator[asyncio.Queue[None]]:
        queue: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        subscribers = self._ingestion_subscribers.setdefault(document_id, set())
        subscribers.add(queue)
        try:
            yield queue
        finally:
            subscribers.discard(queue)
            if not subscribers:
                self._ingestion_subscribers.pop(document_id, None)

    def _notify_ingestion_subscribers(self, document_id: str) -> None:
        for queue in tuple(self._ingestion_subscribers.get(document_id, ())):
            if queue.empty():
                queue.put_nowait(None)

    async def close(self) -> None:
        self._closing = True
        active = [
            (
                document_id,
                cancel_scope,
                self._ingestion_finished[document_id],
            )
            for document_id, cancel_scope in tuple(self._active_ingestions.items())
            if document_id in self._ingestion_finished
        ]
        for document_id, cancel_scope, _ in active:
            document = self.repository.get(document_id)
            if document is not None and document.status == "processing":
                self.repository.stop_ingestion(
                    document_id,
                    phase_label="Ingestion stopped during server shutdown",
                )
            cancel_scope.cancel()
        for _, _, finished in active:
            await finished.wait()

    async def enhance_document_page(
        self,
        document_id: str,
        page_number: int,
        *,
        llm_model: str | None = None,
        model_reference: ModelReference | None = None,
        agent_model_defaults: AgentModelDefaults | None = None,
        triage_model: str | None = None,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        return await self.ingestion.enhance_page(
            document_id,
            page_number,
            llm_model=llm_model,
            model_reference=model_reference,
            agent_model_defaults=agent_model_defaults,
            triage_model=triage_model,
            progress=progress,
        )
