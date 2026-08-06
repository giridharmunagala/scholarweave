from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from functools import partial
from pathlib import Path
from typing import Any, Protocol

import anyio
from pypdf import PdfReader

from backend.core.config import ROOT_DIR, Settings
from backend.documents.errors import (
    DocumentProcessingError,
    ProgressCallback,
    report_progress,
)
from backend.providers.ollama import OllamaClient, OllamaError


class OllamaMemoryManager(Protocol):
    async def unload_all_models(self) -> list[str]: ...


class DocumentOCR:
    def __init__(
        self,
        settings: Settings,
        ollama: OllamaMemoryManager | None = None,
        ollama_gpu_lock: anyio.Lock | None = None,
    ) -> None:
        self.settings = settings
        self.ollama = ollama or OllamaClient(settings)
        self._ollama_gpu_lock = ollama_gpu_lock or anyio.Lock()
        self._surya_lock = anyio.Lock()

    def available(self) -> bool:
        if importlib.util.find_spec("pypdfium2") is None:
            return False
        if self.settings.ocr_engine == "surya":
            return all(
                importlib.util.find_spec(package) is not None
                for package in ("torch", "transformers", "markdownify")
            )
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
        if self.settings.ocr_engine == "surya":
            with tempfile.TemporaryDirectory(
                prefix="scholarweave-surya-pages-"
            ) as temp:
                return await self._extract_pages(
                    pdf_path,
                    force_ocr=force_ocr,
                    retain_page_images=retain_page_images,
                    progress=progress,
                    surya_spool=Path(temp),
                )
        return await self._extract_pages(
            pdf_path,
            force_ocr=force_ocr,
            retain_page_images=retain_page_images,
            progress=progress,
            surya_spool=None,
        )

    async def _extract_pages(
        self,
        pdf_path: Path,
        *,
        force_ocr: bool,
        retain_page_images: bool,
        progress: ProgressCallback | None,
        surya_spool: Path | None,
    ) -> list[dict[str, Any]]:
        reader = PdfReader(str(pdf_path))
        pages: list[dict[str, Any]] = []
        pending_surya: list[tuple[dict[str, Any], Path, str]] = []
        total_pages = len(reader.pages)
        started_at = time.monotonic()
        phase_label = (
            "Preparing pages for Surya OCR"
            if self.settings.ocr_engine == "surya"
            else "Rendering and OCR"
        )
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
            if needs_ocr and self.settings.ocr_engine == "surya":
                assert surya_spool is not None
                image_path = surya_spool / f"page-{index}.png"
                await anyio.to_thread.run_sync(image_path.write_bytes, page_image)
                pending_surya.append((entry, image_path, embedded_text))
            elif needs_ocr:
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

        if pending_surya:
            await report_progress(
                progress,
                {
                    "phase": "ocr",
                    "phase_label": f"Running Surya OCR on {len(pending_surya)} page(s)",
                    "completed_pages": 0,
                    "total_pages": len(pending_surya),
                },
            )
            try:
                results = await self._surya_image_paths(
                    [image_path for _, image_path, _ in pending_surya]
                )
            except DocumentProcessingError as exc:
                if any(not embedded for _, _, embedded in pending_surya):
                    raise
                for entry, _, _ in pending_surya:
                    entry["ocr_fallback_note"] = (
                        "Surya OCR failed; the PDF's embedded text was used instead. "
                        f"Details: {exc}"
                    )
            else:
                for position, ((entry, _, embedded), ocr_text) in enumerate(
                    zip(pending_surya, results, strict=True),
                    start=1,
                ):
                    if ocr_text.strip():
                        entry["raw_text"] = ocr_text.strip()
                        entry["text"] = ocr_text.strip()
                        entry["ocr_used"] = True
                        entry["ocr_engine"] = "surya"
                    elif embedded:
                        entry["ocr_fallback_note"] = (
                            "Surya returned no text; the PDF's embedded text was used instead."
                        )
                    else:
                        raise DocumentProcessingError(
                            f"Surya returned no text for page {entry['page']}"
                        )
                    await report_progress(
                        progress,
                        {
                            "phase": "ocr",
                            "phase_label": "Running Surya OCR",
                            "completed_pages": position,
                            "total_pages": len(pending_surya),
                            "current_page": entry["page"],
                        },
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
        if self.settings.ocr_engine == "surya":
            return (await self._surya_images([image_png]))[0]
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

    async def _surya_images(self, images: list[bytes]) -> list[str]:
        with tempfile.TemporaryDirectory(
            prefix="scholarweave-surya-images-"
        ) as temp:
            temp_dir = Path(temp)
            image_paths = []
            for index, image in enumerate(images, start=1):
                image_path = temp_dir / f"page-{index}.png"
                await anyio.to_thread.run_sync(image_path.write_bytes, image)
                image_paths.append(image_path)
            return await self._surya_image_paths(image_paths)

    async def _surya_image_paths(self, image_paths: list[Path]) -> list[str]:
        async with self._surya_lock:
            async with self._ollama_gpu_lock:
                if self.settings.surya_unload_ollama_models:
                    try:
                        await self.ollama.unload_all_models()
                    except OllamaError as exc:
                        raise DocumentProcessingError(
                            "Could not verify that Ollama released its loaded models "
                            f"before starting Surya: {exc}"
                        ) from exc

                with tempfile.TemporaryDirectory(prefix="scholarweave-surya-") as temp:
                    temp_dir = Path(temp)
                    manifest_path = temp_dir / "manifest.json"
                    output_path = temp_dir / "output.json"
                    manifest_path.write_text(
                        json.dumps(
                            {
                                "model": self.settings.surya_model,
                                "cache_dir": str(self.settings.surya_cache_dir),
                                "device": self.settings.surya_device,
                                "max_new_tokens": self.settings.surya_max_new_tokens,
                                "max_image_width": self.settings.surya_max_image_width,
                                "images": [str(path) for path in image_paths],
                            }
                        ),
                        encoding="utf-8",
                    )
                    command = [
                        sys.executable,
                        "-m",
                        "backend.documents.surya_worker",
                        "--manifest",
                        str(manifest_path),
                        "--output",
                        str(output_path),
                    ]
                    try:
                        with anyio.fail_after(self.settings.surya_timeout_seconds):
                            result = await anyio.run_process(
                                command,
                                check=False,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                cwd=ROOT_DIR,
                            )
                    except TimeoutError as exc:
                        raise DocumentProcessingError(
                            "Surya OCR exceeded its configured timeout; its isolated "
                            "worker was terminated to release GPU memory."
                        ) from exc
                    if result.returncode != 0:
                        details = result.stderr.decode("utf-8", errors="replace").strip()
                        raise DocumentProcessingError(
                            f"Surya OCR worker failed: {details[-2000:] or 'unknown error'}"
                        )
                    if not output_path.is_file():
                        raise DocumentProcessingError(
                            "Surya OCR worker exited without producing a result."
                        )
                    payload = json.loads(output_path.read_text(encoding="utf-8"))
                    pages = payload.get("pages")
                    if (
                        not isinstance(pages, list)
                        or len(pages) != len(image_paths)
                        or not all(isinstance(page, str) for page in pages)
                    ):
                        raise DocumentProcessingError(
                            "Surya OCR worker returned an invalid result."
                        )
                    return pages

    def _unavailable_error(self, page_number: int) -> DocumentProcessingError:
        if self.settings.ocr_engine == "surya":
            return DocumentProcessingError(
                f"Page {page_number} requires OCR, but the Surya runtime "
                "dependencies are unavailable."
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
