from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.bootstrap import create_services
from backend.core.errors import ConflictError, ValidationError as DomainValidationError
from backend.research.schemas import ArxivPaperInput, PdfPaperInput
from backend.research.reviews.schemas import Candidate, EmbeddingRole, ReviewControlRequest, ReviewCreateRequest, SummaryRole
from backend.tests.test_literature_reviews import command, configure, seed_paper, settle
from backend.tests.test_targeted_reviews import brief_reply
from backend.tests.test_topic_research import embedding_reference


def test_arxiv_search_api_is_explicit_paginated_and_reports_source_errors(test_settings, monkeypatch):
    from backend.app import create_app

    app = create_app(test_settings)
    calls = []
    authors = [f"Researcher {index}" for index in range(1001)]

    async def search(query, *, start, limit):
        calls.append((query, start, limit))
        if query == "failure":
            raise httpx.ConnectError("arXiv unavailable")
        return {"query": query, "provider": "arxiv", "results": [{
            "arxiv_id": "2610.01234", "title": "Calibration", "summary": "Abstract only",
            "authors": authors, "url": "https://arxiv.org/abs/2610.01234",
            "pdf_url": "https://arxiv.org/pdf/2610.01234",
            "published_at": "2026-10-01", "updated_at": "2026-10-01", "categories": ["cs.LG"],
        }], "next_start": 20}

    with TestClient(app) as client:
        monkeypatch.setattr(app.state.services.research_search, "search_arxiv_page", search)
        result = client.get("/api/research/arxiv", params={"query": "calibration", "start": 10})
        assert result.status_code == 200, result.text
        assert result.json()["results"][0]["title"] == "Calibration"
        returned = result.json()["results"][0]
        assert returned["authors"] == authors
        assert ArxivPaperInput(**{
            key: returned[key] for key in ("arxiv_id", "title", "summary", "authors")
        }).authors == authors
        assert result.json()["next_start"] == 20
        assert calls == [("calibration", 10, 10)]
        assert client.get("/api/documents").json() == []
        assert client.get("/api/research/arxiv", params={"query": " ", "start": 0}).status_code == 422
        assert client.get("/api/research/arxiv", params={"query": "calibration", "start": -1}).status_code == 422
        failure = client.get("/api/research/arxiv", params={"query": "failure"})
        assert failure.status_code == 502
        assert "arXiv unavailable" in failure.json()["detail"]


@pytest.mark.anyio
async def test_explicit_inputs_join_landscape_and_pdf_collection_waits_for_approval(test_settings, stub_provider, monkeypatch):
    services = create_services(test_settings)
    monkeypatch.setattr(stub_provider, "_reply_for", brief_reply)
    downloads = []

    async def download(url, **options):
        downloads.append((url, options))
        identifier = await seed_paper(services, title="Direct PDF input")
        return services.documents.get_document(identifier)

    monkeypatch.setattr(services.reviews.downloads, "download_pdf", download)
    try:
        main = configure(services, stub_provider)
        spec = ReviewCreateRequest(
            title="Calibration", objective="Compare calibration.", main_model=main,
            summary_model=SummaryRole(model_reference=main),
            embedding_model=EmbeddingRole(model_reference=embedding_reference(services, stub_provider)),
            reading_mode="targeted", web_enabled=False,
            input_papers=[ArxivPaperInput(arxiv_id="2610.01234", title="Explicit calibration input",
                                         summary="A calibration method abstract.", authors=["Researcher"])],
        )
        review = await services.reviews.create(spec)
        await settle(services, review.id)
        pdf_input = {"url": "https://example.org/download?id=calibration", "title": "Direct PDF input"}
        with pytest.raises(ConflictError):
            await command(services, review.id, "add_pdf_paper", pdf_paper=pdf_input)
        assert services.reviews._candidates(review.id) == []
        await command(services, review.id, "approve_plan")
        landscape = await settle(services, review.id)
        assert landscape.approval == "topics", landscape.error
        candidates = services.reviews._candidates(review.id)
        assert len(candidates) == 1
        assert candidates[0].title == "Explicit calibration input"
        assert candidates[0].abstract == "A calibration method abstract."
        assert candidates[0].pdf_url == "https://arxiv.org/pdf/2610.01234"
        assert candidates[0].document_id is None
        assert landscape.topics[0].candidate_ids == [candidates[0].id]
        assert services.documents.list_documents() == []
        original_topic_id = landscape.topics[0].id
        await command(services, review.id, "add_arxiv_papers", input_papers=[{
            "arxiv_id": "2610.05678", "title": "Follow-up calibration paper",
            "summary": "A different calibration approach.", "authors": [],
        }])
        expanded = await settle(services, review.id)
        assert expanded.approval == "topics", expanded.error
        assert expanded.topics[0].id == original_topic_id
        assert expanded.topics[0].candidate_ids == [candidates[0].id]
        assert len(services.reviews._candidates(review.id)) == 2
        assert services.documents.list_documents() == []
        await command(services, review.id, "add_arxiv_papers", input_papers=[{
            "arxiv_id": "2610.05678", "title": "Follow-up calibration paper",
        }])
        assert (await settle(services, review.id)).approval == "topics"
        assert len(services.reviews._candidates(review.id)) == 2
        await command(services, review.id, "add_pdf_paper", pdf_paper=pdf_input)
        expanded = await settle(services, review.id)
        assert expanded.approval == "topics", expanded.error
        assert expanded.topics[0].id == original_topic_id
        assert expanded.topics[0].candidate_ids == [candidates[0].id]
        added = next(row for row in services.reviews._candidates(review.id) if row.pdf_url == pdf_input["url"])
        assert added.title == pdf_input["title"]
        assert added.document_id is None and not added.selected
        assert downloads == []
        assert services.documents.list_documents() == []
        with pytest.raises(ConflictError):
            await services.reviews.control(review.id, ReviewControlRequest(
                action="add_pdf_paper", expected_revision=expanded.revision - 1, pdf_paper=pdf_input,
            ))
        await command(services, review.id, "add_pdf_paper", pdf_paper={"url": pdf_input["url"] + "#page=2"})
        expanded = await settle(services, review.id)
        assert expanded.approval == "topics", expanded.error
        assert len(services.reviews._candidates(review.id)) == 3
        assert services.reviews.repository.candidate(review.id, added.id).title == pdf_input["title"]
        assert downloads == []
        with pytest.raises(ConflictError, match="Unapproved papers"):
            await services.reviews._prepare_paper(review.id, added)
        topic = next(topic for topic in expanded.topics if added.id in topic.candidate_ids)
        await command(services, review.id, "approve_topics", topic_ids=[topic.id],
                      candidate_ids=[added.id], reading_mode="targeted", feedback="Compare calibration methods")
        completed = await settle(services, review.id)
        assert completed.status == "completed", completed.error
        assert downloads == [(pdf_input["url"], {
            "title": pdf_input["title"], "enhance_with_llm": False, "match_title": False,
            "owner_review_id": review.id,
        })]
        assert services.reviews.repository.candidate(review.id, added.id).document_id is not None
        assert completed.work_used.read_papers == 1
    finally:
        await services.close()


def test_arxiv_input_does_not_accept_arbitrary_download_urls():
    with pytest.raises(ValidationError):
        ArxivPaperInput(arxiv_id="../../private", title="Invalid")
    with pytest.raises(ValidationError):
        ArxivPaperInput(arxiv_id="2610.01234", title="Invalid", pdf_url="http://localhost/private")


@pytest.mark.anyio
async def test_pdf_link_reuses_known_download_url_without_overwriting_identity_or_metadata(test_settings):
    services = create_services(test_settings)
    try:
        repository = services.reviews.repository
        repository.create("pdf-identity", {}, {})
        original = Candidate(
            id="original", title="Known paper", authors=["Researcher"], abstract="Known abstract",
            doi="10.1234/example", url="https://example.org/article",
            pdf_url="https://example.org/paper.pdf", selected=True,
        )
        repository.save_candidate("pdf-identity", "doi:10.1234/example", original)
        paper = PdfPaperInput(url="https://example.org/paper.pdf#page=3")
        services.reviews._add_pdf_input("pdf-identity", paper)
        candidates = services.reviews._candidates("pdf-identity")
        assert len(candidates) == 1
        assert candidates[0].model_dump(exclude={"discovery"}) == original.model_dump(exclude={"discovery"})
        assert candidates[0].discovery
        ambiguous = original.model_copy(update={
            "id": "other", "doi": "10.1234/other", "url": "https://example.org/other", "selected": False,
        })
        repository.save_candidate("pdf-identity", "doi:10.1234/other", ambiguous)
        with pytest.raises(DomainValidationError, match="Multiple shortlist papers"):
            services.reviews._add_pdf_input("pdf-identity", paper)
        assert len(services.reviews._candidates("pdf-identity")) == 2
    finally:
        await services.close()


@pytest.mark.parametrize("url", [
    "file:///private/paper.pdf", "javascript:alert(1)", "not a URL",
    "https://user:secret@example.org/paper.pdf", "https://example.org:bad/paper.pdf",
])
def test_pdf_input_rejects_invalid_or_credentialed_urls(url):
    with pytest.raises(ValidationError):
        PdfPaperInput(url=url)


def test_pdf_input_is_explicit_and_cannot_be_attached_to_reading_approval():
    with pytest.raises(ValidationError):
        ReviewControlRequest(action="add_pdf_paper", expected_revision=1)
    with pytest.raises(ValidationError):
        ReviewControlRequest(action="approve_topics", expected_revision=1,
                             pdf_paper={"url": "https://example.org/paper.pdf"})
    with pytest.raises(ValidationError):
        PdfPaperInput(url="https://example.org/paper.pdf", title="   ")
