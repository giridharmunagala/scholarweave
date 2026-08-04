from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from openai import OpenAIError
from pypdf import PdfReader
from sqlalchemy import and_, delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from backend.config import Settings
from backend.models import Artifact, Document, DocumentChunk
from backend.ollama import OllamaClient, OllamaError
from backend.provider_runtime import ModelRuntime, ProviderRuntimeError, ResolvedModel
from backend.retrieval import RetrievalService
from backend.schemas import ModelReference, WorkflowModelDefaults
from backend.storage import SafeStorage, StoredFile
from backend.utils import clean_filename, utcnow


class DocumentProcessingError(RuntimeError):
    pass


ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]

OCR_QUALITY_LEVELS = ("good", "average", "poor")

OCR_QUALITY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "quality": {"type": "string", "enum": list(OCR_QUALITY_LEVELS)},
        "issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["quality", "issues"],
    "additionalProperties": False,
}


class DocumentService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        storage: SafeStorage,
        retrieval: RetrievalService,
        ollama: OllamaClient,
        model_runtime: ModelRuntime,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.storage = storage
        self.retrieval = retrieval
        self.ollama = ollama
        self.model_runtime = model_runtime

    def ocr_available(self) -> bool:
        try:
            import pypdfium2  # noqa: F401
            import pytesseract
        except ImportError:
            return False
        if shutil.which("tesseract") is None:
            return False
        try:
            return self.settings.ocr_language in pytesseract.get_languages(config=self._tesseract_config())
        except (OSError, pytesseract.TesseractError, pytesseract.TesseractNotFoundError):
            return False

    @staticmethod
    def _tesseract_config() -> str:
        candidates = [
            os.environ.get("TESSDATA_PREFIX"),
            "/usr/share/tesseract-ocr/5/tessdata",
            "/usr/share/tessdata",
            "/usr/local/share/tessdata",
        ]
        for candidate in candidates:
            if candidate and Path(candidate).is_dir():
                return f'--tessdata-dir "{candidate}"'
        return ""

    async def create_document_from_upload(self, upload: Any, title: str | None = None) -> Document:
        document = Document(
            id=str(uuid.uuid4()),
            title=title or Path(upload.filename or "document.pdf").stem,
            source_filename=clean_filename(upload.filename or "document.pdf"),
            content_type=upload.content_type or "application/pdf",
            status="uploaded",
            metadata_json={},
        )
        stored = await self.storage.save_upload(upload, f"{document.id}/source")
        with self.session_factory() as session:
            session.add(document)
            session.flush()
            session.add(
                Artifact(
                    document_id=document.id,
                    owner_type="document",
                    kind="source_pdf",
                    relative_path=stored.relative_path,
                    media_type=document.content_type,
                    size_bytes=stored.size_bytes,
                    sha256=stored.sha256,
                    metadata_json={"storage_area": "documents"},
                )
            )
            session.commit()
            session.refresh(document)
            return document

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
        now = utcnow()
        metadata_json = {**(metadata or {}), "storage_area": storage_area}
        artifact_id = str(uuid.uuid4())
        with self.session_factory() as session:
            statement = sqlite_insert(Artifact).values(
                id=artifact_id,
                document_id=document_id,
                run_id=run_id,
                owner_type=owner_type,
                kind=kind,
                relative_path=relative_path,
                media_type=media_type,
                size_bytes=stored.size_bytes,
                sha256=stored.sha256,
                metadata_json=metadata_json,
                created_at=now,
                updated_at=now,
            )
            statement = statement.on_conflict_do_update(
                index_elements=[Artifact.relative_path],
                set_={
                    "kind": kind,
                    "media_type": media_type,
                    "size_bytes": stored.size_bytes,
                    "sha256": stored.sha256,
                    "metadata_json": metadata_json,
                    "updated_at": now,
                },
                where=and_(
                    Artifact.owner_type == owner_type,
                    Artifact.document_id == document_id,
                    Artifact.run_id == run_id,
                ),
            )
            result = session.execute(statement)
            if result.rowcount == 0:
                session.rollback()
                raise ValueError(f"Artifact path is already owned by another resource: {relative_path}")
            session.commit()
            artifact = session.scalar(select(Artifact).where(Artifact.relative_path == relative_path))
            assert artifact is not None
            return artifact

    def list_documents(self) -> list[Document]:
        with self.session_factory() as session:
            return list(session.scalars(select(Document).order_by(Document.created_at.desc())))

    def get_document(self, document_id: str) -> Document | None:
        with self.session_factory() as session:
            return session.get(Document, document_id)

    def get_document_details(self, document_id: str) -> tuple[Document, list[Artifact], list[DocumentChunk]] | None:
        with self.session_factory() as session:
            document = session.get(Document, document_id)
            if not document:
                return None
            artifacts = list(session.scalars(select(Artifact).where(Artifact.document_id == document_id).order_by(Artifact.created_at.asc())))
            chunks = list(session.scalars(select(DocumentChunk).where(DocumentChunk.document_id == document_id).order_by(DocumentChunk.chunk_index.asc())))
            return document, artifacts, chunks

    def get_artifact(self, artifact_id: str) -> Artifact | None:
        with self.session_factory() as session:
            return session.get(Artifact, artifact_id)

    def delete_document(self, document_id: str) -> bool:
        details = self.get_document_details(document_id)
        if not details:
            return False
        _, artifacts, _ = details
        for artifact in artifacts:
            storage_area = (artifact.metadata_json or {}).get("storage_area", "artifacts")
            base = self.settings.documents_dir if storage_area == "documents" else self.settings.artifacts_dir
            self.storage.delete_stored_file(base, artifact.relative_path)
        with self.session_factory() as session:
            session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document_id))
            session.execute(delete(Artifact).where(Artifact.document_id == document_id))
            document = session.get(Document, document_id)
            if document is not None:
                session.delete(document)
            session.commit()
        return True

    def delete_run_artifact_files(self, run_id: str) -> None:
        with self.session_factory() as session:
            artifacts = list(session.scalars(select(Artifact).where(Artifact.run_id == run_id)))
        for artifact in artifacts:
            storage_area = (artifact.metadata_json or {}).get("storage_area", "artifacts")
            base = self.settings.documents_dir if storage_area == "documents" else self.settings.artifacts_dir
            self.storage.delete_stored_file(base, artifact.relative_path)

    def artifact_bytes(self, artifact: Artifact) -> bytes:
        storage_area = (artifact.metadata_json or {}).get("storage_area", "artifacts")
        base = self.settings.documents_dir if storage_area == "documents" else self.settings.artifacts_dir
        return (base / artifact.relative_path).read_bytes()

    def artifact_content(self, artifact: Artifact) -> Any:
        raw = self.artifact_bytes(artifact)
        if artifact.media_type == "application/json":
            import json

            return json.loads(raw.decode("utf-8"))
        if artifact.media_type.startswith("text/"):
            return raw.decode("utf-8")
        return raw.decode("utf-8", errors="replace")

    async def ingest_document(
        self,
        document_id: str,
        *,
        enhance_with_llm: bool | None = None,
        llm_model: str | None = None,
        model_reference: ModelReference | None = None,
        workflow_defaults: WorkflowModelDefaults | None = None,
        triage_model: str | None = None,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        details = self.get_document_details(document_id)
        if not details:
            raise ValueError(f"Unknown document {document_id}")
        document, artifacts, _ = details
        source = next((artifact for artifact in artifacts if artifact.kind == "source_pdf"), None)
        if not source:
            raise ValueError("Document is missing its source PDF")
        pdf_path = self.settings.documents_dir / source.relative_path
        enhancement_enabled = (
            self.settings.ocr_llm_enhancement_enabled
            if enhance_with_llm is None
            else enhance_with_llm
        )
        enhancement_model: ResolvedModel | None = None
        quality_model: ResolvedModel | None = None
        if enhancement_enabled:
            enhancement_model = self._resolve_enhancement_model(
                llm_model=llm_model,
                model_reference=model_reference,
                workflow_defaults=workflow_defaults,
            )
            quality_model = self._resolve_quality_model(enhancement_model, triage_model)
        pages = await self._extract_pages(
            pdf_path,
            force_ocr=enhancement_enabled,
            retain_page_images=enhancement_enabled,
            progress=progress,
        )
        await self._report_progress(
            progress,
            {
                "phase": "figures",
                "phase_label": "Extracting figures",
                "completed_pages": len(pages),
                "total_pages": len(pages),
            },
        )
        figure_artifacts = self._extract_figures(pdf_path, document.id)
        figures_by_page: dict[int, list[dict[str, Any]]] = {}
        for figure in figure_artifacts:
            figures_by_page.setdefault(figure["page"], []).append(figure)
        for page in pages:
            page["figures"] = figures_by_page.get(page["page"], [])
        if enhancement_enabled:
            assert enhancement_model is not None
            assert quality_model is not None
            await self._enhance_pages(
                pages,
                enhancement_model,
                triage_model=quality_model,
                progress=progress,
            )
        else:
            for page in pages:
                page["text"] = self._append_missing_figure_references(page["text"], page["figures"])
        await self._report_progress(
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
            figure_artifacts,
            enhancement_model=enhancement_model.model if enhancement_model else None,
            triage_model=quality_model.model if quality_model else None,
        )

    def _resolve_enhancement_model(
        self,
        *,
        llm_model: str | None,
        model_reference: ModelReference | None,
        workflow_defaults: WorkflowModelDefaults | None,
    ) -> ResolvedModel:
        reference = model_reference
        workflow_reference = workflow_defaults.vision if workflow_defaults else None
        settings_reference = self.settings.default_model_references.get("vision")
        if reference is None and llm_model:
            reference = ModelReference(model=llm_model)
        if reference is None and workflow_reference is None and not settings_reference and self.settings.ocr_llm_model:
            reference = ModelReference(model=self.settings.ocr_llm_model)
        try:
            return self.model_runtime.resolve(
                "vision",
                node_reference=reference,
                workflow_defaults=workflow_defaults,
            )
        except ProviderRuntimeError as exc:
            raise DocumentProcessingError(str(exc)) from exc

    def _resolve_quality_model(
        self,
        enhancement_model: ResolvedModel,
        triage_model: str | None,
    ) -> ResolvedModel:
        model = triage_model
        if model is None and enhancement_model.kind == "ollama":
            model = self.settings.ocr_llm_triage_model
        if not model:
            return enhancement_model
        try:
            return self.model_runtime.resolve(
                "vision",
                node_reference=ModelReference(
                    provider_profile_id=enhancement_model.profile_id,
                    model=model,
                ),
            )
        except ProviderRuntimeError as exc:
            raise DocumentProcessingError(str(exc)) from exc

    async def enhance_document_page(
        self,
        document_id: str,
        page_number: int,
        *,
        llm_model: str | None = None,
        model_reference: ModelReference | None = None,
        workflow_defaults: WorkflowModelDefaults | None = None,
        triage_model: str | None = None,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        details = self.get_document_details(document_id)
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
        manifest = self.artifact_content(manifest_artifact)
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
        model = self._resolve_enhancement_model(
            llm_model=llm_model,
            model_reference=model_reference,
            workflow_defaults=workflow_defaults,
        )
        quality_model = self._resolve_quality_model(model, triage_model)
        pdf_path = self.settings.documents_dir / source.relative_path
        await self._report_progress(
            progress,
            {
                "phase": "rendering",
                "phase_label": f"Rendering page {page_number}",
                "completed_pages": 0,
                "total_pages": 1,
                "current_page": page_number,
            },
        )
        page["_image_png"] = await asyncio.to_thread(
            self._render_page_png,
            pdf_path,
            page_number - 1,
        )
        await self._enhance_pages(
            [page],
            model,
            triage_model=quality_model,
            progress=progress,
            force_repair=True,
        )
        figures = manifest.get("figures", [])
        if not isinstance(figures, list):
            figures = []
        result = self._persist_document_content(
            document,
            pages,
            figures,
            enhancement_model=model.model,
            triage_model=quality_model.model,
        )
        result["page"] = {
            key: value for key, value in page.items() if not key.startswith("_")
        }
        return result

    def _persist_document_content(
        self,
        document: Document,
        pages: list[dict[str, Any]],
        figure_artifacts: list[dict[str, Any]],
        *,
        enhancement_model: str | None,
        triage_model: str | None = None,
    ) -> dict[str, Any]:
        serializable_pages = [
            {key: value for key, value in page.items() if not key.startswith("_")}
            for page in pages
        ]
        chunks = self._chunk_pages(pages, title=document.title)
        markdown = self._build_markdown(document.title, pages)
        manifest = {
            "document_id": document.id,
            "title": document.title,
            "pages": serializable_pages,
            "chunks": chunks,
            "figures": figure_artifacts,
        }
        markdown_path = f"documents/{document.id}/extracted.md"
        json_path = f"documents/{document.id}/manifest.json"
        stored_md = self.storage.write_text(self.settings.artifacts_dir, markdown_path, markdown)
        stored_json = self.storage.write_json(self.settings.artifacts_dir, json_path, manifest)
        md_artifact = self.create_artifact_record(
            owner_type="document",
            kind="extracted_markdown",
            document_id=document.id,
            relative_path=markdown_path,
            media_type="text/markdown",
            stored=stored_md,
        )
        json_artifact = self.create_artifact_record(
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
        with self.session_factory() as session:
            row = session.get(Document, document.id)
            assert row is not None
            row.status = "ready"
            row.page_count = len(pages)
            row.metadata_json = {
                "ocr_available": self.ocr_available(),
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
                "figure_count": len(figure_artifacts),
            }
            session.commit()
        return {
            "document_id": document.id,
            "page_count": len(pages),
            "chunks": chunks,
            "artifact_ids": [
                md_artifact.id,
                json_artifact.id,
                *(
                    figure["artifact_id"]
                    for figure in figure_artifacts
                    if isinstance(figure, dict) and "artifact_id" in figure
                ),
            ],
            "text": "\n\n".join(page["text"] for page in pages if page["text"]),
        }

    async def _extract_pages(
        self,
        pdf_path: Path,
        *,
        force_ocr: bool = False,
        retain_page_images: bool = False,
        progress: ProgressCallback | None = None,
    ) -> list[dict[str, Any]]:
        reader = PdfReader(str(pdf_path))
        pages: list[dict[str, Any]] = []
        total_pages = len(reader.pages)
        started_at = time.monotonic()
        await self._report_progress(
            progress,
            {
                "phase": "ocr",
                "phase_label": "Rendering and OCR",
                "completed_pages": 0,
                "total_pages": total_pages,
            },
        )
        for index, page in enumerate(reader.pages, start=1):
            embedded_text = (page.extract_text() or "").strip()
            text = embedded_text
            ocr_used = False
            ocr_fallback_note: str | None = None
            page_image = b""
            if force_ocr or len(text) < self.settings.pdf_min_text_chars:
                if not self.ocr_available():
                    raise DocumentProcessingError(
                        f"Page {index} requires OCR, but Tesseract and the "
                        f"{self.settings.ocr_language!r} language data are unavailable"
                    )
                page_image = await asyncio.to_thread(self._render_page_png, pdf_path, index - 1)
                try:
                    ocr_text = await self._ocr_image(page_image, index)
                except DocumentProcessingError as exc:
                    if not embedded_text:
                        raise
                    ocr_text = ""
                    ocr_fallback_note = (
                        f"OCR failed for page {index}; the PDF's embedded text was used instead. "
                        f"Details: {exc}"
                    )
                if ocr_text:
                    text = ocr_text.strip()
                    ocr_used = True
            entry: dict[str, Any] = {
                "page": index,
                "raw_text": text,
                "text": text,
                "ocr_used": ocr_used,
                "llm_enhanced": False,
            }
            if ocr_fallback_note:
                entry["ocr_fallback_note"] = ocr_fallback_note
            if retain_page_images:
                if not page_image:
                    page_image = await asyncio.to_thread(self._render_page_png, pdf_path, index - 1)
                entry["_image_png"] = page_image
            pages.append(entry)
            elapsed = time.monotonic() - started_at
            average = elapsed / index
            await self._report_progress(
                progress,
                {
                    "phase": "ocr",
                    "phase_label": "Rendering and OCR",
                    "completed_pages": index,
                    "total_pages": total_pages,
                    "current_page": index,
                    "elapsed_seconds": round(elapsed, 1),
                    "average_seconds_per_page": round(average, 1),
                    "eta_seconds": round(average * (total_pages - index), 1),
                },
            )
        return pages

    async def _ocr_page(self, pdf_path: Path, zero_based_page_index: int) -> str:
        if not self.ocr_available():
            return ""
        image_png = await asyncio.to_thread(self._render_page_png, pdf_path, zero_based_page_index)
        return await self._ocr_image(image_png, zero_based_page_index + 1)

    async def _ocr_image(self, image_png: bytes, page_number: int) -> str:
        import pytesseract
        from PIL import Image

        try:
            with Image.open(io.BytesIO(image_png)) as image:
                return await asyncio.to_thread(
                    pytesseract.image_to_string,
                    image,
                    lang=self.settings.ocr_language,
                    config=self._tesseract_config(),
                )
        except (OSError, pytesseract.TesseractError, pytesseract.TesseractNotFoundError) as exc:
            raise DocumentProcessingError(f"OCR failed for page {page_number}: {exc}") from exc

    @staticmethod
    def _render_page_png(pdf_path: Path, zero_based_page_index: int) -> bytes:
        import pypdfium2

        document = pypdfium2.PdfDocument(str(pdf_path))
        try:
            page = document[zero_based_page_index]
            bitmap = page.render(scale=2)
            image = bitmap.to_pil()
            output = io.BytesIO()
            image.save(output, format="PNG")
            return output.getvalue()
        finally:
            document.close()

    def _extract_figures(self, pdf_path: Path, document_id: str) -> list[dict[str, Any]]:
        reader = PdfReader(str(pdf_path))
        figures: list[dict[str, Any]] = []
        for page_number, page in enumerate(reader.pages, start=1):
            figure_number = 0
            for embedded_image in page.images:
                image = embedded_image.image
                width, height = image.size
                if width < 128 or height < 128 or width * height < 40_000:
                    continue
                figure_number += 1
                output = io.BytesIO()
                image.save(output, format="PNG")
                relative_path = (
                    f"documents/{document_id}/figures/"
                    f"page-{page_number:04d}-figure-{figure_number:02d}.png"
                )
                stored = self.storage.write_bytes(
                    self.settings.artifacts_dir,
                    relative_path,
                    output.getvalue(),
                )
                artifact = self.create_artifact_record(
                    owner_type="document",
                    kind="extracted_figure",
                    document_id=document_id,
                    relative_path=relative_path,
                    media_type="image/png",
                    stored=stored,
                    metadata={
                        "page": page_number,
                        "figure": figure_number,
                        "source_name": embedded_image.name,
                        "width": width,
                        "height": height,
                    },
                )
                figures.append(
                    {
                        "page": page_number,
                        "figure": figure_number,
                        "artifact_id": artifact.id,
                        "path": f"/api/artifacts/{artifact.id}/raw",
                        "alt": f"Extracted figure {figure_number} from page {page_number}",
                        "width": width,
                        "height": height,
                    }
                )
        return figures

    async def _enhance_pages(
        self,
        pages: list[dict[str, Any]],
        model: str | ResolvedModel,
        *,
        triage_model: str | ResolvedModel | None = None,
        progress: ProgressCallback | None = None,
        force_repair: bool = False,
    ) -> None:
        """Triage OCR quality with a small model and only repair the poor pages.

        Pages rated ``good`` or ``average`` keep their OCR text untouched, which
        avoids a slow vision-model rewrite for the majority of a typical paper.
        """
        resolved_model = self._coerce_vision_model(model)
        quality_model = (
            triage_model
            if isinstance(triage_model, ResolvedModel)
            else self._coerce_vision_model(triage_model, profile_id=resolved_model.profile_id)
            if triage_model
            else resolved_model
        )
        if force_repair:
            for page in pages:
                page.setdefault("ocr_quality_issues", [])
            targets = list(pages)
        else:
            targets = await self._triage_pages(pages, quality_model, progress=progress)
        repaired = await self._repair_pages(targets, resolved_model, progress=progress)
        await self._validate_repaired_pages(repaired, quality_model, progress=progress)

    def _coerce_vision_model(
        self,
        model: str | ResolvedModel,
        *,
        profile_id: str | None = None,
    ) -> ResolvedModel:
        if isinstance(model, ResolvedModel):
            return model
        return self.model_runtime.resolve(
            "vision",
            node_reference=ModelReference(provider_profile_id=profile_id, model=model),
        )

    async def _generate_vision(
        self,
        model: ResolvedModel,
        prompt: str,
        *,
        format_: dict[str, Any] | str | None = None,
        image_png: bytes,
    ) -> dict[str, Any]:
        image = base64.b64encode(image_png).decode("ascii")
        ollama_base_url = getattr(self.ollama, "base_url", model.base_url)
        if model.kind == "ollama" and str(ollama_base_url).rstrip("/") == model.base_url.rstrip("/"):
            kwargs: dict[str, Any] = {
                "stream": False,
                "options": {"temperature": 0},
                "images": [image],
                "think": False,
            }
            if format_ is not None:
                kwargs["format_"] = format_
            return await self.ollama.generate(model.model, prompt, **kwargs)
        return await self.model_runtime.generate(
            model,
            prompt,
            temperature=0,
            format_=format_,
            images=[image],
            think=False,
        )

    async def _triage_pages(
        self,
        pages: list[dict[str, Any]],
        model: ResolvedModel,
        *,
        progress: ProgressCallback | None = None,
    ) -> list[dict[str, Any]]:
        durations: list[float] = []
        total_pages = len(pages)
        poor_pages: list[dict[str, Any]] = []
        await self._report_progress(
            progress,
            {
                "phase": "ocr_triage",
                "phase_label": "Checking OCR quality",
                "completed_pages": 0,
                "total_pages": total_pages,
            },
        )
        for completed, page in enumerate(pages, start=1):
            page_started_at = time.monotonic()
            image_png = page.get("_image_png")
            if not isinstance(image_png, bytes):
                raise DocumentProcessingError(
                    f"Rendered image is missing for OCR triage of page {page['page']}"
                )
            ocr_text = str(page.get("raw_text") or "").strip()
            if not ocr_text:
                quality = "poor"
                issues = ["OCR produced no text for this page."]
            else:
                prompt = (
                    "You are grading how faithfully an OCR transcription captured a single PDF "
                    "page. Compare the OCR TEXT against the supplied page image and rate it.\n\n"
                    "Rate 'good' when the OCR text is complete and accurate: reading order is "
                    "correct, no meaningful text is missing, numbers and citations match the "
                    "image, and equations and tables remain understandable.\n"
                    "Rate 'average' when the text is usable but imperfect, for example a few "
                    "garbled words, dropped headers or footnotes, or loose table formatting.\n"
                    "Rate 'poor' when the text needs rework: significant content is missing, "
                    "the reading order is scrambled, columns are interleaved, numbers or "
                    "equations are corrupted, or tables are destroyed.\n\n"
                    "Do not rewrite or transcribe the page. List concise, specific issues and "
                    "return only the requested JSON object.\n\n"
                    f"OCR TEXT:\n{ocr_text}"
                )
                try:
                    response = await self._generate_vision(
                        model,
                        prompt,
                        format_=OCR_QUALITY_SCHEMA,
                        image_png=image_png,
                    )
                except (OllamaError, OpenAIError, ProviderRuntimeError) as exc:
                    quality = "poor"
                    issues = [f"OCR quality check failed: {exc}"]
                else:
                    assessment, parse_error = self._parse_quality_response(response)
                    if parse_error:
                        quality = "poor"
                        issues = [parse_error]
                    else:
                        quality = assessment["quality"]
                        issues = assessment["issues"]
            page["ocr_quality"] = quality
            page["ocr_quality_issues"] = issues
            if quality == "poor":
                poor_pages.append(page)
            else:
                self._keep_ocr_text(page)
            durations.append(time.monotonic() - page_started_at)
            average = sum(durations) / len(durations)
            await self._report_progress(
                progress,
                {
                    "phase": "ocr_triage",
                    "phase_label": "Checking OCR quality",
                    "completed_pages": completed,
                    "total_pages": total_pages,
                    "current_page": page["page"],
                    "page_quality": quality,
                    "pages_needing_repair": len(poor_pages),
                    "page_seconds": round(durations[-1], 1),
                    "average_seconds_per_page": round(average, 1),
                    "eta_seconds": round(average * (total_pages - completed), 1),
                },
            )
        return poor_pages

    async def _repair_pages(
        self,
        pages: list[dict[str, Any]],
        model: ResolvedModel,
        *,
        progress: ProgressCallback | None = None,
    ) -> list[dict[str, Any]]:
        if not pages:
            return []
        durations: list[float] = []
        total_pages = len(pages)
        repaired: list[dict[str, Any]] = []
        await self._report_progress(
            progress,
            {
                "phase": "llm_enhancement",
                "phase_label": "Rewriting low-quality OCR pages",
                "completed_pages": 0,
                "total_pages": total_pages,
            },
        )
        for completed, page in enumerate(pages, start=1):
            page_started_at = time.monotonic()
            image_png = page.get("_image_png")
            if not isinstance(image_png, bytes):
                raise DocumentProcessingError(f"Rendered image is missing for page {page['page']}")
            figures = page.get("figures", [])
            figure_lines = self._figure_markdown(figures)
            prompt = (
                "Transcribe and reconstruct this single PDF page as faithful Markdown using the "
                "page image as the source of truth and the OCR text as a draft. "
                "Correct OCR errors only when the image supports the correction. Preserve every "
                "heading, paragraph, list, equation, footnote, and table; use GitHub-flavored "
                "Markdown tables when appropriate. Do not summarize, explain, or invent content. "
                "You must return the reconstructed page, even when the OCR draft is sparse. "
                "Return only the page Markdown without analysis or a code fence.\n\n"
                f"OCR TEXT:\n{page.get('raw_text') or '(empty)'}"
            )
            quality_issues = page.get("ocr_quality_issues")
            if quality_issues:
                joined = "\n".join(f"- {issue}" for issue in quality_issues)
                prompt += (
                    "\n\nA quality check flagged the following problems with the OCR draft. "
                    f"Pay particular attention to them:\n{joined}"
                )
            if figure_lines:
                prompt += (
                    "\n\nThe following local figure references were extracted from this page. "
                    "Place each reference once near its caption or most relevant surrounding text, "
                    "without changing its URL or alt text:\n"
                    f"{figure_lines}"
                )
            try:
                response = await self._generate_vision(
                    model,
                    prompt,
                    image_png=image_png,
                )
            except (OllamaError, OpenAIError, ProviderRuntimeError) as exc:
                self._use_ocr_fallback(page, f"the LLM request failed: {exc}")
            else:
                enhanced = self._strip_markdown_fence(self._ollama_response_text(response))
                if not enhanced:
                    detail = (
                        "the model returned only an internal thinking trace"
                        if response.get("thinking")
                        else "the model returned no page content"
                    )
                    self._use_ocr_fallback(page, detail)
                else:
                    page["text"] = self._append_missing_figure_references(enhanced, figures)
                    page["llm_enhanced"] = True
                    page["llm_validation_status"] = "pending"
                    page.pop("llm_enhancement_note", None)
                    repaired.append(page)
            durations.append(time.monotonic() - page_started_at)
            average = sum(durations) / len(durations)
            await self._report_progress(
                progress,
                {
                    "phase": "llm_enhancement",
                    "phase_label": "Rewriting low-quality OCR pages",
                    "completed_pages": completed,
                    "total_pages": total_pages,
                    "current_page": page["page"],
                    "page_seconds": round(durations[-1], 1),
                    "average_seconds_per_page": round(average, 1),
                    "eta_seconds": round(average * (total_pages - completed), 1),
                },
            )
        return repaired

    async def _validate_repaired_pages(
        self,
        pages: list[dict[str, Any]],
        model: ResolvedModel,
        *,
        progress: ProgressCallback | None = None,
    ) -> None:
        if not pages:
            return
        durations: list[float] = []
        total_pages = len(pages)
        await self._report_progress(
            progress,
            {
                "phase": "llm_validation",
                "phase_label": "Validating rewritten pages",
                "completed_pages": 0,
                "total_pages": total_pages,
            },
        )
        for completed, page in enumerate(pages, start=1):
            page_started_at = time.monotonic()
            image_png = page.get("_image_png")
            if not isinstance(image_png, bytes):
                raise DocumentProcessingError(
                    f"Rendered image is missing for validation of page {page['page']}"
                )
            prompt = (
                "You are grading a Markdown reconstruction of a single PDF page. Compare the "
                "CANDIDATE MARKDOWN against the supplied page image and the OCR draft.\n\n"
                "Rate 'good' when the candidate faithfully represents the page.\n"
                "Rate 'average' when it represents the page but has minor formatting or "
                "wording slips.\n"
                "Rate 'poor' when it omits meaningful content, invents content, changes numbers "
                "or citations, scrambles reading order, corrupts equations, or materially "
                "misrepresents tables.\n\n"
                "Local /api/artifacts/ image references are system-added and must not lower the "
                "rating. List concise, specific issues and return only the requested JSON "
                "object.\n\n"
                f"OCR DRAFT:\n{page.get('raw_text') or '(empty)'}\n\n"
                f"CANDIDATE MARKDOWN:\n{page.get('text') or '(empty)'}"
            )
            try:
                response = await self._generate_vision(
                    model,
                    prompt,
                    format_=OCR_QUALITY_SCHEMA,
                    image_png=image_png,
                )
            except (OllamaError, OpenAIError, ProviderRuntimeError) as exc:
                self._reject_enhancement(page, [f"Validation request failed: {exc}"])
            else:
                assessment, parse_error = self._parse_quality_response(response)
                if parse_error:
                    self._reject_enhancement(page, [parse_error])
                elif assessment["quality"] == "poor":
                    issues = assessment["issues"] or [
                        "The validation model rated the rewritten page as poor without details."
                    ]
                    self._reject_enhancement(page, issues)
                else:
                    page["llm_validation_status"] = "passed"
                    page["llm_validation_quality"] = assessment["quality"]
                    page["llm_validation_issues"] = assessment["issues"]
            durations.append(time.monotonic() - page_started_at)
            average = sum(durations) / len(durations)
            await self._report_progress(
                progress,
                {
                    "phase": "llm_validation",
                    "phase_label": "Validating rewritten pages",
                    "completed_pages": completed,
                    "total_pages": total_pages,
                    "current_page": page["page"],
                    "page_seconds": round(durations[-1], 1),
                    "average_seconds_per_page": round(average, 1),
                    "eta_seconds": round(average * (total_pages - completed), 1),
                },
            )

    def _keep_ocr_text(self, page: dict[str, Any]) -> None:
        page["text"] = self._append_missing_figure_references(
            str(page.get("raw_text") or page.get("text") or ""),
            page.get("figures", []),
        )
        page["llm_enhanced"] = False
        for key in ("llm_enhancement_note", "llm_validation_status", "llm_validation_issues", "llm_validation_quality"):
            page.pop(key, None)

    def _reject_enhancement(self, page: dict[str, Any], issues: list[str]) -> None:
        page["llm_validation_status"] = "failed"
        page["llm_validation_issues"] = issues
        self._use_ocr_fallback(
            page,
            f"the validation check rejected it: {'; '.join(issues)}",
        )

    def _parse_quality_response(
        self,
        response: dict[str, Any],
    ) -> tuple[dict[str, Any], str | None]:
        raw = self._ollama_response_text(response)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            return {}, f"The quality model returned invalid JSON: {exc.msg}"
        if not isinstance(payload, dict):
            return {}, "The quality model returned a non-object result."
        quality = payload.get("quality")
        issues = payload.get("issues", [])
        if isinstance(quality, str):
            quality = quality.strip().lower()
        if quality not in OCR_QUALITY_LEVELS:
            return {}, "The quality model did not return a good/average/poor rating."
        if not isinstance(issues, list) or not all(isinstance(issue, str) for issue in issues):
            return {}, "The quality model returned an invalid issues list."
        return {"quality": quality, "issues": issues}, None

    @staticmethod
    async def _report_progress(
        progress: ProgressCallback | None,
        payload: dict[str, Any],
    ) -> None:
        if progress is not None:
            await progress(payload)

    def _use_ocr_fallback(self, page: dict[str, Any], reason: str) -> None:
        page["text"] = self._append_missing_figure_references(
            str(page.get("raw_text") or page.get("text") or ""),
            page.get("figures", []),
        )
        page["llm_enhanced"] = False
        page["llm_enhancement_note"] = (
            f"No LLM enhancement was applied because {reason}. "
            "The original OCR output is used directly."
        )

    @staticmethod
    def _ollama_response_text(response: dict[str, Any]) -> str:
        content = response.get("response")
        if not content and isinstance(response.get("message"), dict):
            content = response["message"].get("content")
        return str(content or "").strip()

    @staticmethod
    def _strip_markdown_fence(text: str) -> str:
        match = re.fullmatch(r"```(?:markdown|md)?\s*\n(.*?)\n```", text, flags=re.DOTALL | re.IGNORECASE)
        return match.group(1).strip() if match else text

    @staticmethod
    def _figure_markdown(figures: list[dict[str, Any]]) -> str:
        return "\n".join(f"![{figure['alt']}]({figure['path']})" for figure in figures)

    def _append_missing_figure_references(
        self,
        text: str,
        figures: list[dict[str, Any]],
    ) -> str:
        missing = [figure for figure in figures if figure["path"] not in text]
        if not missing:
            return text
        references = self._figure_markdown(missing)
        return f"{text.rstrip()}\n\n### Figures\n\n{references}".strip()

    def _chunk_pages(self, pages: list[dict[str, Any]], *, title: str) -> list[dict[str, Any]]:
        chunks: list[dict[str, Any]] = []
        buffer: list[str] = []
        buffer_pages: list[int] = []
        current_section = title

        def flush() -> None:
            if not buffer:
                return
            text = "\n\n".join(buffer).strip()
            if not text:
                return
            page_start = min(buffer_pages)
            page_end = max(buffer_pages)
            citation = f"p.{page_start}" if page_start == page_end else f"pp.{page_start}-{page_end}"
            chunks.append(
                {
                    "section_title": current_section,
                    "page_start": page_start,
                    "page_end": page_end,
                    "citation": citation,
                    "text": text,
                    "metadata": {"source": "pdf_ingest"},
                }
            )
            buffer.clear()
            buffer_pages.clear()

        for page in pages:
            paragraphs = [part.strip() for part in re.split(r"\n\s*\n", page["text"]) if part.strip()]
            if not paragraphs and page["text"].strip():
                paragraphs = [page["text"].strip()]
            for paragraph in paragraphs:
                if page.get("llm_enhanced"):
                    normalized = paragraph.strip()
                else:
                    normalized = " ".join(line.strip() for line in paragraph.splitlines() if line.strip())
                if not normalized:
                    continue
                if self._looks_like_heading(normalized):
                    flush()
                    current_section = normalized[:120]
                    continue
                projected = len("\n\n".join(buffer + [normalized]))
                if buffer and projected > self.settings.max_chunk_chars:
                    flush()
                buffer.append(normalized)
                buffer_pages.append(page["page"])
        flush()
        return chunks[: self.settings.max_chunks_per_document]

    @staticmethod
    def _looks_like_heading(text: str) -> bool:
        stripped = text.strip()
        if "\n" in stripped or len(stripped) > 120:
            return False
        return bool(re.match(r"^(?:\d+(?:\.\d+)*)?\s*[A-Z][A-Za-z0-9 ,:_-]{2,}$", stripped)) and stripped == stripped.title() or stripped.isupper()

    @staticmethod
    def _build_markdown(title: str, pages: list[dict[str, Any]]) -> str:
        parts = [f"# {title}"]
        for page in pages:
            parts.append(f"\n## Page {page['page']}\n")
            if page.get("llm_enhancement_note"):
                parts.append(f"> **OCR note:** {page['llm_enhancement_note']}")
            parts.append(page["text"] or "*(No text extracted)*")
        return "\n\n".join(parts)
