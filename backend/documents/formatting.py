from __future__ import annotations

import re
from typing import Any

from backend.core.config import Settings

PAPER_MANIFEST_SCHEMA_VERSION = 1


class DocumentFormatter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def chunk_pages(
        self,
        pages: list[dict[str, Any]],
        *,
        title: str,
    ) -> list[dict[str, Any]]:
        """Group extracted page paragraphs into citation-bearing retrieval chunks."""
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
        """Join page text into the canonical extracted Markdown document."""
        return "\n\n".join(str(page.get("text") or "") for page in pages)


def build_paper_manifest(
    *,
    document_id: str,
    title: str,
    source_filename: str,
    content_type: str,
    pages: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the normalized, citation-rich manifest retained for an ingested paper."""
    normalized_pages = [
        {
            **{key: value for key, value in page.items() if not key.startswith("_")},
            "page": int(page["page"]),
            "citation": f"p.{int(page['page'])}",
            "text": str(page.get("text") or ""),
            "char_count": len(str(page.get("text") or "")),
        }
        for page in pages
    ]
    normalized_chunks = [
        {
            **chunk,
            "chunk_index": index,
            "citation": str(chunk.get("citation") or ""),
        }
        for index, chunk in enumerate(chunks)
    ]
    sections = [
        {
            "section_index": index,
            "title": str(chunk.get("section_title") or title),
            "citation": str(chunk.get("citation") or ""),
            "page_start": int(chunk.get("page_start") or 1),
            "page_end": int(chunk.get("page_end") or chunk.get("page_start") or 1),
            "chunk_index": index,
        }
        for index, chunk in enumerate(chunks)
    ]
    total_chars = sum(page["char_count"] for page in normalized_pages)
    return {
        "schema": "scholarweave.paper",
        "schema_version": PAPER_MANIFEST_SCHEMA_VERSION,
        "paper": {
            "document_id": document_id,
            "title": title,
            "source_filename": source_filename,
            "content_type": content_type,
            "page_count": len(normalized_pages),
        },
        # Retain these top-level fields for existing direct-agent readers.
        "document_id": document_id,
        "title": title,
        "pages": normalized_pages,
        "sections": sections,
        "chunks": normalized_chunks,
        "content": {
            "char_count": total_chars,
            "nonempty_page_count": sum(
                bool(page["text"].strip()) for page in normalized_pages
            ),
            "chunk_count": len(normalized_chunks),
        },
    }


def manifest_pages(manifest: Any) -> list[dict[str, Any]]:
    """Return valid page objects from a paper manifest or reject a malformed manifest."""
    if not isinstance(manifest, dict) or not isinstance(manifest.get("pages"), list):
        raise ValueError("Paper manifest is invalid.")
    return [page for page in manifest["pages"] if isinstance(page, dict)]
