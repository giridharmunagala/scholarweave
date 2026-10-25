from __future__ import annotations

import json

import pytest

from backend.bootstrap import create_services
from backend.core.errors import ValidationError
from backend.research.reviews.schemas import EmbeddingRole, ReviewCreateRequest, SummaryRole
from backend.research.reviews.topics import TopicResearch
from backend.tests.test_literature_reviews import command, configure, seed_paper, settle
from backend.tests.test_topic_research import embedding_reference, topic_reply


def brief_reply(payload):
    schema = (payload.get("response_format") or {}).get("json_schema") or (payload.get("text") or {}).get("format", {})
    if schema.get("name") != "BriefPacket":
        return topic_reply(payload)
    text = next(row["content"] for row in reversed(payload["messages"]) if row["role"] == "user")
    if isinstance(text, list):
        text = "".join(row.get("text", "") for row in text)
    packet = json.loads(text)
    span = packet["records"][0]
    return json.dumps({
        "claims": [{"finding": "The supplied passage describes the method.", "category": "method",
                    "quote": span["text"][:40], "page": span["page"]}],
        "sections": [
            {"title": title, "content": f"{title}: targeted information about {packet.get('question_focus') or packet['objective']}.",
             "claim_indexes": [0]}
            for title in ["Fit to objective", "Approach", "Pertinent findings",
                          "Limitations and unknowns", "Why and when to read"]
        ],
        "limitations": ["The rest of the paper has not been reviewed."],
    })


def test_targeted_spans_are_bounded_question_relevant_and_verbatim():
    pages = [{"page": 1, "text": "Introduction. " * 1000},
             {"page": 2, "text": "Calibration reliability. " * 1000}]
    spans = TopicResearch.targeted_spans(pages, "What is calibration reliability?")
    assert len(spans) == 6
    assert all(span["page"] == 2 and len(span["text"]) <= 1800 for span in spans)
    for span in spans:
        assert span["text"] == pages[span["page"] - 1]["text"][span["start"]:span["end"]]


def test_targeted_retrieval_preserves_paragraph_boundaries():
    first = "Calibration context. " * 60 + "\n\n"
    second = "A separate limitation paragraph. " * 40
    spans = TopicResearch.targeted_spans([{"page": 1, "text": first + second}], "calibration")
    assert spans[0]["text"] == first
    assert spans[1]["text"] == second
    assert spans[0]["end"] == spans[1]["start"]


@pytest.mark.anyio
async def test_workspace_understanding_api_is_grounded_cached_and_revision_guarded(test_settings, stub_provider, monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from backend.research.reviews.router import router

    services = create_services(test_settings)
    monkeypatch.setattr(stub_provider, "_reply_for", brief_reply)
    app = FastAPI()
    app.state.services = services
    app.include_router(router, prefix="/api")
    try:
        main = configure(services, stub_provider)
        await seed_paper(services, title="Calibration original", suffix="workspace")
        spec = ReviewCreateRequest(
            title="Workspace question", objective="Compare adaptive calibration.", main_model=main,
            summary_model=SummaryRole(model_reference=main),
            embedding_model=EmbeddingRole(model_reference=embedding_reference(services, stub_provider)),
            web_enabled=False, reading_mode="targeted",
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post("/api/literature-reviews", json=spec.model_dump(mode="json"))
            assert created.status_code == 202, created.text
            review_id = created.json()["id"]
            base = f"/api/literature-reviews/{review_id}"
            await settle(services, review_id)

            async def control(action, **values):
                current = (await client.get(base)).json()
                response = await client.post(f"{base}/control", json={
                    "action": action, "expected_revision": current["revision"], **values,
                })
                assert response.status_code == 200, response.text
                return response.json()

            assert (await client.get(f"{base}/understanding")).json() is None
            await control("approve_plan", work_budget={"screened_papers": 24, "read_papers": 6, "comparison_passes": 1})
            landscape = await settle(services, review_id)
            candidate_id = landscape.topics[0].candidate_ids[0]
            await control("approve_topics", topic_ids=[landscape.topics[0].id],
                          candidate_ids=[candidate_id], reading_mode="targeted", feedback="Calibration assumptions")
            done = await settle(services, review_id)
            assert done.status == "completed", done.error
            snapshot = (await client.get(f"{base}/understanding")).json()
            assert snapshot["candidate_ids"] == [candidate_id]
            assert snapshot["question_focus"] == "Calibration assumptions"
            assert snapshot["markdown"]
            papers = (await client.get(f"{base}/papers")).json()
            assert papers[0]["brief"]["findings"][0]["quote"]
            await control("refine_understanding")
            waiting = await settle(services, review_id)
            assert waiting.approval == "budget"
            assert (await client.get(f"{base}/understanding")).json() == snapshot
            await control("extend_budget", work_budget={"screened_papers": 24, "read_papers": 6, "comparison_passes": 2})
            assert (await settle(services, review_id)).status == "completed"
            calls = len(stub_provider.requests)
            refined = (await client.get(f"{base}/understanding")).json()
            await control("refine_understanding")
            await settle(services, review_id)
            assert len(stub_provider.requests) == calls
            assert (await client.get(f"{base}/understanding")).json() == refined
            assert services.reviews.get(review_id).status == "completed"
            assert (test_settings.workspace_dir / done.workspace_path / "understanding.md").exists()
            from backend.core.errors import ConflictError
            with pytest.raises(ConflictError, match="changed"):
                await client.post(f"{base}/control", json={
                    "action": "refine_understanding", "expected_revision": 1,
                })
            # A changed focus does not silently reuse old briefs as new evidence.
            await control("revise_selection")
            assert (await client.get(f"{base}/understanding")).json() == refined
            assert services.reviews.paper(review_id, candidate_id).brief
    finally:
        await services.close()


@pytest.mark.anyio
async def test_targeted_batch_progress_reselection_reuse_and_full_opt_in(
    test_settings, stub_provider, monkeypatch,
):
    services = create_services(test_settings)
    monkeypatch.setattr(stub_provider, "_reply_for", brief_reply)
    try:
        main = configure(services, stub_provider, api_mode="responses")
        summary = configure(services, stub_provider, name="summary", api_mode="responses")
        for index in range(3):
            await seed_paper(services, title=f"Adaptive paper {index}", suffix=str(index))
        spec = ReviewCreateRequest(
            title="Targeted batch", objective="Compare adaptive evaluation settings.", main_model=main,
            summary_model=SummaryRole(model_reference=summary),
            embedding_model=EmbeddingRole(model_reference=embedding_reference(services, stub_provider)),
            web_enabled=False, reading_mode="targeted",
        )
        assert ReviewCreateRequest.model_validate(
            spec.model_dump(exclude={"reading_mode"}),
        ).reading_mode == "full"
        review = await services.reviews.create(spec)
        await settle(services, review.id)
        await command(services, review.id, "approve_plan")
        waiting = await settle(services, review.id)
        assert waiting.approval == "topics", waiting.error
        assert waiting.candidate_count == 3
        topic_ids = [topic.id for topic in waiting.topics]
        candidates = services.reviews._candidates(review.id)
        ids = [candidate.id for candidate in candidates]
        with pytest.raises(ValidationError, match="nonempty reading batch"):
            await command(services, review.id, "approve_topics", topic_ids=topic_ids, candidate_ids=[])
        with pytest.raises(ValidationError, match="nonempty reading batch"):
            await command(services, review.id, "approve_topics", topic_ids=topic_ids, candidate_ids=["invented"])
        original_prepare = services.reviews._prepare_paper
        prepared = []

        async def prepare(review_id, candidate):
            if prepared:
                first = services.reviews.paper(review_id, prepared[0])
                assert first.brief and first.brief.findings
                path = services.reviews.get(review_id).provisional_path
                assert path and "Pertinent findings" in (test_settings.workspace_dir / path).read_text(encoding="utf-8")
            prepared.append(candidate.id)
            return await original_prepare(review_id, candidate)

        monkeypatch.setattr(services.reviews, "_prepare_paper", prepare)
        await command(services, review.id, "approve_topics", topic_ids=topic_ids,
                      candidate_ids=ids[:2], feedback="Which evaluation assumptions matter?")
        done = await settle(services, review.id)
        assert done.status == "completed", done.error
        assert done.selected_count == 2 and done.candidate_count == 3
        assert done.question_focus == "Which evaluation assumptions matter?"
        progress = services.reviews.paper_progress(review.id)
        assert progress.counts.summarized == 2
        assert all(row.summary_complete for row in progress.items)
        paper = services.reviews.paper(review.id, ids[0])
        assert paper.document_id and paper.brief
        assert paper.brief.reading_mode == "targeted"
        assert paper.brief.source_pages
        assert all(claim["quote"] and claim["source_version"] == paper.source_version for claim in paper.findings)
        assert paper.selective_summary == paper.brief.model_dump(mode="json")
        assert services.reviews.paper(review.id, ids[2]).brief is None
        before = len(stub_provider.requests)
        original_summaries = services.reviews._analysis_summaries(review.id)
        assert len(original_summaries) == 2, [(row.candidate_id, row.error, row.brief is not None) for row in services.reviews.papers(review.id)]
        monkeypatch.setattr(services.reviews, "_prepare_paper", original_prepare)
        await command(services, review.id, "resume", stage="reading_plan")
        resumed = await settle(services, review.id)
        assert services.reviews._analysis_summaries(review.id) == original_summaries
        assert resumed.status == "completed", resumed.error
        assert len(stub_provider.requests) == before
        await command(services, review.id, "revise_selection")
        assert services.reviews.get(review.id).approval == "topics"
        services.reviews.jobs.checkpoint(review.id, work_budget={
            "screened_papers": 100, "read_papers": 20, "comparison_passes": 2,
        })
        await command(services, review.id, "approve_topics", topic_ids=topic_ids,
                      candidate_ids=[ids[0]], reading_mode="full")
        assert services.reviews.paper_progress(review.id).counts.summarized == 0
        full = await settle(services, review.id)
        assert full.status == "completed", full.error
        assert full.reading_mode == "full"
        assert full.candidate_count == 3 and full.selected_count == 1
        paper = services.reviews.paper(review.id, ids[0])
        assert paper.brief is None and paper.selective_summary
        assert "FILE_ONLY_PAPER_SUMMARY" in json.dumps(paper.selective_summary)
        assert any(row.get("summary_method") == "targeted-v2"
                   for row in services.reviews.repository.paper_jobs(review.id, ids[0]))
        await command(services, review.id, "revise_selection")
        services.reviews.jobs.checkpoint(review.id, work_budget={
            "screened_papers": 100, "read_papers": 20, "comparison_passes": 3,
        })
        await command(services, review.id, "approve_topics", topic_ids=topic_ids,
                      candidate_ids=[ids[0]], reading_mode="targeted", feedback="What is unknown?")
        assert services.reviews.paper_progress(review.id).counts.summarized == 0
        refocused = await settle(services, review.id)
        assert refocused.status == "completed", refocused.error
        assert services.reviews.paper(review.id, ids[0]).brief.question_focus == "What is unknown?"
        assert len([row for row in services.reviews.repository.paper_jobs(review.id, ids[0])
                    if row["key"].startswith("paper-brief:")]) == 2
    finally:
        await services.close()
