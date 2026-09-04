from __future__ import annotations

import io
import os
import shutil
import time
from functools import partial
from pathlib import Path
from typing import Any

import anyio
import pypdfium2
import pytesseract
from PIL import Image
from pypdf import PdfReader

from backend.core.config import Settings
from backend.core.errors import DocumentProcessingError
from backend.utils import ProgressCallback, report_progress

class DocumentOCR:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def available(self) -> bool:
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
        phase_label = "Extracting PDF text with OCR fallback"
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

    async def ocr_page(self, pdf_path: Path, zero_based_page_index: int) -> str:
        if not self.available():
            return ""
        image_png = await anyio.to_thread.run_sync(
            self.render_page_png,
            pdf_path,
            zero_based_page_index,
        )
        return await self.ocr_image(image_png, zero_based_page_index + 1)

    async def ocr_image(self, image_png: bytes, page_number: int) -> str:
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
