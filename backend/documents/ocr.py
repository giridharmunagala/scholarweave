from __future__ import annotations

import importlib.util
import io
import os
import shutil
import time
from functools import partial
from pathlib import Path
from typing import Any

import anyio
from pypdf import PdfReader

from backend.core.config import Settings
from backend.documents.errors import (
    DocumentProcessingError,
    ProgressCallback,
    report_progress,
)

class DocumentOCR:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._docling_lock = anyio.Lock()
        self._docling_converters: dict[tuple[str, str, int, int, bool], Any] = {}

    def available(self) -> bool:
        if importlib.util.find_spec("pypdfium2") is None:
            return False
        if self.settings.ocr_engine == "docling":
            return importlib.util.find_spec("docling") is not None
        try:
            import pytesseract
        except ImportError:
            return False
        if shutil.which("tesseract") is None:
            return False
        try:
            return self.settings.ocr_language in pytesseract.get_languages(
                config=self._tesseract_config()
            )
        except (
            OSError,
            pytesseract.TesseractError,
            pytesseract.TesseractNotFoundError,
        ):
            return False

    def inspect_text_layer(self, pdf_path: Path) -> dict[str, int | float | str]:
        reader = PdfReader(str(pdf_path))
        total_pages = len(reader.pages)
        embedded_text_pages = sum(
            1
            for page in reader.pages
            if len((page.extract_text() or "").strip()) >= self.settings.pdf_min_text_chars
        )
        embedded_text_ratio = (
            embedded_text_pages / total_pages if total_pages else 0.0
        )
        return {
            "total_pages": total_pages,
            "embedded_text_pages": embedded_text_pages,
            "embedded_text_ratio": embedded_text_ratio,
            "recommended_mode": "embedded" if embedded_text_ratio >= 0.8 else "ocr",
        }

    async def extract_pages(
        self,
        pdf_path: Path,
        *,
        force_ocr: bool = False,
        retain_page_images: bool = False,
        progress: ProgressCallback | None = None,
    ) -> list[dict[str, Any]]:
        if self.settings.ocr_engine == "docling":
            return await self._extract_docling_document(
                pdf_path,
                force_ocr=force_ocr,
                retain_page_images=retain_page_images,
                progress=progress,
            )
        return await self._extract_pages(
            pdf_path,
            force_ocr=force_ocr,
            retain_page_images=retain_page_images,
            progress=progress,
        )

    async def _extract_pages(
        self,
        pdf_path: Path,
        *,
        force_ocr: bool,
        retain_page_images: bool,
        progress: ProgressCallback | None,
    ) -> list[dict[str, Any]]:
        reader = PdfReader(str(pdf_path))
        pages: list[dict[str, Any]] = []
        total_pages = len(reader.pages)
        started_at = time.monotonic()
        phase_label = "Rendering and OCR"
        await report_progress(
            progress,
            {
                "phase": "ocr",
                "phase_label": phase_label,
                "completed_pages": 0,
                "total_pages": total_pages,
            },
        )
        availability_checked = False
        for index, page in enumerate(reader.pages, start=1):
            embedded_text = (page.extract_text() or "").strip()
            text = embedded_text
            page_image = b""
            needs_ocr = force_ocr or len(text) < self.settings.pdf_min_text_chars
            if needs_ocr:
                if not availability_checked:
                    availability_checked = True
                    if not self.available():
                        raise self._unavailable_error(index)
                page_image = await anyio.to_thread.run_sync(
                    self.render_page_png,
                    pdf_path,
                    index - 1,
                )

            entry: dict[str, Any] = {
                "page": index,
                "raw_text": text,
                "text": text,
                "ocr_used": False,
                "llm_enhanced": False,
            }
            if needs_ocr:
                try:
                    ocr_text = await self._tesseract_image(page_image, index)
                except DocumentProcessingError as exc:
                    if not embedded_text:
                        raise
                    entry["ocr_fallback_note"] = (
                        f"OCR failed for page {index}; the PDF's embedded text was "
                        f"used instead. Details: {exc}"
                    )
                else:
                    if ocr_text.strip():
                        entry["raw_text"] = ocr_text.strip()
                        entry["text"] = ocr_text.strip()
                        entry["ocr_used"] = True
                        entry["ocr_engine"] = "tesseract"

            if retain_page_images:
                if not page_image:
                    page_image = await anyio.to_thread.run_sync(
                        self.render_page_png,
                        pdf_path,
                        index - 1,
                    )
                entry["_image_png"] = page_image
            pages.append(entry)
            await self._report_page_progress(
                progress,
                started_at,
                index,
                total_pages,
                phase_label,
            )

        return pages

    async def _extract_docling_document(
        self,
        pdf_path: Path,
        *,
        force_ocr: bool,
        retain_page_images: bool,
        progress: ProgressCallback | None,
    ) -> list[dict[str, Any]]:
        if not self.available():
            raise self._unavailable_error(1)
        reader = PdfReader(str(pdf_path))
        embedded_text = [(page.extract_text() or "").strip() for page in reader.pages]
        total_pages = len(embedded_text)
        await report_progress(
            progress,
            {
                "phase": "ocr",
                "phase_label": "Parsing document with Docling",
                "completed_pages": 0,
                "total_pages": total_pages,
            },
        )
        extracted = await self._docling_pdf_pages(
            pdf_path,
            list(range(1, total_pages + 1)),
            force_ocr=force_ocr,
            progress=progress,
        )
        pages: list[dict[str, Any]] = []
        for page_number, source_text in enumerate(embedded_text, start=1):
            text = extracted.get(page_number, "").strip()
            if not text:
                text = source_text
            if not text:
                raise DocumentProcessingError(
                    f"Docling returned no text for page {page_number}"
                )
            entry: dict[str, Any] = {
                "page": page_number,
                "raw_text": text,
                "text": text,
                "ocr_used": force_ocr
                or len(source_text) < self.settings.pdf_min_text_chars,
                "ocr_engine": "docling",
                "llm_enhanced": False,
            }
            if retain_page_images:
                entry["_image_png"] = await anyio.to_thread.run_sync(
                    self.render_page_png,
                    pdf_path,
                    page_number - 1,
                )
            pages.append(entry)
        return pages

    async def ocr_page(self, pdf_path: Path, zero_based_page_index: int) -> str:
        if not self.available():
            return ""
        if self.settings.ocr_engine == "docling":
            page_number = zero_based_page_index + 1
            return (
                await self._docling_pdf_pages(
                    pdf_path,
                    [page_number],
                    force_ocr=True,
                )
            )[page_number]
        image_png = await anyio.to_thread.run_sync(
            self.render_page_png,
            pdf_path,
            zero_based_page_index,
        )
        return await self.ocr_image(image_png, zero_based_page_index + 1)

    async def ocr_image(self, image_png: bytes, page_number: int) -> str:
        if self.settings.ocr_engine == "docling":
            raise DocumentProcessingError(
                "Docling OCR requires a PDF page; standalone image OCR is unsupported."
            )
        return await self._tesseract_image(image_png, page_number)

    async def _tesseract_image(self, image_png: bytes, page_number: int) -> str:
        import pytesseract
        from PIL import Image

        try:
            with Image.open(io.BytesIO(image_png)) as image:
                return await anyio.to_thread.run_sync(
                    partial(
                        pytesseract.image_to_string,
                        image,
                        lang=self.settings.ocr_language,
                        config=self._tesseract_config(),
                    )
                )
        except (
            OSError,
            pytesseract.TesseractError,
            pytesseract.TesseractNotFoundError,
        ) as exc:
            raise DocumentProcessingError(
                f"OCR failed for page {page_number}: {exc}"
            ) from exc

    async def _docling_pdf_pages(
        self,
        pdf_path: Path,
        page_numbers: list[int],
        *,
        force_ocr: bool = True,
        progress: ProgressCallback | None = None,
    ) -> dict[int, str]:
        results: dict[int, str] = {}
        batch_size = self.settings.docling_batch_size
        async with self._docling_lock:
            converter = self._get_docling_converter(force_ocr=force_ocr)
            for offset in range(0, len(page_numbers), batch_size):
                batch = page_numbers[offset : offset + batch_size]
                try:
                    conversion = await anyio.to_thread.run_sync(
                        partial(
                            converter.convert,
                            pdf_path,
                            page_range=(batch[0], batch[-1]),
                        )
                    )
                except Exception as exc:
                    raise DocumentProcessingError(f"Docling OCR failed: {exc}") from exc
                for page_number in batch:
                    results[page_number] = conversion.document.export_to_markdown(
                        page_no=page_number
                    )
                await report_progress(
                    progress,
                    {
                        "phase": "ocr",
                        "phase_label": "Parsing document with Docling",
                        "completed_pages": min(offset + len(batch), len(page_numbers)),
                        "total_pages": len(page_numbers),
                        "current_page": batch[-1],
                    },
                )
        return results

    def _get_docling_converter(self, *, force_ocr: bool) -> Any:
        signature = (
            self.settings.docling_device,
            self.settings.docling_ocr_backend,
            self.settings.docling_batch_size,
            self.settings.docling_num_threads,
            force_ocr,
        )
        existing = self._docling_converters.get(signature)
        if existing is not None:
            return existing

        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import (
            AcceleratorOptions,
            OcrMode,
            PdfPipelineOptions,
            RapidOcrOptions,
            TableFormerMode,
        )
        from docling.document_converter import DocumentConverter, PdfFormatOption

        pipeline_options = PdfPipelineOptions(
            accelerator_options=AcceleratorOptions(
                device=self.settings.docling_device,
                num_threads=self.settings.docling_num_threads,
            ),
            do_ocr=True,
            do_code_enrichment=True,
            do_formula_enrichment=True,
            ocr_options=RapidOcrOptions(
                backend=self.settings.docling_ocr_backend,
                lang=["english"],
                mode=(
                    OcrMode.FULL_PAGE
                    if force_ocr
                    else OcrMode.PDF_AWARE_LAYOUT_REGIONS
                ),
            ),
            ocr_batch_size=self.settings.docling_batch_size,
            layout_batch_size=self.settings.docling_batch_size,
            table_batch_size=self.settings.docling_batch_size,
        )
        pipeline_options.layout_options.engine_options.compile_model = False
        pipeline_options.table_structure_options.mode = TableFormerMode.ACCURATE
        converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
            }
        )
        self._docling_converters[signature] = converter
        return converter

    def _unavailable_error(self, page_number: int) -> DocumentProcessingError:
        if self.settings.ocr_engine == "docling":
            return DocumentProcessingError(
                f"Page {page_number} requires OCR, but Docling is unavailable."
            )
        return DocumentProcessingError(
            f"Page {page_number} requires OCR, but Tesseract and the "
            f"{self.settings.ocr_language!r} language data are unavailable"
        )

    @staticmethod
    async def _report_page_progress(
        progress: ProgressCallback | None,
        started_at: float,
        index: int,
        total_pages: int,
        phase_label: str,
    ) -> None:
        elapsed = time.monotonic() - started_at
        average = elapsed / index
        await report_progress(
            progress,
            {
                "phase": "ocr",
                "phase_label": phase_label,
                "completed_pages": index,
                "total_pages": total_pages,
                "current_page": index,
                "elapsed_seconds": round(elapsed, 1),
                "average_seconds_per_page": round(average, 1),
                "eta_seconds": round(average * (total_pages - index), 1),
            },
        )

    @staticmethod
    def render_page_png(pdf_path: Path, zero_based_page_index: int) -> bytes:
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
