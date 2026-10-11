from __future__ import annotations

import asyncio
import json
from itertools import groupby

import httpx
import pytest

from backend.agents.context import ScholarWeaveContext
from backend.agents.blueprint import SessionPolicySpec
from backend.app import create_app
from backend.bootstrap import create_services
from backend.conversations.turns import deep_work_blueprint, research_blueprint
from backend.core.errors import ConflictError, NotFoundError, ValidationError
from backend.providers.decisions import score_decisions, validate_decisions
from backend.providers.runtime import _compatible_capabilities, _compatible_context_window
from backend.providers.schemas import ProviderCreate, ProviderModel
from backend.providers.types import ModelReference, ProviderRuntimeError
from backend.research.reviews.discovery import normalize_candidate, candidate_key, ScholarlyDiscovery
from backend.research.reviews.schemas import Candidate, DecisionRole, EvidencePacket, ReviewControlRequest, ReviewCreateRequest, ReviewScope, Screening
from backend.research.search import AsyncRateLimiter, rate_limit_delay
from backend.research.sources import SourceDownloadService
from backend.tools.policy import ToolInputError
from scholarweave_tui.client import ScholarWeaveClient


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


@pytest.mark.anyio
@pytest.mark.parametrize("protocol", ["generation", "systemone"])
async def test_content_page_policy_and_selective_summaries_ignore_review_objective(
    test_settings, stub_provider, monkeypatch, protocol,
):
    services = create_services(test_settings)
    original_native = stub_provider.decisions
    decision_packets = []
    texts = [
        "Abstract: Adaptive study motivation and contribution.",
        "Introduction: The problem and assumptions.",
        "Method: A substantive core algorithm.",
        "Results: The primary measured outcome is 42 percent.",
        "Discussion and conclusion: Meaningful limitations.",
        "Appendix: Peripheral implementation listings.",
        "Extra ablation studies: Optional secondary variants.",
        "Trailing diagrams: Figure labels only.",
        "References: Researcher. Shared adaptive reference (2024).",
        "Appendix: The only substantive core proof is reported here.",
        "Unclear scanned page: insufficient outline.",
        "Appendix: Peripheral diagram labels only.",
        "Author-stated limitations: Substantive qualifications to the main result.",
    ]
    deferred = {6, 7, 8, 9, 12}

    def choices(packet, rows):
        assert not {"objective", "questions", "inclusion_criteria", "exclusion_criteria"} & packet.keys()
        decision_packets.append(packet)
        return [{
            "page": row["page"], "action": "defer" if row["page"] in deferred else "read",
            "reference_page": row["page"] == 9, "reason": "Substantive section content or peripheral material.",
        } for row in rows]

    def reply(payload):
        schema = payload["response_format"]["json_schema"]["name"]
        packet = json.loads(next(row["content"] for row in reversed(payload["messages"]) if row["role"] == "user"))
        if schema == "ReadingPlan":
            instructions = payload["messages"][0]["content"]
            assert "not matching a research objective" in instructions
            assert "extra ablation" in instructions and "only substantive core information" in instructions
            return json.dumps({"pages": choices(packet, packet["records"])})
        if schema == "ReadingGroups":
            return json.dumps({"groups": [
                [row["page"] for row in packet["records"] if row["page"] <= 5],
                [row["page"] for row in packet["records"] if row["page"] > 5],
            ]})
        if schema == "ReferenceExtraction":
            return json.dumps({"references": [{
                "title": "Shared adaptive reference", "authors": ["Researcher"], "year": 2024,
                "doi": None, "arxiv_id": None, "quote": row["text"], "page": row["page"],
            } for row in packet["records"]]})
        result = json.loads(review_reply(payload))
        if schema == "EvidencePacket":
            assert "objective" not in packet and "questions" not in packet
            result["follow_up_pages"] = []
        return json.dumps(result)

    def native(payload):
        packet = payload["state"]
        packet = json.loads(packet) if isinstance(packet, str) else packet
        row = choices(packet, [packet["outline"]])[0]
        criteria = payload["questions"]["priority"]["criteria"]
        assert "extra ablations" in criteria["defer"] and "only substantive core information" in criteria["read"]
        result = original_native(payload)
        for name, choice in {"priority": "defer" if row["page"] in deferred else "uncertain" if row["page"] == 11 else "read",
                             "references": "yes" if row["reference_page"] else "no"}.items():
            result["answers"][name]["choice"] = choice
            result["answers"][name]["probabilities"] = {
                key: float(key == choice) for key in result["answers"][name]["probabilities"]
            }
        return result

    monkeypatch.setattr(stub_provider, "_reply_for", reply)
    monkeypatch.setattr(stub_provider, "decisions", native)
    try:
        main = configure(services, stub_provider)
        if protocol == "systemone":
            provider = services.providers.create(ProviderCreate(
                name="Decision", kind="openai_compatible", base_url=f"{stub_provider.base_url}/v1",
                models=[ProviderModel(name="decision", capabilities={"decision"}, context_window_tokens=8192)],
            ))
            reference = {"provider_profile_id": provider.id, "model": "decision"}
        else:
            reference = configure(services, stub_provider, name="fast")
        role = DecisionRole(model_reference=reference, protocol=protocol, input_tokens=8192)
        document_id = await seed_paper(services)
        services.document_ingestion._persist_document_content(
            services.documents.get_document(document_id),
            [{"page": index + 1, "text": text, "ocr_used": False} for index, text in enumerate(texts)],
            enhancement_model=None, extraction_mode="embedded",
        )
        version = services.documents.source_revision(document_id)["source_version"]
        captured = []
        for index, objective in enumerate(["Find adaptive runtime improvements.", "Compare scientific motivation."]):
            review_id = f"content-policy-{index}"
            specification = ReviewCreateRequest(title="Content review", objective=objective, main_model=main,
                                                decision_model=role, web_enabled=False)
            services.reviews.repository.create(review_id, specification.model_dump(mode="json"), {
                "status": "running", "stage": "reading", "scope": None, "allowance_tokens": 10_000_000,
            })
            candidate = Candidate(id=f"paper-{index}", title="Adaptive methods", document_id=document_id, selected=True)
            services.reviews.repository.save_candidate(review_id, candidate.id, candidate)
            old_key = f"reading-plan:{candidate.id}:{version}:references-v1"
            services.reviews.repository.save_job(review_id, old_key, "reading_plan", {
                "key": old_key, "status": "completed", "pages": [{"page": 1, "action": "defer"}],
            })
            before = len(decision_packets)
            await services.reviews._reading_plan(review_id, candidates=[candidate])
            captured.append(decision_packets[before:])
            assert any(packet.get("neighbor_decisions") for packet in captured[-1])
            paper = services.reviews.paper(review_id, candidate.id)
            assert [row.page for row in paper.pages if row.action == "defer"] == sorted(deferred)
            assert paper.pages[8].reference_page
            await services.reviews._reading(review_id, candidates=[candidate])
            paper = services.reviews.paper(review_id, candidate.id)
            assert paper.read_pages == [1, 2, 3, 4, 5, 10, 11, 13]
            assert len(paper.partial_summaries) == 2
            assert paper.selective_summary["sections"][0]["content"].startswith("The combined inspected results")
            ids = {claim["id"] for claim in paper.findings}
            assert set(paper.selective_summary["sections"][0]["evidence_ids"]) == ids
            assert all(claim["quote"] in texts[claim["page"] - 1] for claim in paper.findings)
            assert all(claim["source_version"] == version for claim in paper.findings)
            assert services.summaries.versions(document_id) == []
            await services.reviews._references(review_id)
            assert services.reviews.references(review_id)[0].citations[0]["page"] == 9
            requests = len(stub_provider.requests) + len(stub_provider.decision_requests)
            await services.reviews._reading_plan(review_id, candidates=[candidate])
            await services.reviews._reading(review_id, candidates=[candidate])
            assert requests == len(stub_provider.requests) + len(stub_provider.decision_requests)
            receipts = [job for job in services.reviews.repository.jobs(review_id, "reading") if job.get("kind") == "evidence"]
            changed = {**receipts[0], "limitations": ["Changed source receipt"]}
            services.reviews.repository.save_job(review_id, changed["key"], "reading", changed)
            assert services.reviews.paper(review_id, candidate.id).selective_summary is None
        assert sorted(map(json.dumps, captured[0])) == sorted(map(json.dumps, captured[1]))
        services.document_ingestion._persist_document_content(
            services.documents.get_document(document_id),
            [{"page": 1, "text": "Replacement source extraction.", "ocr_used": False}],
            enhancement_model=None, extraction_mode="embedded",
        )
        replaced = services.reviews.paper("content-policy-0", "paper-0")
        assert replaced.selective_summary is None and replaced.partial_summaries == [] and replaced.findings == []
    finally:
        await services.close()


@pytest.mark.anyio
@pytest.mark.parametrize("invalid", ["quote", "partial_support", "combined_support", "audit_support"])
async def test_selective_summary_rejects_invalid_source_support(test_settings, stub_provider, monkeypatch, invalid):
    services = create_services(test_settings)

    def reply(payload):
        result = json.loads(review_reply(payload))
        packet = json.loads(next(row["content"] for row in reversed(payload["messages"]) if row["role"] == "user"))
        if "claims" in result:
            result["follow_up_pages"] = []
            if invalid == "quote":
                result["claims"][0]["quote"] = "Invented source text."
            elif invalid == "partial_support":
                result["partial_summary"][0]["claim_indexes"] = [len(result["claims"])]
        elif payload["response_format"]["json_schema"]["name"] == "SelectiveSummary":
            if (invalid == "combined_support" and packet["stage"] == "combine") or (
                invalid == "audit_support" and packet["stage"] == "audit"
            ):
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
        if invalid in {"quote", "partial_support"}:
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
@pytest.mark.parametrize("protocol", ["generation", "systemone"])
@pytest.mark.parametrize("match", ["exact", "ambiguous", "relevant", "somewhat", "irrelevant"])
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


def test_isolated_page_gaps_do_not_fill_separated_blocks():
    from backend.research.reviews.service import LiteratureReviewService
    choices = [{"page": page, "action": "read" if page in (1, 2, 3, 5, 10, 11, 12) else "defer"} for page in range(1, 13)]
    assert LiteratureReviewService._isolated_gaps(choices) == [4]

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
        count = 552 if protocol == "generation" else 16
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


@pytest.mark.parametrize("old,new", [
    ("core", "HIGHLY_RELEVANT"), ("relevant", "RELEVANT"),
    ("background", "SOMEWHAT_RELEVANT"), ("uncertain", "SOMEWHAT_RELEVANT"), ("unrelated", "IRRELEVANT"),
])
def test_legacy_screening_ratings_remain_readable(old, new):
    screening = Screening(kind="research", relevance=old, reason="Existing review.", matched_questions=[])
    assert screening.relevance == new


@pytest.mark.anyio
async def test_review_inherits_default_native_decision_model(test_settings, stub_provider, monkeypatch):
    services = create_services(test_settings)
    monkeypatch.setattr(stub_provider, "_reply_for", review_reply)
    try:
        main = configure(services, stub_provider)
        profile = services.providers.create(ProviderCreate(
            name="Default System One", kind="openai_compatible", base_url=f"{stub_provider.base_url}/v1",
            models=[ProviderModel(name="decision", capabilities={"decision"}, context_window_tokens=8192)],
        ))
        test_settings.default_model_references["decision"] = {"provider_profile_id": profile.id, "model": "decision"}
        review = await services.reviews.create(ReviewCreateRequest(title="Defaults", objective="Adaptive", main_model=main))
        assert review.decision_model.protocol == "systemone"
        assert review.decision_model.model_reference.model == "decision"
        assert review.decision_model.input_tokens == 8192
        await settle(services, review.id)
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
async def test_clear_cancels_parallel_native_requests_before_removing_review(test_settings, stub_provider):
    services = create_services(test_settings)
    stub_provider.decision_delay_seconds = 1
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

        await asyncio.wait_for(wait_for_batch(), timeout=5)
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
@pytest.mark.parametrize("existing,expected_requests", [(0, 5), (475, 1), (500, 0), (510, 0)])
async def test_discovery_cap_stops_sources_and_resume(test_settings, stub_provider, monkeypatch, existing, expected_requests):
    services = create_services(test_settings)
    requests, starts = [], []
    clock = [0.0]

    async def sleep(delay):
        clock[0] += delay

    async def search(request):
        requests.append(request)
        starts.append(clock[0])
        assert request.url.host == "export.arxiv.org"
        start = int(request.url.params["start"])
        limit = int(request.url.params["max_results"])
        entries = "".join(
            f"<entry><id>https://arxiv.org/abs/2401.{index:05d}</id><title>Adaptive {index}</title>"
            f"<summary>Adaptive results.</summary></entry>"
            for index in range(start, start + limit)
        )
        return httpx.Response(200, text=f'<feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>')

    client = httpx.AsyncClient(transport=httpx.MockTransport(search))
    await services.research_search.close()
    monkeypatch.setattr(services.research_search, "_client", client)
    monkeypatch.setitem(services.research_search._limiters, "arxiv", AsyncRateLimiter(20, clock=lambda: clock[0], sleep=sleep))
    try:
        spec = ReviewCreateRequest(title="Bounded discovery", objective="Review adaptive methods.",
                                   main_model=configure(services, stub_provider))
        scope = ReviewScope(
            questions=["Adaptive methods?"], inclusion_criteria=[], exclusion_criteria=[],
            queries=[{"provider": "arxiv", "query": "adaptive", "pages": 20},
                     {"provider": "web", "query": "more adaptive papers", "pages": 1}],
            suggested_allowance_tokens=1_000_000, rationale="Bounded search.",
        )
        services.reviews.repository.create("cap", spec.model_dump(mode="json"), {
            "status": "running", "stage": "discovery", "scope": scope.model_dump(mode="json"),
        })
        for index in range(existing):
            candidate = Candidate(id=f"local-{index}", title=f"Local {index}")
            services.reviews.repository.save_candidate("cap", candidate.id, candidate)
        await services.reviews._discovery("cap")
        assert len(requests) == expected_requests
        assert starts == [3.0 * index for index in range(expected_requests)]
        assert services.reviews.get("cap").candidate_count == max(existing, 500)
        assert any("500-paper cap" in notice for notice in services.reviews.get("cap").notices)
        if existing == 475:
            assert requests[0].url.params["max_results"] == "25"
        await services.reviews._discovery("cap")
        assert len(requests) == expected_requests
        services.reviews._advance("cap")
        assert services.reviews.get("cap").stage == "screening"
    finally:
        await client.aclose()
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
@pytest.mark.parametrize("protocol", ["systemone", "generation"])
async def test_independent_roles_run_in_grouped_phases(test_settings, stub_provider, monkeypatch, protocol):
    services = create_services(test_settings)
    monkeypatch.setattr(stub_provider, "_reply_for", review_reply)
    try:
        main = configure(services, stub_provider, kind="openai")
        if protocol == "systemone":
            profile = services.providers.create(ProviderCreate(
                name="Decision endpoint", kind="openai_compatible", base_url=f"{stub_provider.base_url}/v1",
                models=[ProviderModel(name="decision", capabilities={"decision"}, context_window_tokens=8192)],
            ))
            decision = {"provider_profile_id": profile.id, "model": "decision"}
        else:
            decision = configure(services, stub_provider, api_mode="responses", name="decision")
        await seed_paper(services, title="Adaptive study one", suffix="one")
        await seed_paper(services, title="Adaptive study two", suffix="two")
        review = await services.reviews.create(ReviewCreateRequest(
            title="Two roles", objective="Compare adaptive studies.", main_model=main, web_enabled=False,
            decision_model=DecisionRole(model_reference=decision, protocol=protocol, input_tokens=8192),
        ))
        await settle(services, review.id)
        await command(services, review.id, "approve_plan")
        shortlist = await settle(services, review.id)
        assert shortlist.status == "completed", shortlist.error
        finished = await settle(services, review.id)
        assert finished.status == "completed", finished.error
        assert [name for name, _ in groupby(stub_provider.request_order)] == ["main", "decision", "main"]
        assert stub_provider.max_active_inference == 1
        if protocol == "systemone":
            assert len(stub_provider.decision_requests) == 8
            assert all("decision" != payload["model"] for payload in stub_provider.requests)
        else:
            assert stub_provider.response_api_requests
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
async def test_budget_checkpoint_and_stop_without_evidence(test_settings, stub_provider, monkeypatch):
    services = create_services(test_settings)
    monkeypatch.setattr(stub_provider, "_reply_for", review_reply)
    try:
        await seed_paper(services)
        review = await services.reviews.create(ReviewCreateRequest(
            title="Budget", objective="Review adaptive methods.", main_model=configure(services, stub_provider), web_enabled=False,
        ))
        proposed = await settle(services, review.id)
        await seed_paper(services, title="Adaptive second", suffix="second")
        await command(services, review.id, "approve_plan",
                      work_budget={"screened_papers": 1, "read_papers": 20, "comparison_passes": 1})
        waiting = await settle(services, review.id)
        assert waiting.approval == "budget", waiting.error
        assert waiting.work_used.screened_papers == 1
        await command(services, review.id, "pause")
        await command(services, review.id, "resume")
        assert services.reviews.get(review.id).approval == "budget"
        await command(services, review.id, "stop_stage")
        shortlist = await settle(services, review.id)
        assert shortlist.approval == "budget"
        assert shortlist.output_path is None
        await command(services, review.id, "extend_budget",
                      work_budget={"screened_papers": 2, "read_papers": 20, "comparison_passes": 1})
        assert (await settle(services, review.id)).status == "completed"
    finally:
        await services.close()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["omitted", "duplicated", "empty", "out_of_range", "schema", "corrected", "valid"])
async def test_grouping_automatically_repairs_or_falls_back(test_settings, stub_provider, monkeypatch, failure):
    services = create_services(test_settings)
    group_calls = 0

    def reply(payload):
        nonlocal group_calls
        result = json.loads(review_reply(payload))
        if "groups" in result:
            group_calls += 1
            pages = [number for group in result["groups"] for number in group]
            if failure == "omitted":
                result["groups"] = []
            elif failure == "duplicated":
                result["groups"] = [pages + pages]
            elif failure == "empty":
                result["groups"] = [pages, []]
            elif failure == "out_of_range":
                result["groups"] = [pages + [999]]
            elif failure == "schema":
                result["groups"] = "not groups"
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
        invalid = failure not in {"corrected", "valid"}
        assert bool(fallback) == invalid
        assert any("FALLBACK" in notice for notice in finished.notices) == invalid
        if invalid:
            assert group_calls == 3 * len(fallback)
            assert sorted(number for job in fallback for number in job["pages"]) == [1, 2]
            assert all(job["pages"] == sorted(job["pages"]) for job in fallback)
        elif failure == "corrected":
            assert group_calls == 3
        else:
            assert group_calls == 2
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
        if failure == "empty":
            services.reviews.jobs.checkpoint(
                review.id, status="paused", stage="reading",
                error="ValidationError: An isolated worker returned invalid evidence: "
                "Reading groups must contain every admitted page exactly once.. Resume to retry this task.",
            )
            await services.close()
            services = create_services(test_settings)
            await services.start()
            assert services.reviews.get(review.id).status == "paused"
    finally:
        await services.close()


@pytest.mark.anyio
async def test_pause_during_grouping_repair_does_not_fallback(test_settings, stub_provider, monkeypatch):
    services = create_services(test_settings)
    group_calls = 0

    def reply(payload):
        nonlocal group_calls
        result = json.loads(review_reply(payload))
        if "groups" in result:
            group_calls += 1
            result["groups"] = []
            stub_provider.stream_delay_seconds = 0.5
        return json.dumps(result)

    monkeypatch.setattr(stub_provider, "_reply_for", reply)
    try:
        await seed_paper(services)
        review = await services.reviews.create(ReviewCreateRequest(
            title="Pause repair", objective="Review adaptive methods.",
            main_model=configure(services, stub_provider), web_enabled=False,
        ))
        await settle(services, review.id)
        await command(services, review.id, "approve_plan")
        async with asyncio.timeout(20):
            while group_calls < 2:
                await asyncio.sleep(0.02)
        await command(services, review.id, "pause")
        assert services.reviews.get(review.id).status == "paused"
        assert not any(job.get("kind") == "FALLBACK" for job in services.reviews.repository.jobs(review.id))
        calls = len(stub_provider.requests)
        await services.close()
        services = create_services(test_settings)
        await services.start()
        assert services.reviews.get(review.id).status == "paused"
        assert len(stub_provider.requests) == calls
        stub_provider.stream_delay_seconds = 0
        monkeypatch.setattr(stub_provider, "_reply_for", review_reply)
        await command(services, review.id, "resume")
        finished = await settle(services, review.id)
        assert finished.status == "completed", finished.error
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
async def test_review_uses_saved_track_context_and_seeds_linked_papers(test_settings, stub_provider, monkeypatch):
    services = create_services(test_settings)

    def reply(payload):
        result = json.loads(review_reply(payload))
        if "queries" in result:
            result["queries"] = [{"provider": "library", "query": "unmatchedsearchword", "pages": 1}]
        return json.dumps(result)

    monkeypatch.setattr(stub_provider, "_reply_for", reply)
    try:
        track = services.tracks.create("Adaptive research")
        problem = services.tracks.read_section(track.id, "problem")
        problem = services.tracks.write_section(
            track.id, "problem", "# Problem\n\nCompare adaptive retrieval under a 50 ms latency limit.", problem.sha256,
        )
        exploration = services.tracks.write_section(
            track.id, "exploration/prior-attempts", "# Prior attempts\n\nAdaptive reranking was already rejected for latency.", None,
        )
        document_id = await seed_paper(services)
        services.tracks.add_paper(track.id, document_id)
        folder = services.workspace.paper_folder(document_id)
        summary = "# Existing summary\n\n" + "Earlier adaptive analysis with source citations [p.1]. " * 5
        services.workspace.write_file(f"{folder}/summary.md", summary)
        conversation = services.conversations.create(
            title="Attached research", kind="agent", model_reference={}, session_policy=SessionPolicySpec(),
        )
        services.tracks.link_conversation(track.id, conversation.id)
        review = await services.reviews.create(ReviewCreateRequest(
            title="Track-aware review", objective="Compare adaptive approaches.",
            main_model=configure(services, stub_provider), conversation_id=conversation.id, web_enabled=False,
        ))
        assert review.track_id == track.id
        proposed = await settle(services, review.id)
        assert proposed.approval == "plan", proposed.error
        context = services.reviews.repository.job(review.id, "track-context")
        assert context is not None
        sources = {item["path"]: item for item in context["context"]["sources"]}
        assert sources[problem.path]["sha256"] == problem.sha256
        assert "50 ms" in sources[problem.path]["excerpt"]
        assert "already rejected" in sources[exploration.path]["excerpt"]
        assert sources[f"{folder}/summary.md"]["excerpt"].startswith("# Existing summary")
        assert summary.startswith(sources[f"{folder}/summary.md"]["excerpt"])
        first_prompt = json.loads(next(
            message["content"] for message in reversed(stub_provider.requests[0]["messages"])
            if message["role"] == "user"
        ))
        assert first_prompt["track_context"]["sources"] == context["context"]["sources"]
        assert first_prompt["track_context"]["evidence_status"] == "Unverified prior work; verify claims against current paper sources."
        assert not services.reviews.repository.jobs(review.id, "reading")
        services.tracks.write_section(track.id, "problem", "# Changed after scope\n\nA different question.", problem.sha256)
        await command(services, review.id, "approve_plan")
        shortlist = await settle(services, review.id)
        assert shortlist.status == "completed", shortlist.error
        candidates = services.reviews.candidates(review.id).items
        assert len(candidates) == 1 and candidates[0].document_id == document_id
        assert candidates[0].selected
        assert any("track" in trail for trail in candidates[0].discovery)
        done = await settle(services, review.id)
        assert done.status == "completed", done.error
        assert services.workspace.read_file(f"{folder}/summary.md").content == summary
        assert services.summaries.versions(document_id) == []
        assert services.reviews.repository.job(review.id, "track-context") == context
        assert context in services.reviews.export(review.id)["records"]
    finally:
        await services.close()


@pytest.mark.anyio
async def test_track_context_is_bounded_and_scope_replays_the_original_snapshot(test_settings, stub_provider, monkeypatch):
    services = create_services(test_settings)
    monkeypatch.setattr(stub_provider, "_reply_for", review_reply)
    try:
        track = services.tracks.create("Large notebook")
        problem = services.tracks.read_section(track.id, "problem")
        services.tracks.write_section(track.id, "problem", "# Constraints\n\n" + "Adaptive constraints. " * 2000, problem.sha256)
        for index in range(6):
            services.tracks.write_section(
                track.id, f"exploration/prior-{index}", "# Prior work\n\n" + "Adaptive prior evidence. " * 1000, None,
            )
        reference = configure(services, stub_provider)
        review = await services.reviews.create(ReviewCreateRequest(
            title="Bounded context", objective="Compare adaptive methods.", main_model=reference,
            track_id=track.id, web_enabled=False,
        ))
        proposed = await settle(services, review.id)
        assert proposed.approval == "plan", proposed.error
        context = services.reviews.repository.job(review.id, "track-context")
        assert context["context"]["omitted_source_count"] or any(source["truncated"] for source in context["context"]["sources"])
        assert any("Notebook context was limited" in notice for notice in proposed.notices)
        compiled, budget, _ = services.reviews.jobs.prepare(review.main_model, "literature-scope", ReviewScope)
        packet = next(item["content"] for item in stub_provider.requests[0]["messages"] if item["role"] == "user")
        assert budget.count(compiled.entry_agent.instructions, packet) <= budget.limit
        assert "source.pdf" not in packet
        services.tracks.write_section(track.id, "exploration/later-note", "Do not silently alter the approved scope.", None)
        calls = len(stub_provider.requests)
        await services.reviews._scope(review.id)
        assert len(stub_provider.requests) == calls
        assert services.reviews.repository.job(review.id, "track-context") == context
    finally:
        await services.close()


@pytest.mark.anyio
async def test_review_http_and_terminal_controls(test_settings, stub_provider, monkeypatch):
    app = create_app(test_settings)
    services = app.state.services
    monkeypatch.setattr(stub_provider, "_reply_for", review_reply)
    client = ScholarWeaveClient(transport=httpx.ASGITransport(app=app))
    try:
        reference = configure(services, stub_provider)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as http:
            created = await http.post("/api/literature-reviews", json={
                "title": "API review", "objective": "Review adaptive methods.", "main_model": reference, "web_enabled": False,
            })
            assert created.status_code == 202, created.text
            review = await settle(services, created.json()["id"])
            listed = await client.reviews()
            assert listed[0].id == review.id
            paused = await client.control_review(review.id, ReviewControlRequest(action="pause", expected_revision=review.revision))
            assert paused.status == "paused"
            stale = await http.post(f"/api/literature-reviews/{review.id}/control", json={
                "action": "approve_plan", "expected_revision": review.revision,
            })
            assert stale.status_code == 409
            exported = await http.get(f"/api/literature-reviews/{review.id}/export")
            assert exported.status_code == 200 and exported.json()["schema_version"] == 1
            assert exported.headers["content-disposition"].endswith('.json"')
    finally:
        await client.close()
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


def test_discovered_native_context_is_not_dropped_as_a_small_chat_window():
    metadata = {"architecture": {"output_modalities": ["decisions"]}, "status": {"args": ["--ctx-size", "512"]}}
    assert _compatible_capabilities(metadata) == {"decision"}
    assert _compatible_context_window(metadata) == 512
    metadata["architecture"]["output_modalities"].append("text")
    assert _compatible_context_window(metadata) is None


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
async def test_source_packets_bound_context_without_losing_spans(test_settings, stub_provider):
    services = create_services(test_settings)
    try:
        reference = configure(services, stub_provider)
        from backend.agents.blueprint import ModelReferenceSpec
        prepared = services.reviews.jobs.prepare(ModelReferenceSpec.model_validate(reference), "literature-evidence", EvidencePacket)
        text = "Adaptive evidence with numerical qualifiers and limitations. " * 1000
        packets = services.reviews._source_packets([{"page": 1, "text": text}], {"objective": "Find limitations."}, prepared)
        assert len(packets) > 1
        spans = [span for packet in packets for span in packet["records"]]
        assert "".join(span["text"] for span in spans) == text
        assert spans[0]["start"] == 0 and spans[-1]["end"] == len(text)
        assert all(a["end"] == b["start"] for a, b in zip(spans, spans[1:]))
        assert all(prepared[1].count(prepared[0].entry_agent.instructions, json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"))) <= prepared[1].limit for packet in packets)
        assert not stub_provider.requests
    finally:
        await services.close()


@pytest.mark.anyio
async def test_resume_screening_from_pending_paper_approval(test_settings, stub_provider, monkeypatch):
    services = create_services(test_settings)
    original_decisions = stub_provider.decisions

    def decisions(payload):
        result = original_decisions(payload)
        rating = "IRRELEVANT" if payload["state"]["title"] == "Unrelated paper" else "HIGHLY_RELEVANT"
        answer = result["answers"]["relevance"]
        answer["choice"] = rating
        answer["probabilities"] = {key: float(key == rating) for key in answer["probabilities"]}
        return result

    monkeypatch.setattr(stub_provider, "decisions", decisions)
    try:
        main = configure(services, stub_provider)
        profile = services.providers.create(ProviderCreate(
            name="Decision", kind="openai_compatible", base_url=stub_provider.base_url,
            models=[ProviderModel(name="decision", capabilities={"decision"}, context_window_tokens=8192)],
        ))
        specification = ReviewCreateRequest(
            title="Recover screening", objective="Compare adaptive methods.", main_model=main,
            decision_model=DecisionRole(
                model_reference={"provider_profile_id": profile.id, "model": "decision"}, input_tokens=8192,
            ), web_enabled=False,
        )
        services.reviews.repository.create("recover-screening", specification.model_dump(mode="json"), {
            "status": "awaiting_approval", "stage": "reading_plan", "approval": "papers",
            "scope": None, "allowance_tokens": 1_000_000, "approval_revision": 1,
            "stage_history": {"scope": "completed", "discovery": "completed", "screening": "stopped"},
        })
        for index, title in enumerate(["Adaptive results", "Unrelated paper"]):
            candidate = Candidate(id=f"recover-{index}", title=title, abstract="Research results.")
            services.reviews.repository.save_candidate("recover-screening", candidate.id, candidate)
        await command(services, "recover-screening", "resume", stage="screening")
        waiting = await settle(services, "recover-screening")
        assert waiting.status == "blocked" and waiting.approval is None
        assert waiting.screened_count == 2 and waiting.excluded_count == 1
        assert waiting.stage_history["screening"] == "completed"
        assert services.reviews.repository.get(waiting.id).state.get("approval_revision")
        assert services.reviews.candidates(waiting.id).total == 1
        assert not stub_provider.requests
        await command(services, waiting.id, "resume")
        assert (await settle(services, waiting.id)).status == "blocked"
        assert services.reviews.get(waiting.id).selected_count == 1
    finally:
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
async def test_metadata_discovery_never_expands_a_reference_frontier(test_settings, stub_provider, monkeypatch):
    test_settings.openalex_api_key = "test-key"
    services = create_services(test_settings)
    requests = []
    works = {
        f"W{index}": {
            "id": f"https://openalex.org/W{index}", "title": f"Adaptive paper {index}", "type": "review",
            "referenced_works": [f"https://openalex.org/{ref}" for ref in refs],
        } for index, refs in [(1, ["W2", "W3"]), (2, ["W4"]), (3, ["W4"]), (4, ["W1"])]
    }

    async def metadata(request):
        requests.append(request)
        identifiers = request.url.params.get("filter", "openalex:W1").removeprefix("openalex:").split("|")
        return httpx.Response(200, json={"results": [works[key] for key in identifiers], "meta": {}})

    def reply(payload):
        result = json.loads(review_reply(payload))
        if "queries" in result:
            result["queries"] = [{"provider": "openalex", "query": "adaptive", "pages": 1}]
        return json.dumps(result)

    client = httpx.AsyncClient(transport=httpx.MockTransport(metadata))
    await services.reviews.discovery.close()
    services.reviews.discovery = ScholarlyDiscovery(test_settings, services.research_search, services.reviews.repository, client=client)
    services.reviews.discovery._openalex_limiter = AsyncRateLimiter(60000)
    monkeypatch.setattr(stub_provider, "_reply_for", reply)
    try:
        review = await services.reviews.create(ReviewCreateRequest(
            title="Citation frontier", objective="Find adaptive evidence.", main_model=configure(services, stub_provider),
            openalex_enabled=True,
        ))
        await settle(services, review.id)
        await command(services, review.id, "approve_plan")
        waiting = await settle(services, review.id)
        assert waiting.status == "blocked", waiting.error
        assert waiting.candidate_count == 1 and waiting.selected_count == 1
        assert services.documents.list_documents() == []
        assert len(requests) == 1
        jobs = services.reviews.repository.jobs(review.id, "discovery")
        assert not any(job["key"].startswith("reference-frontier:") for job in jobs)
    finally:
        await client.aclose()
        await services.close()


@pytest.mark.anyio
async def test_remote_acquisition_and_later_papers_are_automatically_screened_and_read(test_settings, stub_provider, monkeypatch):
    services = create_services(test_settings)
    requests = []

    async def download(request):
        requests.append(str(request.url))
        return httpx.Response(200, content=b"%PDF-1.7\n" + request.url.path.encode(), headers={"Content-Type": "application/pdf"})

    async def allow_public(_url):
        return None

    async def options(_identifier):
        return {"recommended_mode": "embedded", "ocr_available": True}

    async def ingest(identifier, *, enhance_with_llm=True, **_kwargs):
        assert enhance_with_llm is False
        services.document_ingestion._persist_document_content(
            services.documents.get_document(identifier),
            [{"page": page, "text": f"Adaptive result and qualification on page {page}.", "ocr_used": False} for page in (1, 2, 3)],
            enhancement_model=None, extraction_mode="embedded",
        )
        return services.documents.get_document(identifier)

    client = httpx.AsyncClient(transport=httpx.MockTransport(download))
    downloads = SourceDownloadService(test_settings, services.documents, services.workspace, client=client)
    services.reviews.downloads = downloads
    monkeypatch.setattr(downloads, "_validate_public_url", allow_public)
    monkeypatch.setattr(services.documents, "ingestion_options", options)
    monkeypatch.setattr(services.documents, "ingest_document", ingest)
    monkeypatch.setattr(stub_provider, "_reply_for", review_reply)
    try:
        review = await services.reviews.create(ReviewCreateRequest(
            title="Acquisition gate", objective="Compare adaptive evidence.", main_model=configure(services, stub_provider),
            web_enabled=False,
        ))
        await settle(services, review.id)
        first = normalize_candidate(review.id, {"title": "Same title", "url": "https://papers.example/first.pdf"}, "web", "supplied reference")
        services.reviews.repository.save_candidate(review.id, candidate_key(first), first)
        await command(services, review.id, "approve_plan")
        completed = await settle(services, review.id)
        assert completed.status == "completed", completed.error
        assert requests == ["https://papers.example/first.pdf"]
        later = normalize_candidate(review.id, {"title": "Same title", "url": "https://papers.example/later.pdf"}, "web", "later reference")
        services.reviews.repository.save_candidate(review.id, candidate_key(later), later)
        await command(services, review.id, "resume", stage="screening")
        waiting = await settle(services, review.id)
        assert waiting.approval == "budget" and waiting.selected_count == 2
        await command(services, review.id, "extend_budget",
                      work_budget={"screened_papers": 100, "read_papers": 20, "comparison_passes": 2})
        with pytest.raises(ConflictError):
            await services.reviews._prepare_paper(review.id, later)
        done = await settle(services, review.id)
        assert done.status == "completed", done.error
        assert requests == ["https://papers.example/first.pdf", "https://papers.example/later.pdf"]
        assert len(services.documents.list_documents()) == 2
    finally:
        await client.aclose()
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
@pytest.mark.parametrize("case", ["oversized", "uncertain", "malformed"])
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
                input_tokens=128 if case == "oversized" else 4096,
            ),
        ))
        await settle(services, review.id)
        await command(services, review.id, "approve_plan")
        waiting = await settle(services, review.id)
        assert waiting.approval != "papers", waiting.error
        assert services.reviews.candidates(review.id).items[0].screening.reason == "Addresses adaptive methods."
        assert len([item for item in stub_provider.decision_requests if "kind" in item["questions"]]) == (0 if case == "oversized" else 1)
        native_jobs = [job for job in services.reviews.repository.jobs(review.id, "screening") if "rubric_hash" in job.get("provenance", {})]
        assert len(native_jobs) == (0 if case == "oversized" else 1)
        if native_jobs:
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


@pytest.mark.parametrize("capability", ["chat", "tools", "vision"])
def test_decision_capability_does_not_relax_generative_context_minimum(capability):
    with pytest.raises(ValueError, match="at least 4096"):
        ProviderModel(name="mixed", capabilities={"decision", capability}, context_window_tokens=512)


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
        assert services.reviews.repository.get(proposed.id).specification["web_enabled"] is False
        runtime = services.runs._tool_runtime
        context = ScholarWeaveContext(run_id=run.id, tool_runtime=runtime)
        replay = await runtime.invoke("research.literature", arguments, context)
        assert replay["id"] == proposed.id and len(services.reviews.list()) == 1
        with pytest.raises(ToolInputError):
            await runtime.invoke("research.literature", {**arguments, "action": "approve_papers", "review_id": proposed.id, "expected_revision": proposed.revision}, context)
        legacy = research_blueprint(reference, research_mode="review")
        assert "literature-review" not in legacy.agents[0].tool_ids
        worker = next(agent for agent in deep_work_blueprint(reference).agents if agent.id == "worker")
        assert "literature-review" not in worker.tool_ids
    finally:
        await services.close()


@pytest.mark.anyio
async def test_terminal_review_picker_pauses_and_returns_to_approval(test_settings, stub_provider, monkeypatch):
    pytest.importorskip("textual")
    from textual.widgets import OptionList
    from scholarweave_tui.app import ScholarWeaveApp
    from scholarweave_tui.preferences import Preferences
    from scholarweave_tui.widgets import ChoicePicker

    backend = create_app(test_settings)
    services = backend.state.services
    monkeypatch.setattr(stub_provider, "_reply_for", review_reply)
    client = ScholarWeaveClient(transport=httpx.ASGITransport(app=backend))
    try:
        reference = configure(services, stub_provider)
        review = await services.reviews.create(ReviewCreateRequest(
            title="Terminal review", objective="Review adaptive studies.", main_model=reference, web_enabled=False,
        ))
        await settle(services, review.id)
        app = ScholarWeaveApp(client, Preferences(test_settings.data_dir / "tui-preferences.json"))
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()

            async def picker(count):
                async with asyncio.timeout(10):
                    while not isinstance(app.screen, ChoicePicker) or app.screen.query_one(OptionList).option_count != count:
                        await pilot.pause()
                await pilot.pause()

            app.choose_literature_review()
            await picker(2)
            await pilot.press("down", "enter")
            await picker(3)
            await pilot.press("down", "enter")
            await app.workers.wait_for_complete()
            assert services.reviews.get(review.id).status == "paused"
            app.choose_literature_review()
            await picker(2)
            await pilot.press("down", "enter")
            await picker(3)
            await pilot.press("down", "down", "enter")
            await app.workers.wait_for_complete()
            assert services.reviews.get(review.id).approval == "plan"
            assert services.reviews.get(review.id).status == "awaiting_approval"
    finally:
        await client.close()
        await services.close()
