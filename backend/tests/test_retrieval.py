from __future__ import annotations

from backend.bootstrap import create_services
from backend.documents.models import Document
from sqlalchemy import event


def test_retrieval_services_support_keyword_vector_and_merge(test_settings) -> None:
    services = create_services(test_settings)
    with services.session_factory() as session:
        session.add(Document(id="doc-1", title="Doc", source_filename="doc.pdf", content_type="application/pdf", status="ready"))
        session.commit()

    chunks = services.retrieval.replace_document_chunks(
        "doc-1",
        [
            {"section_title": "Intro", "page_start": 1, "page_end": 1, "citation": "p.1", "text": "cats and dogs", "embedding": [1.0, 0.0]},
            {"section_title": "Method", "page_start": 2, "page_end": 2, "citation": "p.2", "text": "fish and whales", "embedding": [0.0, 1.0]},
        ],
    )

    vector = services.retrieval.vector_search([0.9, 0.1], document_id="doc-1", top_k=1)
    keyword = services.retrieval.keyword_search("whales", document_id="doc-1", top_k=1)
    merged = services.retrieval.merge_contexts(vector, keyword)
    full = services.retrieval.full_context("doc-1", max_chars=50)

    assert len(chunks) == 2
    assert vector[0]["citation"] == "p.1"
    assert keyword[0]["citation"] == "p.2"
    assert len(merged) == 2
    assert len(full) >= 1


def test_chunk_pagination_and_fts_are_database_bounded_and_replaced(test_settings) -> None:
    services = create_services(test_settings)
    engine = services.session_factory.kw["bind"]
    with services.session_factory() as session:
        session.add_all([
            Document(id=key, title=key, source_filename="d.pdf", content_type="application/pdf", status="ready")
            for key in ("doc-a", "doc-b")
        ])
        session.commit()
    services.retrieval.replace_document_chunks("doc-a", [
        {"text": f"evidence {index} needle" if index == 27 else f"evidence {index}"}
        for index in range(50)
    ])
    services.retrieval.replace_document_chunks("doc-b", [{"text": "needle other paper"}])
    statements: list[str] = []

    def track(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", track)
    try:
        rows = services.retrieval.fetch_document_chunks("doc-a", start=26, limit=2)
        assert [row.chunk_index for row in rows] == [26, 27]
        assert any("LIMIT" in statement and "OFFSET" in statement for statement in statements)
        hits = services.retrieval.keyword_search('needle OR " --', document_id="doc-a", top_k=1)
        assert len(hits) == 1
        assert hits[0]["chunk_index"] == 27
        assert any("document_chunks_fts MATCH" in statement for statement in statements)
        services.retrieval.replace_document_chunks("doc-a", [{"text": "replacement extraction"}])
        assert services.retrieval.keyword_search("needle", document_id="doc-a") == []
        assert services.retrieval.keyword_search("replacement", document_id="doc-a")
        services.documents.delete_document("doc-a")
        assert services.retrieval.keyword_search("replacement") == []
        assert services.retrieval.keyword_search('" : *', document_id="doc-b") == []
    finally:
        event.remove(engine, "before_cursor_execute", track)
        engine.dispose()


def test_full_context_never_exceeds_character_budget(test_settings) -> None:
    services = create_services(test_settings)
    with services.session_factory() as session:
        session.add(Document(id="bounded", title="Bounded", source_filename="b.pdf", content_type="application/pdf", status="ready"))
        session.commit()
    services.retrieval.replace_document_chunks("bounded", [{"text": "x " * 1000 + "hiddenneedle 42"}])
    result = services.retrieval.full_context("bounded", max_chars=37)
    assert len(result[0]["text"]) == 37
    assert result[0]["truncated"] is True
    hit = services.retrieval.keyword_search("hiddenneedle", document_id="bounded")[0]
    assert "hiddenneedle" in hit["text"]
    assert hit["match_offset"] == 2000
    assert len(hit["text"]) < 300
    services.session_factory.kw["bind"].dispose()
