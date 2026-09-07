from __future__ import annotations

from typing import Any

import anyio

from backend.core.config import Settings
from backend.core.errors import DocumentProcessingError
from backend.utils import ProgressCallback, report_progress
from backend.documents.formatting import DocumentFormatter, build_paper_manifest
from backend.documents.models import Document
from backend.documents.ocr import DocumentOCR
from backend.documents.repository import DocumentRepository
from backend.documents.retrieval import RetrievalService
from backend.documents.vision import OCR_QUALITY_LEVELS, VisionEnhancer
from backend.persistence.files import SafeStorage
from backend.providers.runtime import ResolvedModel
from backend.providers.types import AgentModelDefaults, ModelReference


class DocumentIngestion:
    def __init__(
        self,
        settings: Settings,
        storage: SafeStorage,
        repository: DocumentRepository,
        retrieval: RetrievalService,
        ocr: DocumentOCR,
        vision: VisionEnhancer,
        formatter: DocumentFormatter,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.repository = repository
        self.retrieval = retrieval
        self.ocr = ocr
        self.vision = vision
        self.formatter = formatter

    async def ingest(
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
        details = self.repository.get_details(document_id)
        if not details:
            raise ValueError(f"Unknown document {document_id}")
        document, artifacts, _ = details
        source = next((artifact for artifact in artifacts if artifact.kind == "source_pdf"), None)
        if not source:
            raise ValueError("Document is missing its source PDF")
        pdf_path = self.repository.artifact_path(source)
        enhancement_enabled = (
            self.settings.ocr_llm_enhancement_enabled
            if enhance_with_llm is None
            else enhance_with_llm
        )
        enhancement_model: ResolvedModel | None = None
        quality_model: ResolvedModel | None = None
        if enhancement_enabled:
            enhancement_model = self.vision.resolve_enhancement_model(
                llm_model=llm_model,
                model_reference=model_reference,
                agent_model_defaults=agent_model_defaults,
            )
            quality_model = self.vision.resolve_quality_model(enhancement_model, triage_model)
        pages = await self.ocr.extract_pages(
            pdf_path,
            force_ocr=force_ocr,
            retain_page_images=enhancement_enabled,
            progress=progress,
        )
        if enhancement_enabled:
            assert enhancement_model is not None
            assert quality_model is not None
            await self.vision.enhance_pages(
                pages,
                enhancement_model,
                triage_model=quality_model,
                progress=progress,
            )
        await report_progress(
            progress,
            {
                "phase": "saving",
                "phase_label": "Saving extracted content",
                "completed_pages": len(pages),
                "total_pages": len(pages),
            },
        )
        return self._persist_document_content(
            document,
            pages,
            enhancement_model=enhancement_model.model if enhancement_model else None,
            triage_model=quality_model.model if quality_model else None,
            extraction_mode="ocr" if force_ocr else "embedded",
        )

    async def ingestion_options(self, document_id: str) -> dict[str, Any]:
        details = self.repository.get_details(document_id)
        if not details:
            raise ValueError(f"Unknown document {document_id}")
        _, artifacts, _ = details
        source = next((artifact for artifact in artifacts if artifact.kind == "source_pdf"), None)
        if not source:
            raise ValueError("Document is missing its source PDF")
        pdf_path = self.repository.artifact_path(source)
        summary = await anyio.to_thread.run_sync(self.ocr.inspect_text_layer, pdf_path)
        return {
            **summary,
            "ocr_available": self.ocr.available(),
            "ocr_engine": "tesseract",
        }

    async def enhance_page(
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
        details = self.repository.get_details(document_id)
        if not details:
            raise ValueError(f"Unknown document {document_id}")
        document, artifacts, _ = details
        source = next((artifact for artifact in artifacts if artifact.kind == "source_pdf"), None)
        manifest_artifact = next(
            (artifact for artifact in artifacts if artifact.kind == "extracted_manifest"),
            None,
        )
        if not source or not manifest_artifact:
            raise DocumentProcessingError("Ingest the document before enhancing an individual page")
        manifest = self.repository.artifact_content(manifest_artifact)
        if not isinstance(manifest, dict) or not isinstance(manifest.get("pages"), list):
            raise DocumentProcessingError("The extracted page manifest is invalid")
        pages = manifest["pages"]
        page = next(
            (
                candidate
                for candidate in pages
                if isinstance(candidate, dict) and candidate.get("page") == page_number
            ),
            None,
        )
        if page is None:
            raise ValueError(f"Page {page_number} does not exist in document {document_id}")
        model = self.vision.resolve_enhancement_model(
            llm_model=llm_model,
            model_reference=model_reference,
            agent_model_defaults=agent_model_defaults,
        )
        quality_model = self.vision.resolve_quality_model(model, triage_model)
        pdf_path = self.repository.artifact_path(source)
        await report_progress(
            progress,
            {
                "phase": "rendering",
                "phase_label": f"Rendering page {page_number}",
                "completed_pages": 0,
                "total_pages": 1,
                "current_page": page_number,
            },
        )
        page["_image_png"] = await anyio.to_thread.run_sync(
            self.ocr.render_page_png,
            pdf_path,
            page_number - 1,
        )
        await self.vision.enhance_pages(
            [page],
            model,
            triage_model=quality_model,
            progress=progress,
            force_repair=True,
        )
        result = self._persist_document_content(
            document,
            pages,
            enhancement_model=model.model,
            triage_model=quality_model.model,
            extraction_mode="page_enhancement",
        )
        result["page"] = {
            key: value for key, value in page.items() if not key.startswith("_")
        }
        return result

    def _persist_document_content(
        self,
        document: Document,
        pages: list[dict[str, Any]],
        *,
        enhancement_model: str | None,
        triage_model: str | None = None,
        extraction_mode: str,
    ) -> dict[str, Any]:
        extracted_char_count = sum(
            len(str(page.get("text") or "").strip()) for page in pages
        )
        if extracted_char_count == 0:
            raise DocumentProcessingError(
                "No readable text was extracted from the paper. Retry ingestion with OCR."
            )
        chunks = self.formatter.chunk_pages(pages, title=document.title)
        if not chunks:
            raise DocumentProcessingError(
                "Paper extraction produced no readable chunks. Retry ingestion with OCR."
            )
        markdown = self.formatter.build_markdown(pages)
        manifest = build_paper_manifest(
            document_id=document.id,
            title=document.title,
            source_filename=document.source_filename,
            content_type=document.content_type,
            pages=pages,
            chunks=chunks,
        )
        markdown_path = f"documents/{document.id}/extracted.md"
        json_path = f"documents/{document.id}/manifest.json"
        stored_md = self.storage.write_text(self.settings.artifacts_dir, markdown_path, markdown)
        stored_json = self.storage.write_json(self.settings.artifacts_dir, json_path, manifest)
        md_artifact = self.repository.create_artifact(
            owner_type="document",
            kind="extracted_markdown",
            document_id=document.id,
            relative_path=markdown_path,
            media_type="text/markdown",
            stored=stored_md,
        )
        json_artifact = self.repository.create_artifact(
            owner_type="document",
            kind="extracted_manifest",
            document_id=document.id,
            relative_path=json_path,
            media_type="application/json",
            stored=stored_json,
        )
        self.retrieval.replace_document_chunks(document.id, chunks)
        enhancement_notes = {
            str(page["page"]): str(page["llm_enhancement_note"])
            for page in pages
            if page.get("llm_enhancement_note")
        }
        ocr_fallback_notes = {
            str(page["page"]): str(page["ocr_fallback_note"])
            for page in pages
            if page.get("ocr_fallback_note")
        }
        validation_statuses = {
            str(page["page"]): str(page["llm_validation_status"])
            for page in pages
            if page.get("llm_validation_status")
        }
        validation_issues = {
            str(page["page"]): page["llm_validation_issues"]
            for page in pages
            if page.get("llm_validation_issues")
        }
        quality_ratings = {
            str(page["page"]): str(page["ocr_quality"])
            for page in pages
            if page.get("ocr_quality")
        }
        quality_issues = {
            str(page["page"]): page["ocr_quality_issues"]
            for page in pages
            if page.get("ocr_quality_issues")
        }
        quality_counts = {
            level: sum(1 for value in quality_ratings.values() if value == level)
            for level in OCR_QUALITY_LEVELS
        }
        self.repository.mark_ready(
            document.id,
            page_count=len(pages),
            metadata={
                "ocr_available": self.ocr.available(),
                "ocr_pages": [page["page"] for page in pages if page.get("ocr_used")],
                "ocr_fallback_pages": [int(page) for page in ocr_fallback_notes],
                "ocr_fallback_notes": ocr_fallback_notes,
                "llm_enhanced_pages": [page["page"] for page in pages if page.get("llm_enhanced")],
                "llm_unenhanced_pages": [int(page) for page in enhancement_notes],
                "llm_enhancement_notes": enhancement_notes,
                "llm_validated_pages": [
                    page["page"]
                    for page in pages
                    if page.get("llm_validation_status") == "passed"
                ],
                "llm_validation_failed_pages": [
                    page["page"]
                    for page in pages
                    if page.get("llm_validation_status") == "failed"
                ],
                "llm_validation_statuses": validation_statuses,
                "llm_validation_issues": validation_issues,
                "ocr_quality_ratings": quality_ratings,
                "ocr_quality_issues": quality_issues,
                "ocr_quality_counts": quality_counts,
                "ocr_llm_model": enhancement_model,
                "ocr_llm_triage_model": triage_model,
                "extraction_mode": extraction_mode,
                "paper_manifest_schema_version": manifest["schema_version"],
                "extracted_char_count": manifest["content"]["char_count"],
                "chunk_count": manifest["content"]["chunk_count"],
            },
        )
        return {
            "document_id": document.id,
            "page_count": len(pages),
            "chunks": chunks,
            "artifact_ids": [
                md_artifact.id,
                json_artifact.id,
            ],
            "text": "\n\n".join(page["text"] for page in pages if page["text"]),
        }
