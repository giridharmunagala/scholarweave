from __future__ import annotations

from typing import Any


PAPER_MANIFEST_SCHEMA_VERSION = 1


def build_paper_manifest(
    *,
    document_id: str,
    title: str,
    source_filename: str,
    content_type: str,
    pages: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
    figures: list[dict[str, Any]],
) -> dict[str, Any]:
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
        "figures": figures,
        "content": {
            "char_count": total_chars,
            "nonempty_page_count": sum(
                bool(page["text"].strip()) for page in normalized_pages
            ),
            "chunk_count": len(normalized_chunks),
            "figure_count": len(figures),
        },
    }


def manifest_pages(manifest: Any) -> list[dict[str, Any]]:
    if not isinstance(manifest, dict) or not isinstance(manifest.get("pages"), list):
        raise ValueError("Paper manifest is invalid.")
    return [page for page in manifest["pages"] if isinstance(page, dict)]
