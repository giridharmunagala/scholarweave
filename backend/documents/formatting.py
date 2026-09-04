from __future__ import annotations

import re
from typing import Any

from backend.core.config import Settings


class DocumentFormatter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def chunk_pages(
        self,
        pages: list[dict[str, Any]],
        *,
        title: str,
    ) -> list[dict[str, Any]]:
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
            citation = (
                f"p.{page_start}"
                if page_start == page_end
                else f"pp.{page_start}-{page_end}"
            )
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
            paragraphs = [
                part.strip()
                for part in re.split(r"\n\s*\n", page["text"])
                if part.strip()
            ]
            if not paragraphs and page["text"].strip():
                paragraphs = [page["text"].strip()]
            for paragraph in paragraphs:
                if page.get("llm_enhanced"):
                    normalized = paragraph.strip()
                else:
                    normalized = " ".join(
                        line.strip() for line in paragraph.splitlines() if line.strip()
                    )
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
        title_pattern = bool(
            re.match(r"^(?:\d+(?:\.\d+)*)?\s*[A-Z][A-Za-z0-9 ,:_-]{2,}$", stripped)
        )
        return (title_pattern and stripped == stripped.title()) or stripped.isupper()

    @staticmethod
    def build_markdown(pages: list[dict[str, Any]]) -> str:
        return "\n\n".join(str(page.get("text") or "") for page in pages)
