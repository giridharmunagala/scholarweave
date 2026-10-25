from __future__ import annotations

import asyncio
import json
import threading
from unittest.mock import Mock

import httpx
import pytest

from backend.agents.context import ScholarWeaveContext
from backend.app import create_app
from backend.bootstrap import create_services
from backend.conversations.turns import deep_work_blueprint, research_blueprint
from backend.core.errors import ConflictError, NotFoundError, ValidationError
from backend.providers.decisions import score_decisions, validate_decisions
from backend.providers.schemas import ProviderCreate, ProviderModel
from backend.providers.types import ModelReference, ProviderRuntimeError
from backend.research.reviews.discovery import normalize_candidate, candidate_key, ScholarlyDiscovery
from backend.research.reviews.schemas import Candidate, DecisionRole, ReviewControlRequest, ReviewCreateRequest, ReviewScope, SummaryRole
from backend.research.search import AsyncRateLimiter, rate_limit_delay
from backend.research.sources import SourceDownloadService
from backend.tools.policy import ToolInputError


def review_reply(payload: dict) -> str:
    schema = (payload.get("response_format") or {}).get("json_schema") or (payload.get("text") or {}).get("format", {})
    name = schema["name"]
    content = next(item["content"] for item in reversed(payload["messages"]) if item["role"] == "user")
    if isinstance(content, list):
        content = "".join(item.get("text", "") for item in content)
    packet = json.loads(content)
    records = packet.get("records", [])
    if name == "ReviewScope":
        result = {
            "questions": ["Where do adaptive methods fail?"], "inclusion_criteria": ["Original evidence"],
            "exclusion_criteria": ["Unrelated topics"],
            "queries": [{"provider": "library", "query": "adaptive", "pages": 1}],
            "suggested_allowance_tokens": 1_000_000,
            "suggested_work_budget": {"screened_papers": 100, "read_papers": 20, "comparison_passes": 1},
            "rationale": "A modest local review with room for source auditing.",
        }
    elif name == "ReferenceSeeds":
        result = {"candidate_ids": [row["candidate_id"] for row in records], "reason": "Follow complementary supplied references."}
    elif name == "ScreeningBatch":
        result = {"candidates": [{
            "candidate_id": row["candidate_id"], "kind": "research", "relevance": "HIGHLY_RELEVANT",
            "reason": "Addresses adaptive methods.", "matched_questions": packet["questions"],
        } for row in records]}
    elif name == "ReadingPlan":
        result = {"pages": [{
            "page": row["page"], "action": "read" if row["page"] == 1 else "defer",
            "reason": "Inspect the primary result first; request other pages if needed.",
            "reference_page": False,
        } for row in records]}
    elif name == "ReferenceExtraction":
        result = {"references": []}
    elif name == "ReadingGroups":
        result = {"groups": [[row["page"] for row in records]]}
    elif name == "EvidencePacket":
        result = {
            "claims": [{
                "finding": "Adaptive results depend on the evaluation setting.", "category": "result",
                "quote": row["text"].strip(), "page": row["page"],
            } for row in records],
            "limitations": ["Selective extraction, not a complete paper summary."],
            "follow_up_pages": [2] if packet.get("page_count", 3) > 1 and any(row["page"] == 1 for row in records) else [],
            "partial_summary": [{
                "title": "Results", "content": "Adaptive results depend on the inspected evaluation setting.",
                "claim_indexes": list(range(len(records))),
            }] if records else [],
        }
    elif name == "SelectiveSummary":
        if packet["stage"] == "audit":
            proposal = packet["proposal"]
            result = {"sections": [{**proposal, "evidence_ids": [row["id"] for row in records]}],
                      "limitations": ["Selective source coverage."]}
        else:
            sections = [section for row in records for section in row["sections"]]
            result = {"sections": [{
                "title": "Results", "content": "The combined inspected results depend on evaluation conditions.",
                "evidence_ids": list(dict.fromkeys(identifier for section in sections for identifier in section["evidence_ids"])),
            }], "limitations": ["Selective source coverage."]}
    elif name == "Synthesis":
        if packet["stage"] == "reduction":
            result = {
                "sections": [section for row in records for section in row["sections"]],
                "gaps": [gap for row in records for gap in row["gaps"]],
                "limitations": ["Not exhaustive."],
            }
        else:
            ids = [row["id"] for row in records]
            result = {
                "sections": [{
                    "title": "Evaluation assumptions", "content": "The inspected experiments have limited scope.",
                    "evidence_ids": ids,
                }],
                "gaps": [{
                    "hypothesis": "Test adaptive methods outside the inspected setting.",
                    "basis": "inferred", "evidence_ids": ids, "counter_evidence_ids": [],
                    "caveat": "This corpus cannot establish novelty.", "next_check": "Search for out-of-domain evaluations.",
                }] if packet["stage"] != "audit" or "gap" in packet["proposal"] else [],
                "limitations": ["This is a scoped, selective review."],
            }
    else:
        raise AssertionError(f"Unexpected worker schema: {name}")
    return json.dumps(result)


async def seed_paper(services, *, title="Adaptive methods", suffix="") -> str:
    texts = [
        "Adaptive methods improve the measured result in one controlled setting. " + suffix,
        "Adaptive methods were not evaluated outside this setting. This appendix details the limitations.",
        "References and acknowledgments for the adaptive methods study.",
    ]
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n" + suffix.encode() + b"\n%%EOF", filename="adaptive.pdf", title=title,
    )
    services.document_ingestion._persist_document_content(
        document, [{"page": index + 1, "text": text, "ocr_used": False} for index, text in enumerate(texts)],
        enhancement_model=None, extraction_mode="embedded",
    )
    return document.id


def configure(services, stub_provider, *, api_mode="chat_completions", name="main", kind="openai_compatible"):
    provider = services.providers.create(ProviderCreate(
        name=name, kind=kind, api_key="stub-key", base_url=f"{stub_provider.base_url}/v1", api_mode=api_mode,
        models=[ProviderModel(name=name, capabilities={"chat"}, context_window_tokens=32768)],
    ))
    return {"provider_profile_id": provider.id, "model": name}


async def seed_parallel_summary_review(services, stub_provider, *, parallel_requests):
    main = configure(services, stub_provider)
    fast = configure(services, stub_provider, name="parallel-summary")
    role = SummaryRole(model_reference=fast, parallel_requests=parallel_requests)
    spec = ReviewCreateRequest(title="Parallel summaries", objective="Compare methods.", main_model=main, summary_model=role)
    services.reviews.repository.create("parallel", spec.model_dump(mode="json"), {
        "status": "running", "stage": "reading",
        "work_budget": {"screened_papers": 100, "read_papers": 20, "comparison_passes": 1},
    })
    candidates = []
    for index in range(4):
        document_id = await seed_paper(services, suffix=f"parallel-{index}")
        candidate = Candidate(id=f"parallel-{index}", title=f"Paper {index}", document_id=document_id, selected=True)
        services.reviews.repository.save_candidate("parallel", candidate.id, candidate)
        version = services.documents.source_revision(document_id)["source_version"]
        key = f"reading-plan:{candidate.id}:{version}:content-v2"
        services.reviews.repository.save_job("parallel", key, "reading_plan", {
            "key": key, "status": "completed", "pages": [{"page": 1, "action": "read", "reason": "Core method."}],
        })
        candidates.append(candidate)
    return candidates, role


@pytest.mark.anyio
@pytest.mark.parametrize("parallel_requests", [1, 4])
async def test_parallel_summaries_overlap_only_to_the_selected_limit_and_preserve_completed_work(
    test_settings, stub_provider, parallel_requests, monkeypatch,
):
    services = create_services(test_settings)
    release_streams = threading.Event()
    task = None
    try:
        candidates, role = await seed_parallel_summary_review(services, stub_provider, parallel_requests=parallel_requests)
        stub_provider.reply = "## Method\n\nA fast summary of the admitted original text."
        stub_provider.stream_gates["parallel-summary"] = release_streams
        if parallel_requests > 1:
            last = candidates[-1]
            version = services.documents.source_revision(last.document_id)["source_version"]
            key = f"reading-plan:{last.id}:{version}:content-v2"
            services.reviews.repository.save_job("parallel", key, "reading_plan", {
                **services.reviews.repository.job("parallel", key), "status": "running",
            })
            monkeypatch.setattr(stub_provider, "_reply_for", lambda payload:
                                review_reply(payload) if payload.get("response_format") else stub_provider.reply)
        task = asyncio.create_task(services.reviews._reading("parallel"))
        async with asyncio.timeout(10):
            while stub_provider.max_active_inference < parallel_requests:
                await asyncio.sleep(0.01)
        release_streams.set()
        await task
        assert stub_provider.max_active_inference == parallel_requests
        summary_requests = [request for request in stub_provider.requests if request["model"] == "parallel-summary"]
        assert len(summary_requests) == len(candidates)
        assert all(not request.get("tools") and not request.get("response_format") for request in summary_requests)
        assert stub_provider.request_order == (["main"] if parallel_requests > 1 else []) + ["parallel-summary"] * 4
        assert all(services.reviews.paper("parallel", candidate.id).selective_summary for candidate in candidates)
        assert services.inference_scheduler.snapshot()["active_requests"] == 0
        assert services.reviews.get("parallel").active_run_id is None
        services.reviews.jobs.checkpoint(
            "parallel", summary_model=role.model_copy(update={"parallel_requests": 2}).model_dump(mode="json"),
        )
        await services.reviews._reading("parallel")
        assert len(stub_provider.requests) == len(candidates) + (parallel_requests > 1)
    finally:
        release_streams.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await services.close()


@pytest.mark.anyio
async def test_parallel_summary_pause_cancels_every_child_and_resume_keeps_completed_summaries(
    test_settings, stub_provider, monkeypatch,
):
    services = create_services(test_settings)
    try:
        candidates, role = await seed_parallel_summary_review(services, stub_provider, parallel_requests=3)
        stub_provider.reply = "## Method\n\nSaved summary prose."
        await services.reviews._reading("parallel", candidates=candidates[:1])
        first_summary = services.reviews.paper("parallel", candidates[0].id).selective_summary
        stub_provider.stream_delay_seconds = 2
        task = asyncio.create_task(services.reviews._reading("parallel"))
        services.reviews._tasks["parallel"] = task
        async with asyncio.timeout(10):
            while stub_provider.active_inference < 3:
                await asyncio.sleep(0.01)
        current = services.reviews.get("parallel")
        await services.reviews.control("parallel", ReviewControlRequest(action="pause", expected_revision=current.revision))
        assert task.done()
        jobs = [job for job in services.reviews.repository.jobs("parallel", "reading")
                if job["key"].startswith("compact-summary:")]
        cancelled = [job for job in jobs if job["status"] == "cancelled"]
        assert len(cancelled) == 3
        assert all(services.reviews.jobs.runs.get(job["run_id"]).status == "cancelled" for job in cancelled)
        assert services.inference_scheduler.snapshot()["active_requests"] == 0
        assert services.reviews.paper("parallel", candidates[0].id).selective_summary == first_summary
        stub_provider.stream_delay_seconds = 0.8
        stub_provider.max_active_inference = 0
        monkeypatch.setattr(services.reviews, "_schedule", Mock())
        current = services.reviews.get("parallel")
        await services.reviews.control("parallel", ReviewControlRequest(
            action="resume", expected_revision=current.revision,
            summary_model=role.model_copy(update={"parallel_requests": 2}),
        ))
        await services.reviews._reading("parallel")
        assert stub_provider.max_active_inference == 2
        assert all(services.reviews.paper("parallel", candidate.id).selective_summary for candidate in candidates)
        assert services.reviews.paper("parallel", candidates[0].id).selective_summary == first_summary
        assert len(stub_provider.requests) == len(candidates)
    finally:
        await services.close()


@pytest.mark.anyio
async def test_summary_tokenizer_unavailable_is_visible_and_invalid_counts_block(test_settings, stub_provider, monkeypatch):
    from backend.research.reviews.schemas import AnalysisSources

    services = create_services(test_settings)
    try:
        main = configure(services, stub_provider)
        spec = ReviewCreateRequest(title="Tokenizer safety", objective="Compare methods.", main_model=main)
        services.reviews.repository.create("tokenizer-safety", spec.model_dump(mode="json"), {
            "status": "running", "stage": "synthesis",
        })
        prepared = services.reviews.jobs.prepare(spec.main_model, "literature-analysis-sources", AnalysisSources)
        records = [{"summary": "A short summary."}]
        assert await services.reviews.jobs.summary_packets("tokenizer-safety", records, {}, prepared)
        assert prepared[1].method == "utf8-byte-upper-bound"
        assert any("conservative byte-based batches" in notice for notice in services.reviews.get("tokenizer-safety").notices)
        stub_provider.tokenizer_enabled = True
        monkeypatch.setattr(stub_provider, "tokenize", lambda payload: {"tokens": []})
        with pytest.raises(ValidationError, match="invalid tokenizer response"):
            await services.reviews.jobs.summary_packets("tokenizer-safety", records, {}, prepared)
        assert not stub_provider.requests
    finally:
        await services.close()


@pytest.mark.anyio
@pytest.mark.parametrize("api_mode", ["chat_completions", "responses"])
async def test_compact_summary_is_one_tool_free_capped_call_and_reuses_receipts(
    test_settings, stub_provider, monkeypatch, api_mode,
):
    services = create_services(test_settings)
    requests = []
    method_text = "The method adapts its components to the supplied evaluation setting. " * 12
    results_text = "The measured result depends on the assumptions of this evaluation. " * 15
    summary_text = f"## Method\n\n{method_text}\n\n## Results\n\n{results_text}"

    def reply(payload):
        requests.append(payload)
        assert not payload.get("response_format")
        assert payload.get("text", {}).get("format", {}).get("type") != "json_schema"
        instructions = payload["messages"][0]["content"]
        if isinstance(instructions, list):
            instructions = "".join(item.get("text", "") for item in instructions)
        assert "10-15 sentences" in instructions
        assert "There is no per-section word limit" in instructions
        content = next(row["content"] for row in reversed(payload["messages"]) if row["role"] == "user")
        if isinstance(content, list):
            content = "".join(item.get("text", "") for item in content)
        packet = json.loads(content)
        assert [row["page"] for row in packet["records"]] == [1, 2, 3]
        return summary_text

    monkeypatch.setattr(stub_provider, "_reply_for", reply)
    try:
        main = configure(services, stub_provider)
        provider = services.providers.create(ProviderCreate(
            name="Summary", kind="openai_compatible", base_url=f"{stub_provider.base_url}/v1", api_mode=api_mode,
            models=[ProviderModel(name="fast-summary", capabilities={"chat"}, context_window_tokens=262144,
                                  reasoning_efforts=["none", "low"])],
        ))
        role = SummaryRole(model_reference={"provider_profile_id": provider.id, "model": "fast-summary"},
                           reasoning_effort="none")
        assert role.max_output_tokens == 26214
        document_id = await seed_paper(services)
        spec = ReviewCreateRequest(title="Compact review", objective="Compare adaptive methods.",
                                   main_model=main, summary_model=role, web_enabled=False)
        services.reviews.repository.create("compact", spec.model_dump(mode="json"), {
            "status": "running", "stage": "reading", "scope": None, "allowance_tokens": 10_000_000,
        })
        candidate = Candidate(id="compact-paper", title="Adaptive methods", document_id=document_id, selected=True)
        services.reviews.repository.save_candidate("compact", candidate.id, candidate)
        version = services.documents.source_revision(document_id)["source_version"]
        plan_key = f"reading-plan:{candidate.id}:{version}:content-v2"
        plan = {"key": plan_key, "status": "completed", "pages": [
            {"page": number, "action": "read", "reason": "Approved content."} for number in [1, 2, 3]
        ]}
        services.reviews.repository.save_job("compact", plan_key, "reading_plan", plan)

        await services.reviews._reading("compact")

        assert len(requests) == 1
        payload = stub_provider.response_api_requests[0] if api_mode == "responses" else requests[0]
        assert payload["model"] == "fast-summary" and not payload.get("tools")
        if api_mode == "responses":
            assert payload["max_output_tokens"] == 26214
            assert payload["reasoning"]["effort"] == "none"
            assert payload["store"] is False
        else:
            assert payload.get("max_tokens", payload.get("max_completion_tokens")) == 26214
            assert payload["reasoning_effort"] == "none"
        paper = services.reviews.paper("compact", candidate.id)
        assert paper.read_pages == [1, 2, 3] and paper.findings == []
        assert all(section["source_pages"] == [1, 2, 3] for section in paper.selective_summary["sections"])
        assert [section["content"] for section in paper.selective_summary["sections"]] == [summary_text]
        assert all(len(section["content"].split()) > 80 for section in paper.selective_summary["sections"])
        assert "not received a separate model audit" in paper.selective_summary["limitations"][0]
        assert services.reviews.get("compact").summary_model == role
        assert services.reviews.repository.job("compact", plan_key) == plan
        worker = next(job for job in services.reviews.repository.jobs("compact", "reading")
                      if job.get("provenance", {}).get("model") == "fast-summary")
        assert worker["provenance"]["context_tokens"] == 131072
        assert services.summaries.versions(document_id) == []
        usage = services.reviews.jobs.usage("compact")[0]
        assert usage > 0

        await services.reviews._reading("compact")
        assert len(requests) == 1 and services.reviews.jobs.usage("compact")[0] == usage
        services.reviews.jobs.checkpoint(
            "compact", summary_model=role.model_copy(update={"max_output_tokens": 1024}).model_dump(mode="json"),
        )
        await services.reviews._reading("compact")
        assert len(requests) == 1
        services.document_ingestion._persist_document_content(
            services.documents.get_document(document_id),
            [{"page": 1, "text": "Replacement original source text.", "ocr_used": False}],
            enhancement_model=None, extraction_mode="embedded",
        )
        assert services.reviews.paper("compact", candidate.id).selective_summary is None
    finally:
        await services.close()


@pytest.mark.anyio
@pytest.mark.parametrize(("rejected_output", "selection_page"), [
    (None, 1), (None, 999), ("quote", 1),
])
async def test_compact_summary_saves_prose_without_evidence_bookkeeping_and_analysis_reads_originals(
    test_settings, stub_provider, monkeypatch, rejected_output, selection_page,
):
    from backend.research.reviews.jobs import digest

    services = create_services(test_settings)
    calls = []
    candidates = []
    summary_result = {
        "claims": [{"finding": "A promising adaptive method.", "category": "method",
                    "quote": "Approximate wording of the adaptive approach.", "page": 1} for _ in range(15)],
        "partial_summary": [{"title": "Method", "content": "A useful paraphrase of the adaptive method.",
                             "claim_indexes": [0]}],
        "limitations": [], "follow_up_pages": [],
    }
    prose_result = "## Method\n\nA useful paraphrase of the adaptive method."

    def reply(payload):
        schema = (payload.get("response_format") or {}).get("json_schema", {}).get("name", "plain_summary")
        packet = json.loads(next(row["content"] for row in reversed(payload["messages"]) if row["role"] == "user"))
        calls.append((schema, payload["model"], packet))
        if schema == "plain_summary":
            return prose_result
        if schema == "AnalysisSources":
            return json.dumps({"sources": [{
                "candidate_id": candidates[0].id, "pages": [selection_page], "reason": "Verify the promising method.",
            }]})
        if schema == "EvidencePacket":
            assert packet["analysis_source"] is True
            assert packet["records"][0]["text"].startswith("Adaptive methods improve the measured result")
            assert summary_result["claims"][0]["quote"] not in packet["records"][0]["text"]
            result = json.loads(review_reply(payload))
            result["follow_up_pages"] = []
            return json.dumps(result)
        return review_reply(payload)

    monkeypatch.setattr(stub_provider, "_reply_for", reply)
    try:
        main = configure(services, stub_provider)
        fast = configure(services, stub_provider, name="fast-summary")
        role = SummaryRole(model_reference=fast)
        spec = ReviewCreateRequest(title="Fast summaries", objective="Compare adaptive methods.", main_model=main,
                                   summary_model=role, web_enabled=False)
        services.reviews.repository.create("fast-summaries", spec.model_dump(mode="json"), {
            "status": "running", "stage": "reading", "allowance_tokens": 10_000_000,
            "scope": {"questions": ["Compare adaptive methods."], "inclusion_criteria": [],
                      "exclusion_criteria": [], "queries": [{"provider": "library", "query": "adaptive", "pages": 1}],
                      "rationale": "A scoped comparison."},
        })
        for index in range(2):
            document_id = await seed_paper(services, suffix=str(index))
            candidate = Candidate(id=f"fast-{index}", title=f"Adaptive {index}", selected=True, document_id=document_id)
            candidates.append(candidate)
            services.reviews.repository.save_candidate("fast-summaries", candidate.id, candidate)
            revision = services.documents.source_revision(document_id)
            key = f"reading-plan:{candidate.id}:{revision['source_version']}:content-v2"
            services.reviews.repository.save_job("fast-summaries", key, "reading_plan", {
                "key": key, "status": "completed",
                "pages": [{"page": page, "action": "read" if page == 1 else "defer", "reason": "Section role."}
                          for page in (1, 2, 3)],
            })
            if rejected_output:
                pages, _, _ = services.reviews._paper(candidate)
                packet = services.reviews._source_packets([pages[0]], {
                    "title": candidate.title, "page_count": len(pages), **revision,
                    "reading_policy": "content-v2", "summary_method": "compact-v1",
                    "summary_role": role.model_dump(mode="json", exclude={"parallel_requests"}),
                }, services.reviews._prepare_summary(role))[0]
                rejected_key = f"compact-summary:{document_id}:previous"
                services.reviews.repository.save_job("fast-summaries", rejected_key, "reading", {
                    "key": rejected_key, "status": "invalid" if index == 0 else "retry_requested", "raw_result": {
                        **summary_result, "partial_summary": [{
                            **summary_result["partial_summary"][0], "claim_indexes": [14, 15],
                        }],
                    },
                    "validation_error": "An evidence claim did not quote the admitted source page verbatim.",
                    "provenance": {"input_hash": digest(packet), "model": fast["model"],
                                   "provider_profile_id": fast["provider_profile_id"]},
                    "tokens_spent": 100, "usage_estimated": False,
                })

        await services.reviews._reading("fast-summaries")

        assert len(calls) == (0 if rejected_output else 2)
        assert all(schema == "plain_summary" and model == "fast-summary" for schema, model, _ in calls)
        saved_summaries = [services.reviews.paper("fast-summaries", candidate.id).selective_summary for candidate in candidates]
        for candidate in candidates:
            paper = services.reviews.paper("fast-summaries", candidate.id)
            assert paper.selective_summary and paper.read_pages == [1]
            assert paper.evidence_count == 0
            assert paper.findings == []
            assert paper.selective_summary["sections"][0]["source_pages"] == [1]
        assert services.reviews._evidence("fast-summaries") == []
        assert not any("automatically repairing" in notice for notice in services.reviews.get("fast-summaries").notices)
        scheduled = []
        monkeypatch.setattr(services.reviews, "_schedule", scheduled.append)
        services.reviews._stop_stage("fast-summaries")
        assert services.reviews.get("fast-summaries").stage == "references"
        assert scheduled == ["fast-summaries"]

        services.reviews.jobs.checkpoint("fast-summaries", stage="synthesis")
        if selection_page == 999:
            with pytest.raises(ValidationError, match="offered papers and available original pages"):
                await services.reviews._synthesis("fast-summaries")
            assert sum(schema == "AnalysisSources" for schema, _, _ in calls) == 3
            assert not any(schema == "EvidencePacket" for schema, _, _ in calls)
            assert [services.reviews.paper("fast-summaries", candidate.id).selective_summary for candidate in candidates] == saved_summaries
            return
        await services.reviews._synthesis("fast-summaries")

        assert {model for schema, model, _ in calls if schema != "plain_summary"} == {"main"}
        assert sum(schema == "EvidencePacket" for schema, _, _ in calls) == 1
        evidence = services.reviews._evidence("fast-summaries")
        assert {claim["candidate_id"] for claim in evidence} == {candidates[0].id}
        assert all(claim["quote"].startswith("Adaptive methods improve") for claim in evidence)
        assert [services.reviews.paper("fast-summaries", candidate.id).selective_summary for candidate in candidates] == saved_summaries
        before = len(calls)
        await services.reviews._synthesis("fast-summaries")
        assert len(calls) == before
        assert services.reviews.jobs.work_usage("fast-summaries")["comparison_passes"] == 1
        await services.reviews._publication("fast-summaries")
        path = services.reviews.get("fast-summaries").output_path
        published = str(services.workspace.read_file(path).content)
        assert "Unverified summary lead; original page references: 1" in published
        assert summary_result["claims"][0]["quote"] not in published
        assert "Adaptive methods improve the measured result" in published
        services.document_ingestion._persist_document_content(
            services.documents.get_document(candidates[0].document_id),
            [{"page": 1, "text": "Changed original source.", "ocr_used": False}],
            enhancement_model=None, extraction_mode="embedded",
        )
        assert services.reviews._evidence("fast-summaries") == []
        with pytest.raises(ValidationError, match="missing or unknown source evidence"):
            await services.reviews._publication("fast-summaries")
    finally:
        await services.close()


@pytest.mark.anyio
async def test_main_model_switch_validates_before_updating_or_resuming(test_settings, stub_provider, monkeypatch):
    services = create_services(test_settings)
    scheduled = []
    monkeypatch.setattr(services.reviews, "_schedule", scheduled.append)
    try:
        main = configure(services, stub_provider)
        spec = ReviewCreateRequest(title="Saved research", objective="Compare methods.", main_model=main)
        services.reviews.repository.create("invalid-switch", spec.model_dump(mode="json"), {
            "status": "paused", "stage": "reading", "stage_history": {},
        })
        current = services.reviews.get("invalid-switch")
        with pytest.raises(ProviderRuntimeError):
            await services.reviews.control("invalid-switch", ReviewControlRequest(
                action="resume", expected_revision=current.revision,
                main_model={"provider_profile_id": "missing-provider", "model": "replacement"},
            ))
        assert services.reviews.get("invalid-switch") == current
        services.reviews.jobs.checkpoint("invalid-switch", status="running")
        with pytest.raises(ConflictError, match="already running"):
            await services.reviews.control("invalid-switch", ReviewControlRequest(
                action="resume", expected_revision=services.reviews.get("invalid-switch").revision, main_model=main,
            ))
        assert not scheduled
    finally:
        await services.close()


@pytest.mark.anyio
async def test_main_model_switch_reuses_completed_jobs_with_original_provenance(test_settings, stub_provider, monkeypatch):
    services = create_services(test_settings)
    monkeypatch.setattr(services.reviews, "_schedule", lambda review_id: None)
    monkeypatch.setattr(stub_provider, "_reply_for", review_reply)
    try:
        main = configure(services, stub_provider)
        replacement = configure(services, stub_provider, name="replacement-main")
        spec = ReviewCreateRequest(title="Saved research", objective="Compare methods.", main_model=main)
        services.reviews.repository.create("reuse-main", spec.model_dump(mode="json"), {
            "status": "running", "stage": "scope",
            "work_budget": {"screened_papers": 100, "read_papers": 20, "comparison_passes": 1},
        })
        packet = {"objective": "Compare methods."}
        old = services.reviews.jobs.prepare(spec.main_model, "literature-scope", ReviewScope)
        result = await services.reviews.jobs.call("reuse-main", "scope", "scope", packet, old, ReviewScope)
        receipts = services.reviews.repository.jobs("reuse-main", "scope")
        services.reviews.jobs.checkpoint("reuse-main", status="paused")
        await services.reviews.control("reuse-main", ReviewControlRequest(
            action="resume", expected_revision=services.reviews.get("reuse-main").revision, main_model=replacement,
        ))
        new = services.reviews.jobs.prepare(services.reviews._spec("reuse-main").main_model, "literature-scope", ReviewScope)
        assert await services.reviews.jobs.call("reuse-main", "scope", "scope", packet, new, ReviewScope) == result
        assert services.reviews.repository.jobs("reuse-main", "scope") == receipts
        assert len(stub_provider.requests) == 1
        await services.reviews.jobs.call(
            "reuse-main", "scope", "scope", {"objective": "A new question."}, new, ReviewScope,
        )
        assert [request["model"] for request in stub_provider.requests] == ["main", "replacement-main"]
    finally:
        await services.close()


@pytest.mark.anyio
async def test_oversized_compact_summary_preserves_all_spans_and_resumes_without_duplicate_calls(
    test_settings, stub_provider, monkeypatch,
):
    from backend.research.reviews.jobs import ReviewPaused

    services = create_services(test_settings)
    packets = []
    texts = ["First source evidence. " * 600, "Second source evidence. " * 600]

    def reply(payload):
        packet = json.loads(next(row["content"] for row in reversed(payload["messages"]) if row["role"] == "user"))
        packets.append(packet)
        return "## Results\n\nA scoped result from these source spans."

    monkeypatch.setattr(stub_provider, "_reply_for", reply)
    try:
        reference = configure(services, stub_provider)
        provider = services.providers.create(ProviderCreate(
            name="Small context summary", kind="openai_compatible", base_url=f"{stub_provider.base_url}/v1",
            models=[ProviderModel(name="small-summary", capabilities={"chat"}, context_window_tokens=16384)],
        ))
        role = SummaryRole(model_reference={"provider_profile_id": provider.id, "model": "small-summary"},
                           max_output_tokens=2048)
        document_id = await seed_paper(services)
        services.document_ingestion._persist_document_content(
            services.documents.get_document(document_id),
            [{"page": index + 1, "text": text, "ocr_used": False} for index, text in enumerate(texts)],
            enhancement_model=None, extraction_mode="embedded",
        )
        spec = ReviewCreateRequest(title="Oversized review", objective="Compare methods.", main_model=reference,
                                   summary_model=role)
        services.reviews.repository.create("oversized-compact", spec.model_dump(mode="json"), {
            "status": "running", "stage": "reading", "allowance_tokens": 10_000_000,
        })
        candidate = Candidate(id="long-paper", title="Long paper", document_id=document_id, selected=True)
        services.reviews.repository.save_candidate("oversized-compact", candidate.id, candidate)
        version = services.documents.source_revision(document_id)["source_version"]
        key = f"reading-plan:{candidate.id}:{version}:content-v2"
        services.reviews.repository.save_job("oversized-compact", key, "reading_plan", {
            "key": key, "status": "completed",
            "pages": [{"page": page, "action": "read", "reason": "Core evidence."} for page in [1, 2]],
        })
        publish = services.reviews._publish_progress

        def pause_after_receipt(review_id):
            if any(job.get("kind") == "evidence" for job in services.reviews.repository.jobs(review_id, "reading")):
                services.reviews.jobs.checkpoint(review_id, status="paused")
            publish(review_id)

        monkeypatch.setattr(services.reviews, "_publish_progress", pause_after_receipt)
        with pytest.raises(ReviewPaused):
            await services.reviews._reading("oversized-compact")
        assert len(packets) == 1
        monkeypatch.setattr(services.reviews, "_publish_progress", publish)
        services.reviews.jobs.checkpoint("oversized-compact", status="running")

        await services.reviews._reading("oversized-compact")

        assert len(packets) > 1
        for page, text in enumerate(texts, 1):
            spans = [span for packet in packets for span in packet["records"] if span["page"] == page]
            assert "".join(span["text"] for span in spans) == text
            assert spans[0]["start"] == 0 and spans[-1]["end"] == len(text)
        paper = services.reviews.paper("oversized-compact", candidate.id)
        assert paper.read_pages == [1, 2] and paper.selective_summary
        assert len(paper.partial_summaries) == len(packets)
        assert any("combined locally" in notice for notice in services.reviews.get("oversized-compact").notices)
        assert {request["model"] for request in stub_provider.requests} == {"small-summary"}
        assert all(not request.get("response_format") for request in stub_provider.requests)
    finally:
        await services.close()


@pytest.mark.anyio
async def test_failed_preparation_keeps_linked_document_and_clears_active_operation(test_settings, stub_provider, monkeypatch):
    services = create_services(test_settings)
    try:
        reference = configure(services, stub_provider)
        spec = ReviewCreateRequest(title="Failed preparation", objective="Compare methods.", main_model=reference)
        services.reviews.repository.create("failed-preparation", spec.model_dump(mode="json"), {
            "status": "running", "stage": "reading_plan",
        })
        document = services.documents.create_document_from_bytes(
            b"%PDF-1.4\n%%EOF", filename="unreadable.pdf", title="Unreadable source",
        )
        candidate = Candidate(id="unreadable", title="Unreadable source", selected=True,
                              url="https://example.org/unreadable.pdf")
        services.reviews.repository.save_candidate("failed-preparation", candidate.id, candidate)

        async def prepare(review_id, current):
            services.reviews.repository.update_candidate(review_id, current.model_copy(update={"document_id": document.id}))
            raise RuntimeError("PDF extraction failed")

        monkeypatch.setattr(services.reviews, "_prepare_paper", prepare)
        with pytest.raises(ValidationError, match="None of the approved papers"):
            await services.reviews._reading_plan("failed-preparation")
        saved = services.reviews.repository.candidate("failed-preparation", candidate.id)
        assert saved.document_id == document.id and saved.error == "PDF extraction failed"
        paper = services.reviews.paper("failed-preparation", candidate.id)
        assert paper.collected and not paper.prepared and paper.operation is None
        assert paper.error == "PDF extraction failed"
    finally:
        await services.close()


@pytest.mark.anyio
@pytest.mark.parametrize("invalid", ["quote", "combined_support"])
async def test_selective_summary_rejects_invalid_source_support(test_settings, stub_provider, monkeypatch, invalid):
    services = create_services(test_settings)

    def reply(payload):
        result = json.loads(review_reply(payload))
        packet = json.loads(next(row["content"] for row in reversed(payload["messages"]) if row["role"] == "user"))
        if "claims" in result:
            result["follow_up_pages"] = []
            if invalid == "quote":
                result["claims"][0]["quote"] = "Invented source text."
        elif payload["response_format"]["json_schema"]["name"] == "SelectiveSummary":
            if invalid == "combined_support" and packet["stage"] == "combine":
                result["sections"][0]["evidence_ids"] = ["another-paper-evidence"]
        return json.dumps(result)

    monkeypatch.setattr(stub_provider, "_reply_for", reply)
    try:
        document_id = await seed_paper(services)
        specification = ReviewCreateRequest(title="Source checks", objective="Review adaptive methods.",
                                            main_model=configure(services, stub_provider), web_enabled=False)
        services.reviews.repository.create("source-checks", specification.model_dump(mode="json"), {
            "status": "running", "stage": "reading", "scope": None, "allowance_tokens": 10_000_000,
        })
        candidate = Candidate(id="source-paper", title="Adaptive methods", document_id=document_id, selected=True)
        services.reviews.repository.save_candidate("source-checks", candidate.id, candidate)
        await services.reviews._reading_plan("source-checks", candidates=[candidate])
        with pytest.raises(ValidationError, match="quote|partial summary|cite their supplied source"):
            await services.reviews._reading("source-checks", candidates=[candidate])
        paper = services.reviews.paper("source-checks", candidate.id)
        assert paper.selective_summary is None
        if invalid == "quote":
            assert paper.partial_summaries == [] and paper.findings == []
    finally:
        await services.close()


async def settle(services, identifier: str):
    task = services.reviews._tasks.get(identifier)
    if task:
        await asyncio.wait_for(asyncio.shield(task), timeout=30)
    return services.reviews.get(identifier)


async def command(services, identifier: str, action: str, **values):
    current = services.reviews.get(identifier)
    return await services.reviews.control(identifier, ReviewControlRequest(
        expected_revision=current.revision, action=action, **values,
    ))


@pytest.mark.anyio
@pytest.mark.parametrize(("protocol", "match"), [
    ("systemone", "exact"),
    ("generation", "ambiguous"),
    ("generation", "irrelevant"),
])
async def test_original_reference_counts_and_automatic_reading(test_settings, stub_provider, monkeypatch, protocol, match):
    services = create_services(test_settings)
    downloads_seen = []
    metadata_seen = []
    original_native = stub_provider.decisions

    def reply(payload):
        schema = (payload.get("response_format") or {}).get("json_schema") or (payload.get("text") or {}).get("format", {})
        text = next(row["content"] for row in reversed(payload["messages"]) if row["role"] == "user")
        packet = json.loads(text)
        if schema["name"] == "ReferenceExtraction":
            return json.dumps({"references": [{
                "title": "Shared adaptive reference", "authors": ["Researcher"], "year": 2024,
                "doi": None, "arxiv_id": "2401.01234v2", "page": row["page"], "quote": row["text"],
            } for row in packet["records"] if row["page"] == 3]})
        result = json.loads(review_reply(payload))
        if schema["name"] == "ReadingPlan":
            for row in result["pages"]:
                row.update(action="read" if row["page"] in (1, 3) or packet.get("neighbor_decisions") else "defer",
                           reference_page=row["page"] == 3)
        if schema["name"] == "ScreeningBatch" and any(row["title"] == "Shared adaptive reference" for row in packet["records"]):
            result["candidates"][0]["relevance"] = {"somewhat": "SOMEWHAT_RELEVANT", "irrelevant": "IRRELEVANT",
                                                   "relevant": "RELEVANT"}.get(match, "HIGHLY_RELEVANT")
        return json.dumps(result)

    def native(payload):
        result = original_native(payload)
        state = payload["state"]
        state = json.loads(state) if isinstance(state, str) else state
        if "outline" in state:
            page = state["outline"]["page"]
            for name, choice in {"priority": "read" if page in (1, 3) or state.get("neighbor_decisions") else "defer",
                                 "references": "yes" if page == 3 else "no"}.items():
                result["answers"][name]["choice"] = choice
                result["answers"][name]["probabilities"] = {
                    key: float(key == choice) for key in result["answers"][name]["probabilities"]
                }
        elif state.get("title") == "Shared adaptive reference":
            choice = {"somewhat": "SOMEWHAT_RELEVANT", "irrelevant": "IRRELEVANT",
                      "relevant": "RELEVANT"}.get(match, "HIGHLY_RELEVANT")
            result["answers"]["relevance"]["choice"] = choice
            result["answers"]["relevance"]["probabilities"] = {
                key: float(key == choice) for key in result["answers"]["relevance"]["probabilities"]
            }
        return result

    async def metadata(query, *, start, limit):
        metadata_seen.append(query)
        return {"results": [{"title": "Shared adaptive reference", "arxiv_id": "2401.99999" if match == "ambiguous" else "2401.01234",
                             "abstract": "Adaptive methods and limitations.", "authors": ["Researcher"]}]}

    async def download(request):
        downloads_seen.append(str(request.url))
        return httpx.Response(200, content=b"%PDF-1.7\nsupplemental\n%%EOF", headers={"Content-Type": "application/pdf"})

    async def public(_url):
        return None

    async def options(_identifier):
        return {"recommended_mode": "embedded", "ocr_available": True}

    async def ingest(identifier, **_kwargs):
        services.document_ingestion._persist_document_content(
            services.documents.get_document(identifier),
            [{"page": 1, "text": "Supplemental references must never be expanded.", "ocr_used": False}],
            enhancement_model=None, extraction_mode="embedded",
        )
        return services.documents.get_document(identifier)

    client = httpx.AsyncClient(transport=httpx.MockTransport(download))
    services.reviews.downloads = SourceDownloadService(test_settings, services.documents, services.workspace, client=client)
    monkeypatch.setattr(services.reviews.downloads, "_validate_public_url", public)
    monkeypatch.setattr(services.documents, "ingestion_options", options)
    monkeypatch.setattr(services.documents, "ingest_document", ingest)
    monkeypatch.setattr(services.research_search, "search_arxiv_page", metadata)
    monkeypatch.setattr(stub_provider, "_reply_for", reply)
    monkeypatch.setattr(stub_provider, "decisions", native)
    try:
        main = configure(services, stub_provider)
        if protocol == "generation":
            decision = configure(services, stub_provider, name="fast")
        else:
            profile = services.providers.create(ProviderCreate(
                name="Fast decision", kind="openai_compatible", base_url=f"{stub_provider.base_url}/v1",
                models=[ProviderModel(name="fast", capabilities={"decision"}, context_window_tokens=8192)],
            ))
            decision = {"provider_profile_id": profile.id, "model": "fast"}
        for index in range(2):
            document_id = await seed_paper(services, title=f"Adaptive original {index}", suffix=str(index))
            services.document_ingestion._persist_document_content(
                services.documents.get_document(document_id),
                [{"page": 1, "text": "Adaptive methods improve results.", "ocr_used": False},
                 {"page": 2, "text": "Evaluation limitations of adaptive methods.", "ocr_used": False},
                 {"page": 3, "text": "References: Researcher. Shared adaptive reference (2024). arXiv:2401.01234v2.", "ocr_used": False}],
                enhancement_model=None, extraction_mode="embedded",
            )
        review = await services.reviews.create(ReviewCreateRequest(
            title="Original references", objective="Compare adaptive methods.", main_model=main,
            decision_model=DecisionRole(model_reference=decision, protocol=protocol),
        ))
        await settle(services, review.id)
        await command(services, review.id, "approve_plan",
                      work_budget={"screened_papers": 10, "read_papers": 2, "comparison_passes": 1})
        waiting = await settle(services, review.id)
        if match in {"exact", "relevant"}:
            assert waiting.approval == "budget", waiting.error
            assert downloads_seen == []
            assert waiting.work_used.read_papers == 2
            await command(services, review.id, "extend_budget",
                          work_budget={"screened_papers": 10, "read_papers": 3, "comparison_passes": 1})
            waiting = await settle(services, review.id)
        original_ids = [row.id for row in services.reviews.candidates(review.id).items]
        assert waiting.status == "completed", waiting.error
        rows = services.reviews.references(review.id)
        assert len(rows) == 1 and rows[0].citing_count == 2
        assert rows[0].arxiv_id == "2401.01234"
        assert len(metadata_seen) == 1
        for identifier in original_ids:
            assert [row.action for row in services.reviews.paper(review.id, identifier).pages] == ["read", "read", "read"]
        before = len(stub_provider.requests) + len(stub_provider.decision_requests)
        services.reviews.jobs.checkpoint(review.id, status="running")
        await services.reviews._references(review.id)
        assert services.reviews.references(review.id)[0].citing_count == 2
        assert len(stub_provider.requests) + len(stub_provider.decision_requests) == before
        services.reviews.jobs.checkpoint(review.id, status="completed")
        if match in {"ambiguous", "somewhat", "irrelevant"}:
            assert downloads_seen == []
            if match == "ambiguous":
                assert any("skipped without guessing" in notice for notice in waiting.notices)
                assert services.reviews.references(review.id)[0].metadata is None
            return
        finished = await settle(services, review.id)
        assert finished.status == "completed", finished.error
        assert len(downloads_seen) == 1
        assert services.reviews.references(review.id)[0].document_id
        assert any("Supplemental references" in row["quote"] for row in services.reviews._evidence(review.id))
        assert services.reviews.paper(review.id, rows[0].id).read_pages == [1]
        assert finished.work_used.read_papers == 3 and finished.work_used.comparison_passes == 1
        assert {row.id for row in services.reviews._candidates(review.id, selected=True)} == set(original_ids)
        await command(services, review.id, "resume", stage="references")
        assert (await settle(services, review.id)).status == "completed"
        assert services.reviews.references(review.id)[0].citing_count == 2
        assert len(downloads_seen) == 1
        source = services.reviews._candidates(review.id, selected=True)[0]
        services.document_ingestion._persist_document_content(
            services.documents.get_document(source.document_id),
            [{"page": 1, "text": "A replaced original source with different references.", "ocr_used": False}],
            enhancement_model=None, extraction_mode="embedded",
        )
        changed = services.reviews.references(review.id)
        assert changed[0].citing_count == 1
        assert changed[0].metadata is None and changed[0].screening is None
        await services.reviews._reference_collection(review.id)
        assert len(downloads_seen) == 1
    finally:
        await client.aclose()
        await services.close()


@pytest.mark.anyio
async def test_distinct_work_budgets_low_relevance_and_comparison_passes(test_settings, stub_provider, monkeypatch):
    services = create_services(test_settings)
    ratings = {"high": "HIGHLY_RELEVANT", "relevant": "RELEVANT",
               "somewhat": "SOMEWHAT_RELEVANT", "irrelevant": "IRRELEVANT"}

    def reply(payload):
        result = json.loads(review_reply(payload))
        if "candidates" in result:
            packet = json.loads(next(item["content"] for item in reversed(payload["messages"]) if item["role"] == "user"))
            titles = {row["candidate_id"]: row["title"] for row in packet["records"]}
            for row in result["candidates"]:
                row["relevance"] = ratings[titles[row["candidate_id"]].split()[-1]]
        return json.dumps(result)

    monkeypatch.setattr(stub_provider, "_reply_for", reply)
    try:
        documents = {}
        for rating in ratings:
            documents[rating] = await seed_paper(services, title=f"Adaptive {rating}", suffix=rating)
        review = await services.reviews.create(ReviewCreateRequest(
            title="Work budgets", objective="Compare adaptive evidence.", main_model=configure(services, stub_provider),
            web_enabled=False,
        ))
        await settle(services, review.id)
        budget = {"screened_papers": 4, "read_papers": 1, "comparison_passes": 1}
        await command(services, review.id, "approve_plan", work_budget=budget)
        waiting = await settle(services, review.id)
        assert waiting.approval == "budget" and waiting.work_used.read_papers == 1
        assert waiting.work_used.screened_papers == 4 and waiting.work_used.comparison_passes == 0
        budget["read_papers"] = 2
        await command(services, review.id, "extend_budget", work_budget=budget)
        done = await settle(services, review.id)
        assert done.status == "completed", done.error
        assert {row.title for row in services.reviews._candidates(review.id, selected=True)} == {"Adaptive high", "Adaptive relevant"}
        assert not any(row["document_id"] in {documents["somewhat"], documents["irrelevant"]}
                       for row in services.reviews._evidence(review.id))
        assert done.work_used.model_dump() == {"screened_papers": 4, "read_papers": 2, "comparison_passes": 1}
        services.document_ingestion._persist_document_content(
            services.documents.get_document(documents["high"]),
            [{"page": page, "text": f"Revised adaptive result on page {page}.", "ocr_used": False} for page in (1, 2, 3)],
            enhancement_model=None, extraction_mode="embedded",
        )
        await command(services, review.id, "resume", stage="reading_plan")
        waiting = await settle(services, review.id)
        assert waiting.approval == "budget" and "comparison passes" in waiting.error
        assert waiting.work_used.model_dump() == done.work_used.model_dump()
        budget["comparison_passes"] = 2
        await command(services, review.id, "extend_budget", work_budget=budget)
        done = await settle(services, review.id)
        assert done.status == "completed", done.error
        assert done.work_used.comparison_passes == 2 and done.work_used.read_papers == 2
        count = len(stub_provider.requests)
        await command(services, review.id, "resume", stage="publication")
        repeated = await settle(services, review.id)
        assert repeated.work_used == done.work_used and len(stub_provider.requests) == count
    finally:
        await services.close()

@pytest.mark.anyio
async def test_legacy_pending_paper_gates_migrate_and_continue_automatically(test_settings, stub_provider, monkeypatch):
    services = create_services(test_settings)
    monkeypatch.setattr(stub_provider, "_reply_for", review_reply)
    try:
        await seed_paper(services)
        review = await services.reviews.create(ReviewCreateRequest(
            title="Legacy recovery", objective="Compare adaptive evidence.", main_model=configure(services, stub_provider),
            web_enabled=False,
        ))
        await settle(services, review.id)
        await command(services, review.id, "approve_plan")
        assert (await settle(services, review.id)).status == "completed"
        for gate, stage in [("papers", "reading_plan"), ("references", "reference_screening"),
                            ("reference_pdfs", "reference_collection")]:
            services.reviews.jobs.checkpoint(
                review.id, status="awaiting_approval", stage=stage, approval=gate, work_budget=None,
                allowance_tokens=1_000_000,
            )
            await services.reviews.start()
            done = await settle(services, review.id)
            assert done.status == "completed", done.error
            assert done.work_budget is not None and done.approval is None
            assert done.work_used.read_papers == 1 and done.work_used.comparison_passes == 1
        services.reviews.jobs.checkpoint(review.id, status="awaiting_approval", stage="reading_plan", approval="papers")
        await command(services, review.id, "resume")
        assert (await settle(services, review.id)).status == "completed"
    finally:
        await services.close()


@pytest.mark.anyio
@pytest.mark.parametrize("protocol", ["generation", "systemone"])
async def test_screening_ranks_and_excludes_before_pagination(test_settings, stub_provider, monkeypatch, protocol):
    services = create_services(test_settings)
    stub_provider.decision_delay_seconds = 0.3
    ratings = ["IRRELEVANT", "SOMEWHAT_RELEVANT", "RELEVANT", "HIGHLY_RELEVANT"]
    original_decisions = stub_provider.decisions

    def reply(payload):
        result = json.loads(review_reply(payload))
        for row in result["candidates"]:
            row["relevance"] = ratings[int(row["candidate_id"].split("-")[-1]) % 4]
        return json.dumps(result)

    def decisions(payload):
        result = original_decisions(payload)
        state = payload["state"]
        if isinstance(state, str):
            state = json.loads(state)
        rating = ratings[int(state["title"].split()[-1]) % 4]
        answer = result["answers"]["relevance"]
        answer["choice"] = rating
        answer["probabilities"] = {key: float(key == rating) for key in answer["probabilities"]}
        return result

    monkeypatch.setattr(stub_provider, "_reply_for", reply)
    monkeypatch.setattr(stub_provider, "decisions", decisions)
    try:
        main = configure(services, stub_provider)
        role = None
        if protocol == "systemone":
            profile = services.providers.create(ProviderCreate(
                name="Decision endpoint", kind="openai_compatible", base_url=f"{stub_provider.base_url}/v1",
                models=[ProviderModel(name="decision", capabilities={"decision"}, context_window_tokens=8192)],
            ))
            role = DecisionRole(model_reference={"provider_profile_id": profile.id, "model": "decision"}, input_tokens=8192)
        specification = ReviewCreateRequest(title="Rank papers", objective="Compare adaptive methods.",
                                            main_model=main, decision_model=role, web_enabled=False)
        services.reviews.repository.create("ranking", specification.model_dump(mode="json"), {
            "status": "running", "stage": "screening", "scope": None, "allowance_tokens": 10_000_000,
        })
        count = 104 if protocol == "generation" else 16
        for index in range(count):
            candidate = Candidate(id=f"candidate-{index}", title=f"Adaptive {index}", abstract="Adaptive method results.")
            services.reviews.repository.save_candidate("ranking", candidate.id, candidate)
        events = services.reviews.progress_events("ranking")
        initial = await anext(events)
        assert initial.screened_count == 0
        await services.reviews._screening("ranking")
        updates = [await asyncio.wait_for(anext(events), 1) for _ in range(count)]
        assert [update.screened_count for update in updates] == list(range(1, count + 1))
        await events.aclose()
        assert not services.reviews._progress_subscribers
        if protocol == "systemone":
            assert stub_provider.max_active_inference == 8
        progress = services.reviews.get("ranking")
        assert progress.screened_count == count
        assert progress.excluded_count == count // 4
        assert progress.candidate_count == count
        items = []
        offset = 0
        while True:
            page = services.reviews.candidates("ranking", limit=25, offset=offset)
            assert page.total == count * 3 // 4
            items.extend(page.items)
            if page.next_offset is None:
                break
            offset = page.next_offset
        assert len({item.id for item in items}) == count * 3 // 4
        assert [item.screening.relevance for item in items] == [
            rating for rating in reversed(ratings[1:]) for _ in range(count // 4)
        ]
        assert services.reviews.repository.candidate("ranking", "candidate-0").screening.relevance == "IRRELEVANT"
        record = services.reviews.repository.get("ranking")
        with pytest.raises(ValidationError, match="automatically excluded"):
            services.reviews.repository.select_candidates(
                "ranking", ["candidate-0"], revision=record.revision, state=record.state,
            )
        assert services.reviews.repository.get("ranking").revision == record.revision
        requests = len(stub_provider.requests) + len(stub_provider.decision_requests)
        await services.reviews._screening("ranking")
        assert len(stub_provider.requests) + len(stub_provider.decision_requests) == requests
    finally:
        await services.close()


@pytest.mark.anyio
async def test_clear_review_removes_owned_artifacts_and_keeps_shared_and_edited_work(test_settings, stub_provider):
    services = create_services(test_settings)
    try:
        spec = ReviewCreateRequest(title="Clear", objective="Adaptive", main_model=configure(services, stub_provider))
        for identifier in ("clear", "other"):
            services.reviews.repository.create(identifier, spec.model_dump(mode="json"), {
                "status": "paused", "stage": "reading_plan",
            })
        documents = {}
        for name in ("owned", "shared", "edited", "pre-existing"):
            document = services.documents.create_document_from_bytes(
                b"%PDF-1.4\n" + name.encode() + b"\n%%EOF", filename=f"{name}.pdf", title=name,
                metadata={"owner_review_id": "clear"} if name != "pre-existing" else {},
            )
            documents[name] = document
            services.reviews.repository.save_candidate("clear", name, Candidate(
                id=name, title=name, document_id=document.id,
            ))
        services.reviews.repository.save_candidate("other", "shared", Candidate(
            id="other-shared", title="shared", document_id=documents["shared"].id,
        ))
        notes = services.workspace.paper_folder(documents["edited"].id) + "/notes.md"
        services.workspace.write_file(notes, "# Researcher's annotations\nKeep this.")
        output = services.workspace.create_note(name="Review result", content="Review findings", tags=["literature-review:clear"])
        services.reviews.repository.save_job("clear", "output", "publication", {
            "kind": "publication_artifact", "path": output.path, "sha256": output.sha256,
        })
        modified = services.workspace.create_note(name="Edited output", content="Researcher's additions", tags=["literature-review:clear"])
        track = services.tracks.create("Cleanup track")
        block = "<!-- cleanup-review -->\nOwned review block"
        published = services.tracks.append_literature_review(
            track.id, block, marker="<!-- cleanup-review -->", paper_ids=[documents["owned"].id],
        )
        services.reviews.repository.save_job("clear", "track-output", "publication", {
            "kind": "publication_artifact", "path": published.path, "track_id": track.id,
            "body": block, "added_paper_ids": [documents["owned"].id],
        })
        with pytest.raises(ConflictError):
            await services.reviews.clear("clear", 999)
        result = await services.reviews.clear("clear", services.reviews.get("clear").revision)
        assert result.deleted_document_ids == [documents["owned"].id]
        assert set(result.preserved_document_ids) == {documents[name].id for name in ("shared", "edited", "pre-existing")}
        assert result.preserved_paths == [modified.path]
        assert services.documents.get_document(documents["owned"].id) is None
        with pytest.raises(FileNotFoundError):
            services.workspace.read_file(output.path)
        with pytest.raises(NotFoundError):
            services.reviews.get("clear")
        assert services.reviews.get("other").candidate_count == 1
        assert services.tracks.get(track.id).paper_ids == []
        assert "Owned review block" not in services.tracks.read_section(track.id, "literature").content
    finally:
        await services.close()


@pytest.mark.anyio
async def test_existing_review_can_move_tracks_without_rewriting_saved_artifacts(test_settings, stub_provider):
    app = create_app(test_settings)
    services = app.state.services
    try:
        old, target = services.tracks.create("Original"), services.tracks.create("Destination")
        spec = ReviewCreateRequest(
            title="Existing findings", objective="Compare prior evidence",
            main_model=configure(services, stub_provider), track_id=old.id,
        )
        output = services.workspace.create_note(name="Saved findings", content="Keep this annotated report")
        services.reviews.repository.create("move-review", spec.model_dump(mode="json"), {
            "status": "completed", "stage": "publication", "output_path": output.path,
        })
        receipt = {"kind": "publication_artifact", "track_id": old.id, "body": "Original publication"}
        services.reviews.repository.save_job("move-review", "topic-publication", "publication", receipt)
        original_state = services.reviews.repository.get("move-review").state
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as http:
            revision = services.reviews.get("move-review").revision
            moved = await http.put("/api/literature-reviews/move-review/track", json={
                "track_id": target.id, "expected_revision": revision,
            })
            assert moved.status_code == 200, moved.text
            assert moved.json()["track_id"] == target.id
            assert moved.json()["output_path"] == output.path
            assert services.reviews.repository.get("move-review").state == original_state
            assert services.workspace.read_file(output.path).content == output.content
            assert services.reviews.list(track_id=old.id) == []
            assert services.reviews.list(track_id=target.id)[0].id == "move-review"
            stale = await http.put("/api/literature-reviews/move-review/track", json={
                "track_id": old.id, "expected_revision": revision,
            })
            assert stale.status_code == 409
            key, previous = services.reviews._publication_receipt("move-review", "topic-publication", target.id)
            assert previous is None and key != "topic-publication"
            services.reviews.repository.save_job("move-review", key, "publication", {**receipt, "track_id": target.id})
            assert services.reviews.repository.job("move-review", "topic-publication") == receipt
            assert services.reviews._publication_receipt("move-review", "topic-publication", old.id)[1] == receipt
            current = services.reviews.get("move-review")
            with pytest.raises(NotFoundError):
                await services.reviews.assign_track(current.id, "missing", current.revision)
            services.reviews.jobs.checkpoint(current.id, status="running")
            current = services.reviews.get(current.id)
            blocked = await http.put(f"/api/literature-reviews/{current.id}/track", json={
                "track_id": old.id, "expected_revision": current.revision,
            })
            assert blocked.status_code == 409
            assert services.reviews.get(current.id).track_id == target.id
    finally:
        await services.close()


@pytest.mark.anyio
async def test_moved_topic_review_preserves_both_publication_receipts(test_settings, stub_provider):
    services = create_services(test_settings)
    try:
        original, destination = services.tracks.create("Original"), services.tracks.create("Destination")
        reference = configure(services, stub_provider)
        spec = ReviewCreateRequest(
            title="Saved topic research", objective="Compare evidence", main_model=reference,
            summary_model={"model_reference": reference}, embedding_model={"model_reference": reference},
            track_id=original.id,
        )
        folder = "research/original/saved-review"
        report = services.workspace.write_file(f"{folder}/report.md", "# Existing findings\n")
        services.reviews.repository.create("moved-topic", spec.model_dump(mode="json"), {
            "status": "completed", "stage": "publication", "workspace_path": folder, "output_path": report.path,
        })
        await services.reviews._publication("moved-topic")
        old_receipt = services.reviews.repository.job("moved-topic", "topic-publication")
        old_content = services.tracks.read_section(original.id, "literature").content
        review = services.reviews.get("moved-topic")
        await services.reviews.assign_track(review.id, destination.id, review.revision)
        await services.reviews._publication(review.id)
        published = services.tracks.read_section(destination.id, "literature")
        await services.reviews._publication(review.id)
        assert services.tracks.read_section(destination.id, "literature").sha256 == published.sha256
        assert services.tracks.read_section(original.id, "literature").content == old_content
        assert services.reviews.repository.job(review.id, "topic-publication") == old_receipt
        assert services.reviews.repository.job(review.id, f"topic-publication:track:{destination.id}")["track_id"] == destination.id
        assert services.reviews.get(review.id).workspace_path == folder
        assert services.workspace.read_file(report.path).content == report.content
    finally:
        await services.close()


@pytest.mark.anyio
async def test_clear_cancels_parallel_native_requests_before_removing_review(test_settings, stub_provider):
    services = create_services(test_settings)
    # Keep every request pending until clear cancels it, even under worker contention.
    stub_provider.decision_delay_seconds = 60
    try:
        profile = services.providers.create(ProviderCreate(
            name="System One", kind="openai_compatible", base_url=f"{stub_provider.base_url}/v1",
            models=[ProviderModel(name="decision", capabilities={"decision"}, context_window_tokens=8192)],
        ))
        spec = ReviewCreateRequest(
            title="Cancel parallel", objective="Adaptive", main_model=configure(services, stub_provider),
            decision_model=DecisionRole(model_reference={"provider_profile_id": profile.id, "model": "decision"}),
        )
        services.reviews.repository.create("cancel", spec.model_dump(mode="json"), {"status": "running", "stage": "screening"})
        for index in range(8):
            services.reviews.repository.save_candidate("cancel", str(index), Candidate(id=str(index), title=f"Adaptive {index}"))
        services.reviews._schedule("cancel")

        async def wait_for_batch():
            while services.model_runtime.inference_scheduler.snapshot()["active_requests"] != 8:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_for_batch(), timeout=30)
        result = await services.reviews.clear("cancel", services.reviews.get("cancel").revision)
        assert result.deleted_document_ids == []
        assert services.model_runtime.inference_scheduler.snapshot()["active_requests"] == 0
        assert not services.reviews._tasks
        with pytest.raises(NotFoundError):
            services.reviews.get("cancel")
    finally:
        await services.close()


@pytest.mark.anyio
async def test_failed_clear_stays_paused_and_can_be_retried(test_settings, stub_provider, monkeypatch):
    services = create_services(test_settings)
    try:
        spec = ReviewCreateRequest(title="Cleanup retry", objective="Adaptive", main_model=configure(services, stub_provider))
        services.reviews.repository.create("retry-clear", spec.model_dump(mode="json"), {"status": "paused", "stage": "scope"})
        document = services.documents.create_document_from_bytes(
            b"%PDF-1.4\n%%EOF", filename="owned.pdf", title="Owned",
            metadata={"owner_review_id": "retry-clear"},
        )
        delete = services.documents.delete_document

        def fail(_identifier):
            raise OSError("File is busy")

        monkeypatch.setattr(services.documents, "delete_document", fail)
        with pytest.raises(OSError, match="File is busy"):
            await services.reviews.clear("retry-clear", 1)
        assert services.reviews.get("retry-clear").status == "paused"
        with pytest.raises(ConflictError, match="cleanup is incomplete"):
            await command(services, "retry-clear", "resume")
        monkeypatch.setattr(services.documents, "delete_document", delete)
        result = await services.reviews.clear("retry-clear", services.reviews.get("retry-clear").revision)
        assert result.deleted_document_ids == [document.id]
    finally:
        await services.close()


@pytest.mark.anyio
async def test_review_skips_keyless_openalex_and_bounds_web_requests(test_settings, stub_provider, monkeypatch):
    test_settings.web_search_max_requests_per_session = 2
    services = create_services(test_settings)
    queries = []

    async def web(query, limit=10):
        queries.append((query, limit))
        return {"results": [{"title": query, "url": f"https://example.org/{query}"}]}

    monkeypatch.setattr(services.research_search, "search_web", web)
    try:
        spec = ReviewCreateRequest(title="Legacy sources", objective="Adaptive", main_model=configure(services, stub_provider),
                                   openalex_enabled=True)
        scope = ReviewScope(
            questions=["Adaptive?"], inclusion_criteria=[], exclusion_criteria=[],
            queries=[{"provider": "openalex", "query": "adaptive", "pages": 1},
                     *[{"provider": "web", "query": f"paper-{index}", "pages": 1} for index in range(4)]],
            suggested_allowance_tokens=1_000_000, rationale="Use available sources.",
        )
        services.reviews.repository.create("web-limit", spec.model_dump(mode="json"), {
            "status": "running", "stage": "discovery", "scope": scope.model_dump(mode="json"),
        })
        await services.reviews._discovery("web-limit")
        assert queries == [("paper-0", 10), ("paper-1", 10)]
        current = services.reviews.get("web-limit")
        assert current.candidate_count == 2
        assert any("OpenAlex discovery was skipped" in notice for notice in current.notices)
        assert any("DuckDuckGo review request limit" in notice for notice in current.notices)
        await services.reviews._discovery("web-limit")
        assert len(queries) == 2
    finally:
        await services.close()


@pytest.mark.anyio
@pytest.mark.parametrize("api_mode", ["chat_completions", "responses"])
async def test_review_approval_selective_evidence_publication_and_reuse(test_settings, stub_provider, monkeypatch, api_mode):
    services = create_services(test_settings)
    monkeypatch.setattr(stub_provider, "_reply_for", review_reply)
    try:
        reference = configure(services, stub_provider, api_mode=api_mode)
        document_id = await seed_paper(services)
        review = await services.reviews.create(ReviewCreateRequest(
            title="Adaptive evidence review", objective="Find limitations of adaptive methods.",
            main_model=reference, web_enabled=False,
        ))
        proposed = await settle(services, review.id)
        assert proposed.status == "awaiting_approval", proposed.error
        assert proposed.approval == "plan"
        assert proposed.candidate_count == 0
        assert len(stub_provider.requests) == 1
        await command(services, review.id, "approve_plan")
        shortlist = await settle(services, review.id)
        assert shortlist.status == "completed", shortlist.error
        assert shortlist.candidate_count == 1
        assert shortlist.evidence_count == 2
        candidates = services.reviews.candidates(review.id)
        assert candidates.items[0].document_id == document_id
        finished = await settle(services, review.id)
        assert finished.status == "completed", finished.error
        assert finished.evidence_count == 2
        assert finished.output_path
        body = services.workspace.read_file(finished.output_path).content
        assert body.count("# Adaptive evidence review") == 1
        assert "Next checks" in body and "cannot establish novelty" in body
        assert "p.1" in body and "p.2" in body and "Deferred Adaptive methods, p.3" in body
        assert services.summaries.versions(document_id) == []
        count = len(stub_provider.requests)
        spent = finished.used_tokens
        await command(services, review.id, "resume", stage="publication")
        repeated = await settle(services, review.id)
        assert repeated.status == "completed", repeated.error
        assert repeated.output_path == finished.output_path
        assert len(stub_provider.requests) == count
        assert repeated.used_tokens == spent
        assert len(services.workspace.search(tags=[f"literature-review:{review.id}"])) == 1
        assert all(not payload.get("tools") for payload in stub_provider.requests)
        assert all(len([message for message in payload["messages"] if message["role"] == "user"]) == 1 for payload in stub_provider.requests)
        if api_mode == "responses":
            assert all(payload["store"] is False for payload in stub_provider.response_api_requests)
    finally:
        await services.close()


@pytest.mark.anyio
@pytest.mark.parametrize("base_path", ["", "/v1"])
async def test_native_and_generation_share_the_global_lane(test_settings, stub_provider, base_path):
    services = create_services(test_settings)
    try:
        main = configure(services, stub_provider, kind="openai")
        profile = services.providers.create(ProviderCreate(
            name="Native decision", kind="openai_compatible", base_url=f"{stub_provider.base_url}{base_path}",
            models=[ProviderModel(name="decision", capabilities={"decision"}, context_window_tokens=512)],
        ))
        stub_provider.decision_delay_seconds = 0.2
        native = asyncio.create_task(score_decisions(
            services.model_runtime, ModelReference(profile.id, "decision"), "a research article",
            {"paper": {"type": "noul", "instructions": "Is it a paper?"}}, input_tokens=512,
        ))
        async with asyncio.timeout(5):
            while stub_provider.active_inference == 0:
                await asyncio.sleep(0.01)
        text = asyncio.create_task(services.model_runtime.generate(
            services.model_runtime.resolve("chat", model_reference=ModelReference.model_validate(main)), "hello",
        ))
        await asyncio.gather(native, text)
        assert stub_provider.max_active_inference == 1
        assert services.inference_scheduler.snapshot()["active_requests"] == 0
        verified = await services.providers.verify(profile.id, model_name="decision")
        assert verified.reachable and not verified.tool_calling
        with pytest.raises(ProviderRuntimeError):
            services.model_runtime.resolve("chat", model_reference=ModelReference(profile.id, "decision"))
        with pytest.raises(ValidationError):
            await score_decisions(
                services.model_runtime, ModelReference(profile.id, "decision"), "x" * 600,
                {"paper": {"type": "noul", "instructions": "Is it a paper?"}}, input_tokens=8192,
            )
    finally:
        await services.close()


@pytest.mark.anyio
async def test_skipping_references_before_reading_preserves_approval_and_survives_resume(test_settings, stub_provider, monkeypatch):
    services = create_services(test_settings)
    monkeypatch.setattr(stub_provider, "_reply_for", review_reply)
    try:
        await seed_paper(services)
        review = await services.reviews.create(ReviewCreateRequest(
            title="Skip references", objective="Review adaptive methods.",
            main_model=configure(services, stub_provider), web_enabled=False,
        ))
        await settle(services, review.id)
        skipped = await command(services, review.id, "skip_references")
        assert skipped.references_skipped
        assert skipped.approval == "plan"
        await command(services, review.id, "approve_plan")
        completed = await settle(services, review.id)
        assert completed.status == "completed", completed.error
        assert completed.output_path
        assert completed.evidence_count > 0
        assert all(completed.stage_history[stage] == "skipped"
                   for stage in ("references", "reference_screening", "reference_collection"))
        assert not services.reviews.repository.jobs(review.id, "references")
        await command(services, review.id, "resume", stage="references")
        resumed = await settle(services, review.id)
        assert resumed.status == "completed", resumed.error
        assert resumed.references_skipped
        assert resumed.stage_history["references"] == "skipped"
        with pytest.raises(ConflictError):
            await command(services, review.id, "skip_references")
    finally:
        await services.close()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["omitted", "corrected"])
async def test_grouping_automatically_repairs_or_falls_back(test_settings, stub_provider, monkeypatch, failure):
    services = create_services(test_settings)
    group_calls = 0

    def reply(payload):
        nonlocal group_calls
        result = json.loads(review_reply(payload))
        if "groups" in result:
            group_calls += 1
            if failure == "omitted":
                result["groups"] = []
            elif failure == "corrected" and group_calls == 1:
                result["groups"] = []
        return json.dumps(result)

    monkeypatch.setattr(stub_provider, "_reply_for", reply)
    try:
        await seed_paper(services)
        review = await services.reviews.create(ReviewCreateRequest(
            title="Grouping recovery", objective="Review adaptive methods.",
            main_model=configure(services, stub_provider), web_enabled=False,
        ))
        await settle(services, review.id)
        await command(services, review.id, "approve_plan")
        finished = await settle(services, review.id)
        assert finished.status == "completed", finished.error
        jobs = services.reviews.repository.jobs(review.id)
        fallback = [job for job in jobs if job.get("kind") == "FALLBACK"]
        invalid = failure == "omitted"
        assert bool(fallback) == invalid
        assert any("FALLBACK" in notice for notice in finished.notices) == invalid
        if invalid:
            assert group_calls == 3 * len(fallback)
            assert sorted(number for job in fallback for number in job["pages"]) == [1, 2]
            assert all(job["pages"] == sorted(job["pages"]) for job in fallback)
        else:
            assert group_calls == 3
        assert finished.evidence_count > 0
        assert finished.work_used.read_papers == 1
        assert sum(job.get("tokens_spent", 0) for job in jobs) == finished.used_tokens
        before = len(stub_provider.requests)
        await services.reviews.recover()
        await services.reviews.start()
        assert all(
            payload["response_format"]["json_schema"]["name"] not in {"ReadingGroups", "EvidencePacket"}
            for payload in stub_provider.requests[before:]
        )
        if failure == "omitted":
            state = services.reviews.repository.get(review.id).state
            original_budget = state["work_budget"]
            legacy_error = (
                "ValidationError: An isolated worker returned invalid evidence: "
                "Reading groups must contain every admitted page exactly once.. Resume to retry this task."
            )
            services.reviews.jobs.checkpoint(review.id, status="blocked", stage="reading", error=legacy_error)
            await services.close()
            services = create_services(test_settings)
            await services.start()
            recovered = await settle(services, review.id)
            assert recovered.status == "completed", recovered.error
            state = services.reviews.repository.get(review.id).state
            assert state["grouping_recovery_applied"] is True
            assert state["work_budget"] == original_budget
            assert recovered.work_used == finished.work_used
            assert all(
                payload["response_format"]["json_schema"]["name"] not in {"ReadingGroups", "EvidencePacket"}
                for payload in stub_provider.requests[before:]
            )
            services.reviews.jobs.checkpoint(review.id, status="blocked", stage="reading", error=legacy_error)
            await services.reviews.start()
            assert services.reviews.get(review.id).status == "blocked"
    finally:
        await services.close()


@pytest.mark.anyio
async def test_invalid_quotes_retry_only_the_failed_job_and_keep_usage(test_settings, stub_provider, monkeypatch):
    services = create_services(test_settings)
    broken = True

    def reply(payload):
        result = json.loads(review_reply(payload))
        if broken and "claims" in result and result["claims"]:
            result["claims"][0]["quote"] = "This sentence is not in the admitted paper."
        return json.dumps(result)

    monkeypatch.setattr(stub_provider, "_reply_for", reply)
    try:
        await seed_paper(services)
        review = await services.reviews.create(ReviewCreateRequest(
            title="Checked sources", objective="Review adaptive methods.", main_model=configure(services, stub_provider), web_enabled=False,
        ))
        await settle(services, review.id)
        await command(services, review.id, "approve_plan")
        await settle(services, review.id)
        failed = await settle(services, review.id)
        assert failed.status == "blocked" and "verbatim" in failed.error
        assert failed.evidence_count == 0 and not failed.output_path
        invalid_jobs = [
            job for job in services.reviews.repository.jobs(review.id, "reading")
            if job.get("status") == "invalid"
        ]
        assert len(invalid_jobs) == 3
        assert all(job["tokens_spent"] > 0 and job["provenance"] for job in invalid_jobs)
        assert any("automatically repairing (2/2)" in notice for notice in failed.notices)
        calls = len(stub_provider.requests)
        broken = False
        await command(services, review.id, "resume")
        finished = await settle(services, review.id)
        assert finished.status == "completed", finished.error
        assert finished.used_tokens > failed.used_tokens
        new_schemas = [payload["response_format"]["json_schema"]["name"] for payload in stub_provider.requests[calls:]]
        assert "ReviewScope" not in new_schemas and "ScreeningBatch" not in new_schemas and "ReadingPlan" not in new_schemas
        receipts = services.reviews.repository.jobs(review.id, "reading")
        assert any(item.get("previous_tokens", 0) > 0 for item in receipts)
    finally:
        await services.close()


@pytest.mark.anyio
async def test_pause_restart_and_completed_receipts_survive_run_deletion(test_settings, stub_provider, monkeypatch):
    services = create_services(test_settings)
    monkeypatch.setattr(stub_provider, "_reply_for", review_reply)
    try:
        await seed_paper(services)
        review = await services.reviews.create(ReviewCreateRequest(
            title="Restart", objective="Review adaptive methods.", main_model=configure(services, stub_provider), web_enabled=False,
        ))
        await settle(services, review.id)
        await command(services, review.id, "approve_plan")
        await settle(services, review.id)
        prior_calls = len(stub_provider.requests)
        await asyncio.gather(*list(services.runs._tasks.values()))
        for run in services.runs.list():
            services.runs.delete(run.id)
        stub_provider.stream_delay_seconds = 0.3
        document = services.documents.list_documents()[0]
        services.document_ingestion._persist_document_content(
            document, [{"page": page, "text": f"Adaptive revised evidence on page {page}.", "ocr_used": False}
                       for page in (1, 2, 3)], enhancement_model=None, extraction_mode="embedded",
        )
        await command(services, review.id, "resume", stage="reading_plan")
        async with asyncio.timeout(5):
            while services.reviews.get(review.id).active_run_id is None:
                await asyncio.sleep(0.01)
        await command(services, review.id, "pause")
        paused = services.reviews.get(review.id)
        assert paused.status == "paused" and paused.active_run_id is None
        await services.close()
        services = create_services(test_settings)
        await services.start()
        assert services.reviews.get(review.id).status == "paused"
        stub_provider.stream_delay_seconds = 0
        await command(services, review.id, "resume")
        finished = await settle(services, review.id)
        assert finished.approval == "budget"
        await command(services, review.id, "extend_budget",
                      work_budget={"screened_papers": 100, "read_papers": 20, "comparison_passes": 2})
        finished = await settle(services, review.id)
        assert finished.status == "completed", finished.error
        schemas = [payload["response_format"]["json_schema"]["name"] for payload in stub_provider.requests[prior_calls:]]
        assert "ReviewScope" not in schemas and "ScreeningBatch" not in schemas
    finally:
        await services.close()


@pytest.mark.anyio
async def test_source_revision_prevents_stale_publication_and_preserves_track_edits(test_settings, stub_provider, monkeypatch):
    services = create_services(test_settings)
    monkeypatch.setattr(stub_provider, "_reply_for", review_reply)
    try:
        track = services.tracks.create("Adaptive track")
        original = services.tracks.read_section(track.id, "literature")
        services.tracks.write_section(track.id, "literature", original.content + "\nResearcher's existing note.", original.sha256)
        document_id = await seed_paper(services)
        review = await services.reviews.create(ReviewCreateRequest(
            title="Track review", objective="Review adaptive methods.", main_model=configure(services, stub_provider),
            track_id=track.id, web_enabled=False,
        ))
        await settle(services, review.id)
        await command(services, review.id, "approve_plan")
        await settle(services, review.id)
        done = await settle(services, review.id)
        assert done.status == "completed", done.error
        saved = services.tracks.read_section(track.id, "literature").content
        assert "Researcher's existing note." in saved
        assert services.tracks.get(track.id).paper_ids == [document_id]
        assert "summary.md" in saved and "notes.md" in saved and "source.pdf" in saved
        assert saved.index("## Key findings") < saved.index("## Scope and method")
        assert "Motivation, contribution, and novelty" in saved
        assert "Not established from the inspected pages." in saved
        assert "Selective evidence, not a complete paper summary" in saved
        services.document_ingestion._persist_document_content(
            services.documents.get_document(document_id),
            [{"page": 1, "text": "The revised adaptive experiment no longer supports the original result.", "ocr_used": False}],
            enhancement_model=None, extraction_mode="embedded",
        )
        await command(services, review.id, "resume", stage="publication")
        failed = await settle(services, review.id)
        assert failed.status == "blocked" and "unknown source evidence" in failed.error
        assert services.tracks.read_section(track.id, "literature").content == saved
        assert failed.evidence_count == 0
    finally:
        await services.close()


@pytest.mark.anyio
@pytest.mark.parametrize(("decision", "model_switch"), [
    ("extend_analysis", "none"), ("write_report", "current"),
])
async def test_synthesis_reading_plan_caps_followups_and_exposes_saved_outputs(
    test_settings, stub_provider, monkeypatch, decision, model_switch,
):
    from backend.research.reviews.jobs import ReviewPaused

    app = create_app(test_settings)
    services = app.state.services
    monkeypatch.setattr("backend.research.reviews.service._ANALYSIS_PAGE_BATCH", 2)
    reads = []

    def reply(payload):
        schema = (payload.get("response_format") or {}).get("json_schema", {}).get("name")
        if schema is None:
            return "## Method\n\nAdaptive methods need source verification."
        if schema == "AnalysisSources":
            return json.dumps({"sources": [{
                "candidate_id": "bounded-paper", "pages": [1, 2],
                "reason": "Check the method and evaluation against the review question.",
            }]})
        if schema == "EvidencePacket":
            packet = json.loads(next(row["content"] for row in reversed(payload["messages"]) if row["role"] == "user"))
            reads.extend(row["page"] for row in packet["records"])
            result = json.loads(review_reply(payload))
            result["follow_up_pages"] = [3]
            return json.dumps(result)
        return review_reply(payload)

    monkeypatch.setattr(stub_provider, "_reply_for", reply)
    try:
        main = configure(services, stub_provider)
        summary = configure(services, stub_provider, name="fast-summary")
        spec = ReviewCreateRequest(
            title="Bounded analysis", objective="Compare adaptive methods.", main_model=main,
            summary_model=SummaryRole(model_reference=summary), web_enabled=False,
        )
        services.reviews.repository.create("bounded-analysis", spec.model_dump(mode="json"), {
            "status": "running", "stage": "reading",
            "work_budget": {"screened_papers": 100, "read_papers": 20, "comparison_passes": 1},
            "scope": {"questions": ["Compare adaptive methods."], "inclusion_criteria": [],
                      "exclusion_criteria": [], "queries": [{"provider": "library", "query": "adaptive", "pages": 1}],
                      "rationale": "A bounded comparison."},
        })
        document_id = await seed_paper(services)
        candidate = Candidate(id="bounded-paper", title="Adaptive paper", selected=True, document_id=document_id)
        services.reviews.repository.save_candidate("bounded-analysis", candidate.id, candidate)
        revision = services.documents.source_revision(document_id)
        key = f"reading-plan:{candidate.id}:{revision['source_version']}:content-v2"
        services.reviews.repository.save_job("bounded-analysis", key, "reading_plan", {
            "key": key, "status": "completed",
            "pages": [{"page": page, "action": "read", "reason": "Core content."} for page in (1, 2, 3)],
        })
        await services.reviews._reading("bounded-analysis")
        services.reviews.jobs.checkpoint("bounded-analysis", stage="synthesis")
        with pytest.raises(ReviewPaused):
            await services.reviews._synthesis("bounded-analysis")
        paused = services.reviews.get("bounded-analysis")
        assert paused.approval == "analysis"
        assert sorted(set(reads)) == [1, 2]
        progress = services.reviews.analysis_progress("bounded-analysis")
        assert progress.page_limit == progress.admitted_pages == progress.completed_pages == 2
        assert progress.pending_pages == 1
        assert progress.items[-1].page == 3 and not progress.items[-1].admitted
        assert progress.selection_completed == progress.selection_total
        await services.reviews.start()
        await command(services, "bounded-analysis", "resume")
        assert services.reviews.get("bounded-analysis").approval == "analysis"
        assert reads == [1, 2]
        if model_switch != "none":
            await command(services, "bounded-analysis", "pause")
            replacement = configure(services, stub_provider, name="replacement-main")
            before = len(stub_provider.requests)
            current = services.reviews.get("bounded-analysis")
            updated = await services.reviews.control("bounded-analysis", ReviewControlRequest(
                action="resume", expected_revision=current.revision, main_model=replacement,
            ))
            assert updated.approval == "analysis" and updated.status == "awaiting_approval"
            assert services.reviews.analysis_progress("bounded-analysis") == progress
            assert len(stub_provider.requests) == before
            assert services.reviews.jobs.work_usage("bounded-analysis")["comparison_passes"] == 1
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as http:
            response = await http.get("/api/literature-reviews/bounded-analysis/analysis?limit=1&offset=2")
            assert response.status_code == 200
            assert response.json()["items"][0]["page"] == 3
            assert response.json()["total"] == 3
            activity = await http.get("/api/literature-reviews/bounded-analysis/activity?limit=1")
            assert activity.status_code == 200 and activity.json()["next_offset"] == 1
            key = activity.json()["items"][0]["key"]
            detail = await http.get("/api/literature-reviews/bounded-analysis/activity/detail", params={"key": key})
            assert detail.status_code == 200
            assert "result" in detail.json()
            assert "provenance" not in detail.json()
            assert (await http.get("/api/literature-reviews/bounded-analysis/activity/detail",
                                   params={"key": "missing"})).status_code == 404
        await command(services, "bounded-analysis", decision)
        completed = await settle(services, "bounded-analysis")
        assert completed.status == "completed", completed.error
        progress = services.reviews.analysis_progress("bounded-analysis")
        if decision == "extend_analysis":
            assert progress.page_limit == 4 and progress.completed_pages == 3
            assert sorted(set(reads)) == [1, 2, 3]
        else:
            assert progress.completed_pages == 2 and progress.items[-1].status == "skipped"
            assert progress.report_requested
            assert reads == [1, 2]
            assert any("pending synthesis source reads were skipped" in item
                       for item in services.reviews.repository.get("bounded-analysis").state["synthesis"]["limitations"])
        assert services.reviews.jobs.work_usage("bounded-analysis")["comparison_passes"] == 1
        if model_switch != "none":
            assert stub_provider.requests[before:]
            assert all(request["model"] == "replacement-main" for request in stub_provider.requests[before:])
        with pytest.raises(ConflictError):
            await command(services, "bounded-analysis", "extend_analysis")
    finally:
        await services.close()


def test_decision_answers_are_validated_not_treated_as_calibrated_truth():
    question = {"kind": {"type": "choice", "instructions": "Publication kind?", "criteria": {"research": "original", "review": "survey"}}}
    valid = {"answers": {"kind": {"type": "choice", "choice": "research", "probabilities": {"research": 0.8, "review": 0.2}}}}
    assert validate_decisions(valid, question) is valid
    for probabilities in [{"research": float("nan"), "review": 0.2}, {"research": 0.2, "review": 0.8}, {"research": 0.8, "invented": 0.2}]:
        with pytest.raises(ProviderRuntimeError):
            validate_decisions({"answers": {"kind": {**valid["answers"]["kind"], "probabilities": probabilities}}}, question)
    with pytest.raises(ProviderRuntimeError, match="output tokens"):
        validate_decisions({**valid, "usage": {"input_tokens": 100, "output_tokens": 10}}, question)


@pytest.mark.anyio
async def test_deduplication_and_source_cache_cursors_cooldowns(test_settings, stub_provider):
    services = create_services(test_settings)
    calls = []

    async def request(request):
        calls.append(request)
        if request.url.params.get("search") == "limited":
            return httpx.Response(429, headers={"Retry-After": "120"})
        return httpx.Response(200, json={"results": [], "meta": {"next_cursor": "next-token"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(request))
    discovery = ScholarlyDiscovery(test_settings, services.research_search, services.reviews.repository, client=client)
    discovery._openalex_limiter = AsyncRateLimiter(60000)
    try:
        services.reviews.repository.create("dedup", {}, {})
        for raw, provider in [
            ({"title": "Same paper", "arxiv_id": "2401.12345v2", "url": "https://arxiv.org/abs/2401.12345"}, "arxiv"),
            ({"title": "Same paper", "doi": "https://doi.org/10.1234/ABC", "url": "https://arxiv.org/abs/2401.12345"}, "openalex"),
            ({"title": "Same paper", "doi": "10.1234/abc", "url": "https://publisher.example/paper"}, "web"),
            ({"title": "Same paper", "doi": "10.1234/different", "url": "https://publisher.example/different"}, "web"),
        ]:
            candidate = normalize_candidate("dedup", raw, provider, provider)
            services.reviews.repository.save_candidate("dedup", candidate_key(candidate), candidate)
        rows, total = services.reviews.repository.candidates("dedup")
        assert total == 2
        assert any(len(row.discovery) == 3 for row in rows)
        first = await discovery.query("openalex", "adaptive", 0)
        assert calls[-1].url.params["cursor"] == "*"
        await discovery.query("openalex", "adaptive", 0)
        assert len(calls) == 1
        await discovery.query("openalex", "adaptive", 1, cursor=first["meta"]["next_cursor"])
        assert calls[-1].url.params["cursor"] == "next-token"
        with pytest.raises(ValidationError, match="rate-limited"):
            await discovery.query("openalex", "limited", 0)
        count = len(calls)
        with pytest.raises(ValidationError, match="cooling down"):
            await discovery.query("openalex", "another query", 0)
        assert len(calls) == count
        assert services.research_search._limiters["arxiv"] is services.source_downloads._arxiv_limiter
    finally:
        await client.aclose()
        await services.close()


@pytest.mark.anyio
async def test_stale_approval_and_pending_approval_pause_resume(test_settings, stub_provider, monkeypatch):
    services = create_services(test_settings)
    monkeypatch.setattr(stub_provider, "_reply_for", review_reply)
    try:
        review = await services.reviews.create(ReviewCreateRequest(
            title="Approval", objective="Review adaptive systems.", main_model=configure(services, stub_provider),
            web_enabled=False,
        ))
        proposed = await settle(services, review.id)
        await command(services, review.id, "pause")
        paused = services.reviews.get(review.id)
        assert paused.status == "paused"
        with pytest.raises(ConflictError):
            await services.reviews.control(review.id, ReviewControlRequest(
                action="approve_plan", expected_revision=proposed.revision,
            ))
        await command(services, review.id, "resume")
        current = services.reviews.get(review.id)
        assert current.status == "awaiting_approval" and current.approval == "plan"
        assert len(stub_provider.requests) == 1
        with pytest.raises(ConflictError):
            await command(services, review.id, "approve_papers", candidate_ids=["invented"])
    finally:
        await services.close()


@pytest.mark.anyio
async def test_missing_pdf_can_be_explicitly_linked_without_title_guessing(test_settings, stub_provider, monkeypatch):
    services = create_services(test_settings)
    monkeypatch.setattr(stub_provider, "_reply_for", review_reply)
    try:
        review = await services.reviews.create(ReviewCreateRequest(
            title="Local recovery", objective="Review adaptive methods.", main_model=configure(services, stub_provider), web_enabled=False,
        ))
        await settle(services, review.id)
        candidate = normalize_candidate(review.id, {"title": "An unavailable study", "doi": "10.1234/unavailable"}, "web", "reference")
        services.reviews.repository.save_candidate(review.id, candidate_key(candidate), candidate)
        await command(services, review.id, "approve_plan")
        await settle(services, review.id)
        blocked = await settle(services, review.id)
        assert blocked.status == "blocked" and "None of the approved papers" in blocked.error
        document_id = await seed_paper(services, title="An unavailable study")
        await command(services, review.id, "resume")
        assert (await settle(services, review.id)).status == "blocked"
        await command(services, review.id, "link_paper", candidate_ids=[candidate.id], document_id=document_id)
        assert services.reviews.get(review.id).status == "paused"
        await command(services, review.id, "resume")
        completed = await settle(services, review.id)
        assert completed.status == "completed", completed.error
        assert services.reviews.candidates(review.id).items[0].document_id == document_id
        assert services.reviews.paper(review.id, candidate.id).partial_pages == []
    finally:
        await services.close()


@pytest.mark.anyio
@pytest.mark.parametrize("partial_page", [False, True])
async def test_stopping_reading_publishes_only_verified_partial_results(test_settings, stub_provider, monkeypatch, partial_page):
    services = create_services(test_settings)
    reading_calls = 0

    def reply(payload):
        nonlocal reading_calls
        result = json.loads(review_reply(payload))
        if "claims" in result:
            reading_calls += 1
            if partial_page:
                for claim in result["claims"]:
                    claim["quote"] = claim["quote"][:100]
            if reading_calls > 1:
                result["claims"][0]["quote"] = "An invalid later-source quote."
        return json.dumps(result)

    monkeypatch.setattr(stub_provider, "_reply_for", reply)
    try:
        await seed_paper(services, suffix="Adaptive details and limitations. " * 2000 if partial_page else "")
        review = await services.reviews.create(ReviewCreateRequest(
            title="Partial review", objective="Review adaptive studies.", main_model=configure(services, stub_provider), web_enabled=False,
        ))
        await settle(services, review.id)
        await command(services, review.id, "approve_plan")
        await settle(services, review.id)
        candidate_id = services.reviews.candidates(review.id).items[0].id
        blocked = await settle(services, review.id)
        assert blocked.evidence_count == 1 and blocked.stage == "reading"
        await command(services, review.id, "stop_stage")
        done = await settle(services, review.id)
        assert done.status == "completed" and done.stage_history["reading"] == "stopped", done.error
        content = services.workspace.read_file(done.output_path).content
        assert "explicitly partial results" in content and "invalid later-source quote" not in content
        coverage = services.reviews.paper(review.id, candidate_id)
        assert coverage.read_pages == ([] if partial_page else [1])
        assert coverage.partial_pages == ([1] if partial_page else [])
        if partial_page:
            assert "Partially read Adaptive methods, p.1" in content
    finally:
        await services.close()


@pytest.mark.anyio
async def test_publication_recovers_a_note_written_before_index_failure(test_settings, stub_provider, monkeypatch):
    services = create_services(test_settings)
    monkeypatch.setattr(stub_provider, "_reply_for", review_reply)
    original_index = services.workspace._index_file
    interrupt = True

    def index(path, **kwargs):
        nonlocal interrupt
        if interrupt and "literature-review" in (kwargs.get("tags") or []):
            interrupt = False
            raise OSError("Simulated interruption after the note was written.")
        return original_index(path, **kwargs)

    monkeypatch.setattr(services.workspace, "_index_file", index)
    try:
        await seed_paper(services)
        review = await services.reviews.create(ReviewCreateRequest(
            title="Recover publication", objective="Review adaptive studies.", main_model=configure(services, stub_provider), web_enabled=False,
        ))
        await settle(services, review.id)
        await command(services, review.id, "approve_plan")
        await settle(services, review.id)
        failed = await settle(services, review.id)
        assert failed.status == "blocked" and failed.stage == "publication"
        files = list((test_settings.workspace_dir / "knowledge").glob("*.md"))
        assert len(files) == 1
        body = files[0].read_text(encoding="utf-8")
        await command(services, review.id, "resume")
        done = await settle(services, review.id)
        assert done.status == "completed", done.error
        assert list((test_settings.workspace_dir / "knowledge").glob("*.md")) == files
        assert files[0].read_text(encoding="utf-8") == body
        assert len(services.workspace.search(tags=[f"literature-review:{review.id}"])) == 1
    finally:
        await services.close()


@pytest.mark.anyio
async def test_publication_recovers_track_write_before_checkpoint_without_losing_edits(test_settings, stub_provider, monkeypatch):
    services = create_services(test_settings)
    monkeypatch.setattr(stub_provider, "_reply_for", review_reply)
    original_checkpoint = services.reviews.jobs.checkpoint
    interrupt = True

    def checkpoint(review_id, **values):
        nonlocal interrupt
        if values.get("output_path") and interrupt:
            interrupt = False
            raise OSError("Simulated interruption after writing the track.")
        return original_checkpoint(review_id, **values)

    monkeypatch.setattr(services.reviews.jobs, "checkpoint", checkpoint)
    try:
        track = services.tracks.create("Publication recovery")
        await seed_paper(services)
        review = await services.reviews.create(ReviewCreateRequest(
            title="Track recovery", objective="Review adaptive studies.", main_model=configure(services, stub_provider),
            track_id=track.id, web_enabled=False,
        ))
        await settle(services, review.id)
        await command(services, review.id, "approve_plan")
        await settle(services, review.id)
        failed = await settle(services, review.id)
        assert failed.status == "blocked" and failed.stage == "publication"
        assert failed.output_path is None
        published = services.tracks.read_section(track.id, "literature")
        marker = f"<!-- scholarweave-literature-review:{review.id}:"
        assert published.content.count(marker) == 1
        edited = services.tracks.write_section(
            track.id, "literature", published.content + "\nResearcher's later addition.\n", published.sha256,
        )
        request_count = len(stub_provider.requests)
        await command(services, review.id, "resume")
        done = await settle(services, review.id)
        assert done.status == "completed", done.error
        assert done.output_path == edited.path
        assert services.tracks.read_section(track.id, "literature").content == edited.content
        assert len(stub_provider.requests) == request_count
    finally:
        await services.close()


@pytest.mark.anyio
@pytest.mark.parametrize("case", ["uncertain", "malformed"])
async def test_native_failures_use_grouped_main_fallback_and_keep_cost(test_settings, stub_provider, monkeypatch, case):
    services = create_services(test_settings)
    monkeypatch.setattr(stub_provider, "_reply_for", review_reply)
    original = stub_provider.decisions

    def decisions(payload):
        result = original(payload)
        if "kind" not in result["answers"]:
            return result
        if case == "malformed":
            result["answers"].pop("kind")
        elif case == "uncertain":
            answer = result["answers"]["kind"]
            answer["choice"] = "unknown"
            answer["probabilities"] = {key: float(key == "unknown") for key in answer["probabilities"]}
        return result

    monkeypatch.setattr(stub_provider, "decisions", decisions)
    try:
        await seed_paper(services)
        main = configure(services, stub_provider)
        profile = services.providers.create(ProviderCreate(
            name="Small decisions", kind="openai_compatible", base_url=f"{stub_provider.base_url}/v1",
            models=[ProviderModel(name="decision", capabilities={"decision"}, context_window_tokens=4096)],
        ))
        review = await services.reviews.create(ReviewCreateRequest(
            title="Fallback", objective="Review adaptive studies.", main_model=main, web_enabled=False,
            decision_model=DecisionRole(
                model_reference={"provider_profile_id": profile.id, "model": "decision"},
                input_tokens=4096,
            ),
        ))
        await settle(services, review.id)
        await command(services, review.id, "approve_plan")
        waiting = await settle(services, review.id)
        assert waiting.approval != "papers", waiting.error
        assert services.reviews.candidates(review.id).items[0].screening.reason == "Addresses adaptive methods."
        assert len([item for item in stub_provider.decision_requests if "kind" in item["questions"]]) == 1
        native_jobs = [job for job in services.reviews.repository.jobs(review.id, "screening") if "rubric_hash" in job.get("provenance", {})]
        assert len(native_jobs) == 1
        assert native_jobs[0]["usage_estimated"] is (case == "malformed")
        if case == "malformed":
            assert any(job.get("status") == "interrupted" and job["tokens_spent"] > 100 for job in services.reviews.repository.jobs(review.id))
    finally:
        await services.close()


@pytest.mark.anyio
async def test_identity_bridges_merge_unapproved_rows_but_not_conflicting_dois(test_settings):
    services = create_services(test_settings)
    try:
        services.reviews.repository.create("identities", {}, {})
        for raw in [
            {"doi": "10.1234/one", "url": "https://publisher.example/shared"},
            {"doi": "10.1234/two", "url": "https://publisher.example/shared"},
            {"arxiv_id": "2401.12345v3", "url": "https://arxiv.org/abs/2401.12345"},
            {"doi": "10.1234/one", "arxiv_id": "2401.12345", "authors": "Ada Author", "url": "javascript:alert(1)"},
        ]:
            candidate = normalize_candidate("identities", {"title": "Shared title", **raw}, "web", str(raw))
            services.reviews.repository.save_candidate("identities", candidate_key(candidate), candidate)
        items, count = services.reviews.repository.candidates("identities")
        assert count == 2
        merged = next(item for item in items if item.doi == "10.1234/one")
        assert merged.authors == ["Ada Author"] and merged.arxiv_id == "2401.12345"
        assert len(merged.discovery) == 3 and merged.url.startswith("https://")
    finally:
        await services.close()


@pytest.mark.anyio
async def test_rate_limiter_serializes_slow_requests_and_preserves_retry_after():
    clock = [0.0]
    starts, sleeps = [], []
    active, maximum = 0, 0

    async def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay
        await asyncio.sleep(0)

    limiter = AsyncRateLimiter(20, clock=lambda: clock[0], sleep=sleep)

    async def request():
        nonlocal active, maximum
        async with limiter.request():
            starts.append(clock[0])
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(request(), request(), request())
    assert maximum == 1 and starts == [0, 3, 6]
    with pytest.raises(httpx.HTTPStatusError):
        async with limiter.request():
            httpx.Response(429, headers={"Retry-After": "120"}, request=httpx.Request("GET", "https://arxiv.org")).raise_for_status()
    await request()
    assert sleeps[-1] == 120
    for value in ("inf", "nan", "1e300"):
        assert rate_limit_delay(httpx.Headers({"Retry-After": value})) == 86_400


@pytest.mark.anyio
async def test_agent_review_handoff_inherits_web_policy_and_cannot_approve(test_settings, stub_provider, monkeypatch):
    from backend.tests.test_topic_research import embedding_reference

    services = create_services(test_settings)
    arguments = {
        "action": "start", "review_id": None, "title": "Agent review", "objective": "Review adaptive evidence.",
        "expected_revision": None, "offset": 0, "limit": 25,
    }
    stub_provider.call_tool = "literature_review"
    stub_provider.tool_arguments = arguments
    monkeypatch.setattr(stub_provider, "_reply_for", lambda payload: review_reply(payload) if payload.get("response_format") else "The review is proposed; use the review panel to approve it.")
    try:
        reference = configure(services, stub_provider)
        services.reviews.jobs.runtime.settings.default_model_references["embedding"] = embedding_reference(services, stub_provider)
        small = configure(services, stub_provider, name="small-tasks")
        test_settings.default_model_references["small_tasks"] = small
        chat = research_blueprint(reference, web_enabled=False)
        assert "literature-review" not in chat.agents[0].tool_ids
        assert "[Deep Work](/deep-work)" in chat.agents[0].instructions
        blueprint = deep_work_blueprint(reference, web_enabled=False)
        run = services.runs.create(services.compiler.compile(blueprint), "Create a literature review document.", conversation_id=None)
        await asyncio.wait_for(asyncio.shield(services.runs._tasks[run.id]), timeout=30)
        assert services.runs.get(run.id).status == "completed"
        reviews = services.reviews.list()
        assert len(reviews) == 1
        proposed = await settle(services, reviews[0].id)
        assert proposed.approval == "plan", proposed.error
        assert proposed.reading_mode == "targeted"
        assert proposed.embedding_model is not None
        assert proposed.summary_model is not None
        assert proposed.summary_model.model_reference.model_dump() == small
        assert proposed.main_model.model_dump() == reference
        assert services.reviews.repository.get(proposed.id).specification["web_enabled"] is False
        runtime = services.runs._tool_runtime
        context = ScholarWeaveContext(run_id=run.id, tool_runtime=runtime)
        replay = await runtime.invoke("research.literature", arguments, context)
        assert replay["id"] == proposed.id and len(services.reviews.list()) == 1
        services.reviews.jobs.runtime.settings.default_model_references.pop("embedding")
        with pytest.raises(ToolInputError, match="Configure a default embedding model"):
            await runtime.invoke("research.literature", {**arguments, "title": "Another question"}, context)
        assert len(services.reviews.list()) == 1
        with pytest.raises(ToolInputError):
            await runtime.invoke("research.literature", {**arguments, "action": "approve_papers", "review_id": proposed.id, "expected_revision": proposed.revision}, context)
        legacy = research_blueprint(reference, research_mode="review")
        assert "literature-review" not in legacy.agents[0].tool_ids
        worker = next(agent for agent in deep_work_blueprint(reference).agents if agent.id == "worker")
        assert "literature-review" not in worker.tool_ids
    finally:
        await services.close()
