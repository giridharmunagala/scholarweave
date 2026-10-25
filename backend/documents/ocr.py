from __future__ import annotations

import io
import logging
import os
import shutil
import time
from collections.abc import Awaitable, Callable
from contextlib import closing
from functools import partial
from pathlib import Path
from threading import Lock
from typing import Any

import anyio
import pypdfium2
import pytesseract
from PIL import Image
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from backend.core.config import Settings
from backend.core.errors import DocumentProcessingError
from backend.documents.indra import IndraOCR
from backend.documents.surya import SuryaOCR
from backend.utils import ProgressCallback, report_progress

_logger = logging.getLogger(__name__)
_PAGE_ERRORS = (PyPdfError, pypdfium2.PdfiumError, OSError, ValueError, DocumentProcessingError)
_PDFIUM_LOCK = Lock()


class DocumentOCR:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.surya = SuryaOCR(settings)
        self.indra = IndraOCR()
        self._surya_limiter = anyio.CapacityLimiter(settings.ocr_surya_parallel)

    def available(self) -> bool:
        if self.settings.ocr_engine == "surya":
            return self.surya.available()
        tesseract_path = self._tesseract_path()
        if tesseract_path is None:
            return False
        pytesseract.pytesseract.tesseract_cmd = tesseract_path
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
        embedded_text_pages = 0
        for index in range(total_pages):
            try:
                text = self._extract_native_text(reader, index)
            except _PAGE_ERRORS as exc:
                _logger.warning("Cannot inspect PDF page %s: %s", index + 1, exc)
                continue
            embedded_text_pages += int(len(text) >= self.settings.pdf_min_text_chars)
        embedded_text_ratio = (
            embedded_text_pages / total_pages if total_pages else 0.0
        )
        return {
            "total_pages": total_pages,
            "embedded_text_pages": embedded_text_pages,
            "embedded_text_ratio": embedded_text_ratio,
            "recommended_mode": (
                "ocr" if self.settings.ocr_engine == "surya" or embedded_text_ratio < 0.8
                else "embedded"
            ),
        }

    async def extract_pages(
        self,
        pdf_path: Path,
        *,
        force_ocr: bool = False,
        use_layout: bool | None = None,
        retain_page_images: bool = False,
        progress: ProgressCallback | None = None,
        cached_pages: dict[int, dict[str, Any]] | None = None,
        page_completed: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        quick_ocr: bool = False,
    ) -> list[dict[str, Any]]:
        return await self._extract_pages(
            pdf_path,
            force_ocr=force_ocr or (self.settings.ocr_engine == "surya" and use_layout is not False),
            retain_page_images=retain_page_images,
            progress=progress,
            cached_pages=cached_pages,
            page_completed=page_completed,
            quick_ocr=quick_ocr,
        )

    async def _extract_pages(
        self,
        pdf_path: Path,
        *,
        force_ocr: bool,
        retain_page_images: bool,
        progress: ProgressCallback | None,
        cached_pages: dict[int, dict[str, Any]] | None,
        page_completed: Callable[[dict[str, Any]], Awaitable[None]] | None,
        quick_ocr: bool,
    ) -> list[dict[str, Any]]:
        reader, total_pages = await anyio.to_thread.run_sync(self._open_pdf, pdf_path)
        pages = [dict((cached_pages or {}).get(index, {})) for index in range(1, total_pages + 1)]
        pending = [index for index, page in enumerate(pages, 1) if not page]
        completed = total_pages - len(pending)
        reused = completed
        started_at = time.monotonic()
        is_surya = self.settings.ocr_engine == "surya" and not quick_ocr
        phase_label = (
            "Quick CPU OCR with Indra" if quick_ocr else
            "Extracting layout and Markdown with Surya" if is_surya else
            "Extracting PDF text with OCR fallback"
        )
        await report_progress(
            progress,
            {
                "phase": "ocr",
                "phase_label": phase_label,
                "completed_pages": completed,
                "total_pages": total_pages,
                "reused_pages": completed,
            },
        )
        ocr_available: bool | None = None
        prepare_lock = anyio.Lock()

        async def extract(index: int) -> None:
            nonlocal ocr_available, completed
            native_error = None
            try:
                async with prepare_lock:
                    embedded_text = await anyio.to_thread.run_sync(
                        self._extract_native_text, reader, index - 1,
                    )
            except _PAGE_ERRORS as exc:
                native_error = f"Native text extraction failed for page {index}: {exc}"
                _logger.warning("%s", native_error)
                embedded_text = ""
            text = embedded_text
            page_image = b""
            needs_ocr = quick_ocr or force_ocr or len(text) < self.settings.pdf_min_text_chars
            entry: dict[str, Any] = {
                "page": index,
                "raw_text": text,
                "text": text,
                "ocr_used": False,
                "llm_enhanced": False,
            }
            if native_error:
                entry["native_extraction_note"] = native_error
            if needs_ocr:
                try:
                    if ocr_available is None:
                        async with prepare_lock:
                            if ocr_available is None:
                                ocr_available = await anyio.to_thread.run_sync(
                                    self.indra.available if quick_ocr else self.available,
                                )
                    if not ocr_available:
                        if quick_ocr:
                            raise DocumentProcessingError("Indra CPU OCR models are unavailable locally.")
                        raise self._unavailable_error(index)
                    page_image = await anyio.to_thread.run_sync(
                        self.render_page_png, pdf_path, index - 1,
                        *((200,) if quick_ocr else ()),
                    )
                    structured = (
                        await self.indra.recognize(page_image, index) if quick_ocr else
                        await self.surya.recognize(page_image, index) if is_surya else None
                    )
                    ocr_text = structured["text"] if structured else await self.ocr_image(page_image, index)
                    if not ocr_text.strip() and not (
                        structured and all(block["excluded_from_text"] for block in structured["layout_blocks"])
                    ):
                        raise DocumentProcessingError(f"OCR produced no readable text for page {index}")
                except _PAGE_ERRORS as exc:
                    if is_surya and self.settings.ocr_indra_fallback_enabled:
                        try:
                            recovery_image = await anyio.to_thread.run_sync(
                                self.render_page_png, pdf_path, index - 1, 200,
                            )
                            recovery = await self.indra.recognize(recovery_image, index)
                            entry.update(recovery)
                            entry.update(
                                raw_text=recovery["text"], ocr_used=True,
                                ocr_recovery_note=f"Surya failed; Indra CPU recovery used. Details: {exc}",
                            )
                            _logger.warning("Page %s: %s", index, entry["ocr_recovery_note"])
                        except _PAGE_ERRORS as recovery_error:
                            exc = DocumentProcessingError(f"{exc}; Indra recovery failed: {recovery_error}")
                    if not entry["ocr_used"] and embedded_text:
                        entry["ocr_fallback_note"] = (
                            f"OCR failed for page {index}; the PDF's embedded text was "
                            f"used instead. Details: {exc}"
                        )
                    elif not entry["ocr_used"]:
                        entry["ocr_skipped"] = True
                        entry["ocr_fallback_note"] = (
                            f"Skipped unreadable page {index}; no native or OCR text "
                            f"could be recovered. Details: {exc}"
                        )
                    if entry.get("ocr_fallback_note"):
                        _logger.warning("%s", entry["ocr_fallback_note"])
                else:
                    if structured:
                        entry.update(structured)
                    entry["raw_text"] = ocr_text.strip()
                    entry["text"] = ocr_text.strip()
                    entry["ocr_used"] = True
                    entry["ocr_engine"] = "indra" if quick_ocr else self.settings.ocr_engine

            if retain_page_images and not entry.get("ocr_skipped"):
                await self._retain_page_image(pdf_path, entry, page_image)
            pages[index - 1] = entry
            if entry.get("ocr_used") and page_completed is not None:
                await page_completed(entry)
            completed += 1
            await self._report_page_progress(
                progress,
                started_at,
                completed,
                total_pages,
                phase_label,
                current_page=index,
                processed_pages=completed - reused,
            )

        if retain_page_images:
            for page in pages:
                if page and not page.get("llm_enhanced") and not page.get("ocr_quality"):
                    await self._retain_page_image(pdf_path, page)
        if is_surya:
            self._surya_limiter.total_tokens = self.settings.ocr_surya_parallel
            indices = iter(pending)

            async def worker() -> None:
                for index in indices:
                    async with self._surya_limiter:
                        await extract(index)

            async with anyio.create_task_group() as tasks:
                for _ in range(min(len(pending), self.settings.ocr_surya_parallel)):
                    tasks.start_soon(worker)
        else:
            for index in pending:
                await extract(index)
        return pages

    async def _retain_page_image(
        self, pdf_path: Path, page: dict[str, Any], page_image: bytes = b"",
    ) -> None:
        if not page_image:
            try:
                page_image = await anyio.to_thread.run_sync(
                    self.render_page_png, pdf_path, page["page"] - 1,
                )
            except _PAGE_ERRORS as exc:
                page["llm_enhancement_note"] = (
                    f"Could not render page {page['page']}; keeping extracted text "
                    f"without vision enhancement. Details: {exc}"
                )
                _logger.warning("%s", page["llm_enhancement_note"])
        if page_image:
            page["_image_png"] = page_image

    @staticmethod
    def _open_pdf(pdf_path: Path) -> tuple[PdfReader, int]:
        reader = PdfReader(str(pdf_path))
        # Counting pages lazily parses the page tree, so keep it off the event loop too.
        return reader, len(reader.pages)

    @staticmethod
    def _extract_native_text(reader: PdfReader, zero_based_page_index: int) -> str:
        text = reader.pages[zero_based_page_index].extract_text() or ""
        surrogate_count = sum(0xD800 <= ord(character) <= 0xDFFF for character in text)
        if surrogate_count:
            # Some PDF font maps expose UTF-16 code units instead of Unicode scalars.
            text = text.encode("utf-16-le", errors="surrogatepass").decode("utf-16-le", errors="replace")
            _logger.warning(
                "Repaired %s UTF-16 surrogate code units in PDF page %s; "
                "valid pairs were restored and unpaired surrogates replaced with U+FFFD.",
                surrogate_count, zero_based_page_index + 1,
            )
        return text.strip()

    async def ocr_page(self, pdf_path: Path, zero_based_page_index: int) -> str:
        if not await anyio.to_thread.run_sync(self.available):
            return ""
        image_png = await anyio.to_thread.run_sync(
            self.render_page_png,
            pdf_path,
            zero_based_page_index,
        )
        return await self.ocr_image(image_png, zero_based_page_index + 1)

    async def ocr_image(self, image_png: bytes, page_number: int) -> str:
        if self.settings.ocr_engine == "surya":
            async with self._surya_limiter:
                return (await self.surya.recognize(image_png, page_number))["text"]
        return await self._tesseract_image(image_png, page_number)

    async def _tesseract_image(self, image_png: bytes, page_number: int) -> str:
        tesseract_path = self._tesseract_path()
        if tesseract_path is None:
            raise self._unavailable_error(page_number)
        pytesseract.pytesseract.tesseract_cmd = tesseract_path
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

    def _unavailable_error(self, page_number: int) -> DocumentProcessingError:
        if self.settings.ocr_engine == "surya":
            return DocumentProcessingError(
                f"Page {page_number} requires OCR, but Surya model "
                f"{self.settings.ocr_surya_model!r} is unavailable at {self.settings.ocr_surya_base_url}"
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
        *,
        current_page: int | None = None,
        processed_pages: int | None = None,
    ) -> None:
        elapsed = time.monotonic() - started_at
        average = elapsed / (index if processed_pages is None else processed_pages)
        await report_progress(
            progress,
            {
                "phase": "ocr",
                "phase_label": phase_label,
                "completed_pages": index,
                "total_pages": total_pages,
                "current_page": current_page if current_page is not None else index,
                "elapsed_seconds": round(elapsed, 1),
                "average_seconds_per_page": round(average, 1),
                "eta_seconds": round(average * (total_pages - index), 1),
            },
        )

    @staticmethod
    def render_page_png(pdf_path: Path, zero_based_page_index: int, dpi: int = 144) -> bytes:
        # PDFium is not thread-safe, even across distinct document handles.
        with _PDFIUM_LOCK, closing(pypdfium2.PdfDocument(str(pdf_path))) as document:
            with closing(document[zero_based_page_index]) as page:
                width, height = page.get_size()
                scale = min(dpi / 72, 2048 / width, (6_000_000 / (width * height)) ** 0.5)
                with closing(page.render(scale=scale)) as bitmap:
                    with bitmap.to_pil() as image:
                        output = io.BytesIO()
                        image.save(output, format="PNG")
                        return output.getvalue()

    @staticmethod
    def _tesseract_config() -> str:
        candidates = [
            os.environ.get("TESSDATA_PREFIX"),
            "/usr/share/tesseract-ocr/5/tessdata",
            "/usr/share/tessdata",
            "/usr/local/share/tessdata",
            r"C:\Program Files\Tesseract-OCR\tessdata",
            r"C:\Program Files (x86)\Tesseract-OCR\tessdata",
        ]
        for candidate in candidates:
            if not candidate:
                continue
            tessdata_dir = Path(candidate.strip('"'))
            if tessdata_dir.is_dir():
                os.environ["TESSDATA_PREFIX"] = str(tessdata_dir)
                return ""
        return ""

    @staticmethod
    def _tesseract_path() -> str | None:
        candidates = [
            shutil.which("tesseract"),
            os.environ.get("TESSERACT_CMD"),
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        ]
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return str(Path(candidate))
        return None
