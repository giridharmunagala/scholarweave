from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from backend.core.config import Settings
from backend.utils import cosine_similarity
from backend.documents.models import DocumentChunk


class RetrievalService:
    def __init__(self, session_factory: sessionmaker[Session], settings: Settings) -> None:
        self.session_factory = session_factory
        self.settings = settings

    def replace_document_chunks(self, document_id: str, chunks: list[dict[str, Any]]) -> list[DocumentChunk]:
        with self.session_factory() as session:
            session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document_id))
            stored: list[DocumentChunk] = []
            for index, chunk in enumerate(chunks):
                row = DocumentChunk(
                    document_id=document_id,
                    chunk_index=index,
                    section_title=chunk.get("section_title"),
                    page_start=chunk.get("page_start", 1),
                    page_end=chunk.get("page_end", chunk.get("page_start", 1)),
                    citation=chunk.get("citation") or f"p.{chunk.get('page_start', 1)}",
                    text=chunk["text"],
                    embedding_json=chunk.get("embedding"),
                    metadata_json=chunk.get("metadata", {}),
                )
                session.add(row)
                stored.append(row)
            session.commit()
            for row in stored:
                session.refresh(row)
            return stored

    def fetch_document_chunks(self, document_id: str) -> list[DocumentChunk]:
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(DocumentChunk)
                    .where(DocumentChunk.document_id == document_id)
                    .order_by(DocumentChunk.chunk_index.asc())
                )
            )

    def set_embeddings(self, chunk_ids: list[str], embeddings: list[list[float]]) -> None:
        with self.session_factory() as session:
            rows = list(session.scalars(select(DocumentChunk).where(DocumentChunk.id.in_(chunk_ids))))
            by_id = {row.id: row for row in rows}
            for chunk_id, embedding in zip(chunk_ids, embeddings, strict=True):
                by_id[chunk_id].embedding_json = embedding
            session.commit()

    def has_embeddings(self, document_id: str | None = None) -> bool:
        """Reports whether anything is actually searchable by vector yet.

        Chunks exist as soon as a PDF is ingested, but they carry no embedding until the
        indexing step runs — so vector search returns nothing and the agent looks broken.
        """
        with self.session_factory() as session:
            stmt = select(DocumentChunk)
            if document_id:
                stmt = stmt.where(DocumentChunk.document_id == document_id)
            return any(row.embedding_json for row in session.scalars(stmt))

    def vector_search(self, query_embedding: list[float], document_id: str | None = None, top_k: int = 5) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            stmt = select(DocumentChunk)
            if document_id:
                stmt = stmt.where(DocumentChunk.document_id == document_id)
            rows = list(session.scalars(stmt))
        scored: list[tuple[float, DocumentChunk]] = []
        for row in rows:
            embedding = row.embedding_json or []
            if embedding:
                scored.append((cosine_similarity(query_embedding, embedding), row))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [self._chunk_result(row, score=score) for score, row in scored[:top_k] if score > 0]

    def keyword_search(self, query: str, document_id: str | None = None, top_k: int = 5) -> list[dict[str, Any]]:
        tokens = [token.lower() for token in query.split() if token.strip()]
        with self.session_factory() as session:
            stmt = select(DocumentChunk)
            if document_id:
                stmt = stmt.where(DocumentChunk.document_id == document_id)
            rows = list(session.scalars(stmt))
        scored: list[tuple[int, DocumentChunk]] = []
        for row in rows:
            text = row.text.lower()
            counts = Counter(text.count(token) for token in tokens)
            score = sum(token in text for token in tokens) * 10 + sum(k * v for k, v in counts.items())
            if score:
                scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [self._chunk_result(row, score=score) for score, row in scored[:top_k]]

    def full_context(self, document_id: str, max_chars: int | None = None) -> list[dict[str, Any]]:
        limit = max_chars or self.settings.retrieval_max_context_chars
        rows = self.fetch_document_chunks(document_id)
        total = 0
        results: list[dict[str, Any]] = []
        for row in rows:
            if total >= limit:
                break
            results.append(self._chunk_result(row))
            total += len(row.text)
        return results

    def merge_contexts(self, *contexts: list[dict[str, Any]], max_items: int = 12) -> list[dict[str, Any]]:
        seen: set[str] = set()
        merged: list[dict[str, Any]] = []
        for context in contexts:
            for item in context:
                key = item.get("chunk_id") or f"{item.get('citation')}::{item.get('text')}"
                if key in seen:
                    continue
                seen.add(key)
                merged.append(item)
                if len(merged) >= max_items:
                    return merged
        return merged

    @staticmethod
    def render_context(results: list[dict[str, Any]]) -> str:
        return "\n\n".join(f"[{item['citation']}] {item['text']}" for item in results)

    @staticmethod
    def _chunk_result(row: DocumentChunk, score: float | int | None = None) -> dict[str, Any]:
        payload = {
            "chunk_id": row.id,
            "document_id": row.document_id,
            "chunk_index": row.chunk_index,
            "section_title": row.section_title,
            "page_start": row.page_start,
            "page_end": row.page_end,
            "citation": row.citation,
            "text": row.text,
            "metadata": row.metadata_json or {},
        }
        if score is not None:
            payload["score"] = score
        return payload
