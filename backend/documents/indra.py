from __future__ import annotations

import html
import importlib.util
import io
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
from PIL import Image

from backend.core.errors import DocumentProcessingError
from backend.documents.surya import assemble_page

if TYPE_CHECKING:
    from indra_ocr import Converter, Page

INDRA_QUALITY_NOTE = (
    "Fast CPU extraction with Indra. Check equations, table values and reading order "
    "against the PDF; this is not a verified transcription."
)
_SETUP_NOTE = (
    "Indra CPU models are not installed locally. Run "
    "'hf download datalab-to/indra-ocr --revision v1.1.0' "
    "or set INDRA_OCR_MODEL_DIR to a downloaded model folder."
)


class IndraOCR:
    def __init__(self) -> None:
        self._converter: Converter | None = None
        self._limiter = anyio.CapacityLimiter(1)

    @staticmethod
    def model_directory() -> Path | None:
        if importlib.util.find_spec("indra_ocr") is None:
            return None
        from huggingface_hub import try_to_load_from_cache
        from indra_ocr.models import FILES, HF_REPO, HF_REVISION

        configured = os.environ.get("INDRA_OCR_MODEL_DIR")
        if configured:
            directory = Path(configured).expanduser()
        else:
            cached = try_to_load_from_cache(HF_REPO, "meta.json", revision=HF_REVISION)
            if not isinstance(cached, str):
                return None
            directory = Path(cached).parent
        return directory if all((directory / name).is_file() for name in FILES) else None

    def available(self) -> bool:
        return self.model_directory() is not None

    async def recognize(self, image_png: bytes, page: int) -> dict[str, Any]:
        async with self._limiter:
            return await anyio.to_thread.run_sync(self._recognize, image_png, page)

    def _recognize(self, image_png: bytes, page: int) -> dict[str, Any]:
        directory = self.model_directory()
        if directory is None:
            raise DocumentProcessingError(_SETUP_NOTE)
        import indra_ocr
        import numpy as np
        from onnxruntime.capi.onnxruntime_pybind11_state import (
            Fail, InvalidArgument, InvalidGraph, NoSuchFile, RuntimeException,
        )

        try:
            if self._converter is None:
                self._converter = indra_ocr.load(
                    path=directory, threads=min(4, os.cpu_count() or 1), mode="ocr",
                    providers=["CPUExecutionProvider"], math=False,
                )
            with Image.open(io.BytesIO(image_png)) as image:
                with image.convert("L") as gray:
                    result = self._converter.ocr(np.array(gray))
            if result.source == "error" or not result.text.strip():
                raise DocumentProcessingError(
                    f"Indra produced no readable text for page {page}. {result.note}"
                )
            return self.format_page(result, image_png, page)
        except (OSError, ValueError, Fail, InvalidArgument, InvalidGraph, NoSuchFile, RuntimeException) as exc:
            raise DocumentProcessingError(f"Indra CPU OCR failed for page {page}: {exc}") from exc

    @staticmethod
    def format_page(result: Page, image_png: bytes, page: int) -> dict[str, Any]:
        with Image.open(io.BytesIO(image_png)) as original:
            with original.rotate(result.rotation, expand=True) as image:
                output = io.BytesIO()
                image.save(output, format="PNG")
                blocks = []
                pictures = []
                for block in result.blocks:
                    label = block["label"]
                    if not block["text"].strip() and label not in {"Picture", "Page-Header", "Page-Footer"}:
                        continue
                    x0, y0, x1, y1 = block["bbox"]
                    bbox = [
                        max(0, min(1000, x0 / image.width * 1000)),
                        max(0, min(1000, y0 / image.height * 1000)),
                        max(0, min(1000, x1 / image.width * 1000)),
                        max(0, min(1000, y1 / image.height * 1000)),
                    ]
                    if label == "Picture":
                        pictures.append(len(blocks))
                    blocks.append({
                        "label": "Figure" if label == "Picture" else label,
                        "bbox": bbox,
                        "html": html.escape(block["text"]).replace("\n", "<br>"),
                    })
                if blocks:
                    structured = assemble_page(blocks, output.getvalue(), page)
                else:
                    structured = {"text": result.text, "content_format": "markdown"}
        text = result.markdown or structured["text"]
        for index, block_index in enumerate(pictures):
            text = text.replace(f"![](picture-{index})", blocks[block_index]["markdown"])
        return {
            **structured, "text": text, "ocr_engine": "indra",
            "ocr_quality_note": INDRA_QUALITY_NOTE,
            "ocr_rotation": result.rotation,
            "ocr_skew": result.skew,
            "ocr_line_confidences": [line.conf for line in result.lines],
        }
