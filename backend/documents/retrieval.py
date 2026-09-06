from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from backend.core.config import Settings
from backend.utils import cosine_similarity
from backend.documents.models import Document, DocumentChunk


class RetrievalService:
    def __init__(self, session_factory: sessionmaker[Session], settings: Settings) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self._initialize_search()

    def _initialize_search(self) -> None:
        # Use the same SQLite FTS5 engine as workspace search, with transactional triggers
        # so ingestion, re-extraction and deletion cannot leave stale search hits.
        with self.session_factory.kw["bind"].begin() as connection:
            connection.exec_driver_sql(
                "CREATE VIRTUAL TABLE IF NOT EXISTS document_chunks_fts USING fts5("
                "chunk_id UNINDEXED, document_id UNINDEXED, content, tokenize='unicode61')"
            )
            for operation, body in (
                ("insert", "INSERT INTO document_chunks_fts VALUES (new.id, new.document_id, new.text);"),
                ("delete", "DELETE FROM document_chunks_fts WHERE chunk_id = old.id;"),
                ("update", "DELETE FROM document_chunks_fts WHERE chunk_id = old.id; "
                 "INSERT INTO document_chunks_fts VALUES (new.id, new.document_id, new.text);"),
            ):
                connection.exec_driver_sql(
                    f"CREATE TRIGGER IF NOT EXISTS document_chunks_search_{operation} "
                    f"AFTER {operation.upper()} ON document_chunks BEGIN {body} END"
                )
            count = connection.exec_driver_sql("SELECT count(*) FROM document_chunks").scalar_one()
            indexed = connection.exec_driver_sql("SELECT count(*) FROM document_chunks_fts").scalar_one()
            if count != indexed:
                connection.exec_driver_sql("DELETE FROM document_chunks_fts")
                connection.exec_driver_sql(
                    "INSERT INTO document_chunks_fts SELECT id, document_id, text FROM document_chunks"
                )

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
            document = session.get(Document, document_id)
            if document is not None:
                document.metadata_json = {
                    **(document.metadata_json or {}),
                    "extraction_chunk_hash": hashlib.sha256(
                        json.dumps(chunks, sort_keys=True, ensure_ascii=False).encode()
                    ).hexdigest(),
                }
            session.commit()
            for row in stored:
                session.refresh(row)
            return stored

    def fetch_document_chunks(
        self, document_id: str, *, start: int = 0, limit: int | None = None,
    ) -> list[DocumentChunk]:
        with self.session_factory() as session:
            statement = (
                select(DocumentChunk)
                .where(DocumentChunk.document_id == document_id)
                .order_by(DocumentChunk.chunk_index.asc())
                .offset(max(0, start))
            )
            if limit is not None:
                statement = statement.limit(max(0, limit))
            return list(
                session.scalars(statement)
            )

    def chunk_stats(self, document_id: str) -> tuple[int, int]:
        with self.session_factory() as session:
            count, characters = session.execute(
                select(func.count(), func.coalesce(func.sum(func.length(DocumentChunk.text)), 0))
                .where(DocumentChunk.document_id == document_id)
            ).one()
            return int(count), int(characters)

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
        tokens = re.findall(r"\w+", query, flags=re.UNICODE)[:32]
        if not tokens:
            return []
        match = " OR ".join(f'"{token}"' for token in tokens)
        with self.session_factory() as session:
            hits = session.execute(
                text(
                    "SELECT chunk_id, bm25(document_chunks_fts) AS rank, "
                    "snippet(document_chunks_fts, 2, '', '', ' … ', 48) AS excerpt "
                    "FROM document_chunks_fts WHERE document_chunks_fts MATCH :query "
                    + ("AND document_id = :document_id " if document_id else "")
                    + "ORDER BY rank, chunk_id LIMIT :limit"
                ),
                {"query": match, "document_id": document_id, "limit": max(0, top_k)},
            ).all()
            rows = {
                row.id: row for row in session.scalars(
                    select(DocumentChunk).where(DocumentChunk.id.in_([hit[0] for hit in hits]))
                )
            }
            return [
                {
                    **self._chunk_result(rows[chunk_id], score=-rank),
                    "text": excerpt,
                    "excerpt": True,
                    "match_offset": min(
                        (offset for token in tokens if (
                            offset := rows[chunk_id].text.lower().find(token.lower())
                        ) >= 0), default=0,
                    ),
                }
                for chunk_id, rank, excerpt in hits if chunk_id in rows
            ]

    def full_context(self, document_id: str, max_chars: int | None = None) -> list[dict[str, Any]]:
        limit = max_chars or self.settings.retrieval_max_context_chars
        total = 0
        results: list[dict[str, Any]] = []
        start = 0
        while total < limit:
            rows = self.fetch_document_chunks(document_id, start=start, limit=16)
            if not rows:
                break
            for row in rows:
                result = self._chunk_result(row)
                result["text"] = row.text[:limit - total]
                result["truncated"] = len(result["text"]) < len(row.text)
                results.append(result)
                total += len(result["text"])
                if total >= limit:
                    break
            start += len(rows)
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
