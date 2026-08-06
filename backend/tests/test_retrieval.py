from __future__ import annotations

from backend.bootstrap import create_services
from backend.documents.models import Document


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
