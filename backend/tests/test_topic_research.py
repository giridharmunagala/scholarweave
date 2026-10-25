from __future__ import annotations

import json

import httpx
import pytest
from openai import OpenAIError
from sqlalchemy import select

from backend.bootstrap import create_services
from backend.core.errors import ConflictError, ValidationError
from backend.research.reviews.jobs import ReviewPaused
from backend.providers.schemas import ProviderCreate, ProviderModel
from backend.persistence.files import StorageError
from backend.research.reviews.models import ReviewJob
from backend.research.reviews.schemas import Candidate, EmbeddingRole, ReviewCreateRequest, ReviewScope, SummaryRole
from backend.research.reviews.topics import cluster_embeddings
from backend.tests.test_literature_reviews import command, configure, review_reply, seed_paper, settle
from backend.workspace.models import WorkspaceEntry


@pytest.mark.anyio
async def test_receipt_save_reuses_verified_content_and_rejects_corruption(test_settings, monkeypatch):
    services = create_services(test_settings)
    try:
        repository = services.reviews.repository
        repository.create("receipt-test", {"embedding_model": {"model": "embed"}}, {
            "workspace_path": "research/unlinked/receipt-test",
        })
        content = {"key": "test-job", "status": "completed", "result": "saved evidence"}
        repository.save_job("receipt-test", "test-job", "reading", content)
        receipts = repository.workspace_receipts("receipt-test")
        assert len(receipts) == 1
        path = next(iter(receipts))
        storage = repository.storage
        target = storage.resolve_path(test_settings.workspace_dir, path)
        modified = target.stat().st_mtime_ns

        def unexpected_write(*args, **kwargs):
            pytest.fail("Existing receipts must not be rewritten")

        monkeypatch.setattr(storage, "write_bytes_atomic", unexpected_write)
        repository.save_job("receipt-test", "test-job", "reading", content)
        assert repository.job("receipt-test", "test-job") == content
        assert target.stat().st_mtime_ns == modified
        storage.write_bytes(test_settings.workspace_dir, path, b"externally changed")
        with pytest.raises(ValidationError, match="Research receipt changed on disk"):
            repository.save_job("receipt-test", "test-job", "reading", content)
        assert target.read_bytes() == b"externally changed"
        assert repository.workspace_receipts("receipt-test") == receipts
    finally:
        await services.close()


def test_embedding_groups_use_similarity_and_validate_vectors():
    assert cluster_embeddings([[1, 0], [0.99, 0.01], [0, 1], [0.01, 0.99]]) == [[0, 1], [2, 3]]
    assert cluster_embeddings([[1, 0]]) == [[0]]
    for vectors in ([[0, 0]], [[float("nan"), 0]], [[1, 0], [1]]):
        with pytest.raises(ValidationError):
            cluster_embeddings(vectors)


def embedding_reference(services, stub_provider):
    provider = services.providers.create(ProviderCreate(
        name="Topic embeddings", kind="openai_compatible", api_key="stub-key",
        base_url=f"{stub_provider.base_url}/v1",
        models=[ProviderModel(name="embed", capabilities={"embedding"})],
    ))
    return {"provider_profile_id": provider.id, "model": "embed"}


@pytest.mark.anyio
@pytest.mark.parametrize("failure_stage", ["collection", "summary"])
async def test_unreadable_source_is_skipped_reported_and_recoverable(
    test_settings, stub_provider, monkeypatch, failure_stage,
):
    services = create_services(test_settings)
    monkeypatch.setattr(stub_provider, "_reply_for", topic_reply)
    try:
        main = configure(services, stub_provider)
        track = services.tracks.create("Source coverage")
        good_id = await seed_paper(services)
        broken = services.documents.create_document_from_bytes(
            b"%PDF-1.4\nbroken\n%%EOF", filename="broken.pdf", title="Unreadable original",
        )
        if failure_stage == "summary":
            services.document_ingestion._persist_document_content(
                broken, [{"page": 1, "text": "Readable until the source is removed.", "ocr_used": False}],
                enhancement_model=None, extraction_mode="embedded",
            )
        spec = ReviewCreateRequest(
            title="Source coverage", objective="Compare methods.", main_model=main,
            summary_model=SummaryRole(model_reference=main), track_id=track.id,
            embedding_model=EmbeddingRole(model_reference=embedding_reference(services, stub_provider)),
        )
        review_id = "source-coverage"
        root = "research/unlinked/source-coverage"
        services.reviews.repository.create(review_id, spec.model_dump(mode="json"), {
            "status": "running", "stage": "reading_plan", "workspace_path": root,
            "selected_topic_ids": [],
            "work_budget": {"screened_papers": 100, "read_papers": 100, "comparison_passes": 10},
        })
        for candidate in [
            Candidate(id="broken", title="Unreadable original", document_id=broken.id, selected=True),
            Candidate(id="good", title="Readable original", document_id=good_id, selected=True),
        ]:
            services.reviews.repository.save_candidate(review_id, candidate.id, candidate)
        await services.reviews._reading_plan(review_id)
        if failure_stage == "summary":
            original_paper = services.reviews._paper

            def paper(candidate):
                if candidate.id == "broken":
                    raise FileNotFoundError("The extracted source is no longer available.")
                return original_paper(candidate)

            monkeypatch.setattr(services.reviews, "_paper", paper)
        else:
            assert services.reviews.repository.candidate(review_id, "broken").error
            assert set(services.reviews.topic_research.unavailable_sources(review_id)) == {"broken"}
            skipped = services.reviews.paper(review_id, "broken")
            assert skipped.error and not skipped.prepared and skipped.selective_summary is None
            progress = services.reviews.paper_progress(review_id, summaries_only=False)
            assert any(row.candidate_id == "broken" and row.error and not row.prepared for row in progress.items)
        calls = 0
        original_prepare = services.reviews._prepare_paper

        async def prepare(*args, **kwargs):
            nonlocal calls
            calls += 1
            return await original_prepare(*args, **kwargs)

        monkeypatch.setattr(services.reviews, "_prepare_paper", prepare)
        await services.reviews._reading(review_id)
        await services.reviews._reading(review_id)
        assert calls == 0  # Reading/resume must not repeatedly retry a known unavailable source.
        assert services.reviews.repository.candidate(review_id, "broken").error
        assert set(services.reviews.topic_research.unavailable_sources(review_id)) == {"broken"}
        await services.reviews.topic_research.synthesize(review_id)
        await services.reviews._publication(review_id)
        report = (test_settings.workspace_dir / root / "report.md").read_text(encoding="utf-8")
        assert "Unavailable sources and coverage limitations" in report
        assert "Unreadable original" in report
        assert "papers/broken/summary.md" not in report
        assert f"paper={broken.id}" not in report
        assert (test_settings.workspace_dir / root / "papers/good/summary.md").is_file()
        assert not (test_settings.workspace_dir / root / "papers/broken/summary.md").exists()
        assert services.tracks.get(track.id).paper_ids == [good_id]
        assert services.documents.get_document(broken.id) is not None
        packets = [json.loads(next(row["content"] for row in reversed(request["messages"])
                                   if row["role"] == "user"))
                   for request in stub_provider.requests
                   if "messages" in request and any(row["role"] == "user" for row in request["messages"])]
        assert any(packet.get("unavailable_sources") for packet in packets)
        if failure_stage == "summary":
            monkeypatch.setattr(services.reviews, "_paper", original_paper)
        replacement = services.reviews.repository.candidate(review_id, "broken").model_copy(
            update={"document_id": good_id},
        )
        services.reviews.repository.update_candidate(review_id, replacement)
        await services.reviews._reading_plan(review_id, candidates=[replacement])
        assert services.reviews.topic_research.unavailable_sources(review_id) == {}
        assert services.reviews.repository.candidate(review_id, "broken").error is None
        assert json.loads((test_settings.workspace_dir / root / "skipped-sources.json").read_text()) == []
    finally:
        await services.close()


@pytest.mark.anyio
@pytest.mark.parametrize("error", [
    None, StorageError("Disk limit"), RuntimeError("Unexpected failure"),
    OpenAIError("Provider failure"), ReviewPaused(),
])
async def test_source_skipping_does_not_hide_empty_coverage_or_system_failures(
    test_settings, stub_provider, monkeypatch, error,
):
    services = create_services(test_settings)
    try:
        main = configure(services, stub_provider)
        spec = ReviewCreateRequest(
            title="Unavailable papers", objective="Compare methods.", main_model=main,
            summary_model=SummaryRole(model_reference=main),
            embedding_model=EmbeddingRole(model_reference=embedding_reference(services, stub_provider)),
        )
        services.reviews.repository.create("missing", spec.model_dump(mode="json"), {
            "status": "running", "stage": "reading_plan", "workspace_path": "research/unlinked/missing",
            "work_budget": {"screened_papers": 100, "read_papers": 100, "comparison_passes": 1},
        })
        services.reviews.repository.save_candidate("missing", "missing", Candidate(
            id="missing", title="No accessible source", selected=True,
        ))
        if error is not None:
            async def fail(*args, **kwargs):
                raise error
            monkeypatch.setattr(services.reviews, "_prepare_paper", fail)
            with pytest.raises(type(error)):
                await services.reviews._reading_plan("missing")
            assert services.reviews.topic_research.unavailable_sources("missing") == {}
        else:
            with pytest.raises(ValidationError, match="None of the approved papers is readable"):
                await services.reviews._reading_plan("missing")
            assert set(services.reviews.topic_research.unavailable_sources("missing")) == {"missing"}
            with pytest.raises(ValidationError, match="No readable papers"):
                await services.reviews.topic_research.synthesize("missing")
            assert not (test_settings.workspace_dir / "research/unlinked/missing/report.md").exists()
    finally:
        await services.close()


@pytest.mark.anyio
async def test_collect_and_summarize_pdf_larger_than_generic_artifact_limit(test_settings, stub_provider, monkeypatch):
    services = create_services(test_settings)
    monkeypatch.setattr(stub_provider, "_reply_for", topic_reply)
    try:
        main = configure(services, stub_provider)
        source = b"%PDF-1.4\n" + b" " * test_settings.max_artifact_bytes + b"\n%%EOF"
        document = services.documents.create_document_from_bytes(source, filename="large.pdf", title="Large paper")
        services.document_ingestion._persist_document_content(
            document, [{"page": 1, "text": "The large paper evaluates adaptive methods.", "ocr_used": False}],
            enhancement_model=None, extraction_mode="embedded",
        )
        spec = ReviewCreateRequest(
            title="Large paper", objective="Summarize the approved paper.", main_model=main,
            summary_model=SummaryRole(model_reference=main),
            embedding_model=EmbeddingRole(model_reference=embedding_reference(services, stub_provider)),
        )
        services.reviews.repository.create("large-pdf", spec.model_dump(mode="json"), {
            "status": "running", "stage": "reading_plan", "workspace_path": "research/unlinked/large-pdf",
            "work_budget": {"screened_papers": 100, "read_papers": 100, "comparison_passes": 1},
        })
        candidate = Candidate(id="large-source", title="Large paper", document_id=document.id, selected=True)
        services.reviews.repository.save_candidate("large-pdf", candidate.id, candidate)
        await services.reviews._reading_plan("large-pdf")
        path = "research/unlinked/large-pdf/papers/large-source/source.pdf"
        assert (test_settings.workspace_dir / path).read_bytes() == source
        manifest = services.reviews.repository.job("large-pdf", "workspace-files")
        assert manifest["files"][path] == services.storage.file_hash(test_settings.workspace_dir, path)
        await services.reviews._reading("large-pdf")
        assert (test_settings.workspace_dir / path).with_name("summary.md").is_file()
        with pytest.raises(StorageError, match="Artifact exceeds"):
            services.reviews.topic_research.write("large-pdf", "other.bin", source)
        services.storage.write_workspace_document(path, b"%PDF-1.4\nEdited\n%%EOF")
        with pytest.raises(ValidationError, match="edited outside"):
            services.reviews.topic_research.capture_paper("large-pdf", candidate)
    finally:
        await services.close()


def topic_reply(payload):
    schema = (payload.get("response_format") or {}).get("json_schema") or (payload.get("text") or {}).get("format", {})
    if schema.get("name") == "ReviewScope":
        return review_reply(payload)
    text = next(row["content"] for row in reversed(payload["messages"]) if row["role"] == "user")
    if isinstance(text, list):
        text = "".join(row.get("text", "") for row in text)
    packet = json.loads(text)
    if schema.get("name") == "TopicLabels":
        return json.dumps({"topics": [
            {"id": row["id"], "title": "Adaptive evaluation", "description": "Methods and their evaluation settings."}
            for row in packet["records"]
        ]})
    if schema.get("name") == "TopicSearchPlan":
        return json.dumps({"queries": [{"provider": "library", "query": "Orthogonal", "pages": 1}]})
    if payload["model"] == "summary":
        return "FILE_ONLY_PAPER_SUMMARY. The adaptive method was evaluated in one setting."
    return "Compare evaluation assumptions and limitations across the supplied paper IDs."


@pytest.mark.anyio
@pytest.mark.parametrize("api_mode", ["chat_completions", "responses"])
async def test_topic_workflow_gates_collection_and_keeps_summaries_off_sqlite(
    test_settings, stub_provider, monkeypatch, api_mode,
):
    services = create_services(test_settings)
    monkeypatch.setattr(stub_provider, "_reply_for", topic_reply)
    try:
        main = configure(services, stub_provider, api_mode=api_mode)
        summary = configure(services, stub_provider, name="summary", api_mode=api_mode)
        naming = configure(services, stub_provider, name="small-tasks", api_mode=api_mode)
        test_settings.default_model_references["small_tasks"] = naming
        embedding = embedding_reference(services, stub_provider)
        track = services.tracks.create("Adaptive research")
        for index in range(2):
            await seed_paper(services, title=f"Adaptive paper {index}", suffix=str(index))
            stub_provider.embedding_vectors[f"Adaptive paper {index}\n\n"] = [1, 0] if index == 0 else [0, 1]
        review = await services.reviews.create(ReviewCreateRequest(
            title="Topic-led research", objective="Compare adaptive evaluation settings.",
            main_model=main, summary_model=SummaryRole(model_reference=summary),
            embedding_model=EmbeddingRole(model_reference=embedding), web_enabled=False,
            track_id=track.id,
        ))
        assert review.workspace_path.startswith(f"research/adaptive-research--{track.id}/")
        assert (await settle(services, review.id)).approval == "plan"
        if api_mode == "responses":
            request = next(
                row for row in stub_provider.response_api_requests
                if row["model"] == "main" and row.get("text", {}).get("format", {}).get("name") == "ReviewScope"
            )
            response_format = request["text"]["format"]
            schema = response_format["schema"]
        else:
            request = next(
                row for row in stub_provider.requests
                if row["model"] == "main"
                and row.get("response_format", {}).get("json_schema", {}).get("name") == "ReviewScope"
            )
            response_format = request["response_format"]["json_schema"]
            schema = response_format["schema"]
        assert response_format["name"] == "ReviewScope"
        assert response_format["strict"] is True
        assert set(schema["properties"]) == set(ReviewScope.model_fields)
        assert set(schema["required"]) == set(ReviewScope.model_fields)
        assert schema["additionalProperties"] is False
        await command(services, review.id, "approve_plan")
        waiting = await settle(services, review.id)
        assert waiting.approval == "topics", waiting.error
        assert waiting.work_used.read_papers == 0
        assert waiting.selected_count == 0
        assert len(waiting.topics) == 2
        labels = [row for row in (
            stub_provider.response_api_requests if api_mode == "responses" else stub_provider.requests
        ) if (row.get("response_format", {}).get("json_schema") or row.get("text", {}).get("format", {})).get("name") == "TopicLabels"]
        assert labels and all(row["model"] == "small-tasks" for row in labels)
        embeddings = [row for row in stub_provider.requests if row["model"] == "embed"]
        assert len(embeddings) == 1 and len(embeddings[0]["input"]) == 2
        assert all("\n\n" in text for text in embeddings[0]["input"])
        assert not any(row["model"] == "summary" for row in stub_provider.requests)
        with pytest.raises(ValidationError, match="Choose at least one"):
            await command(services, review.id, "approve_topics", topic_ids=["invented"])
        with pytest.raises(ConflictError):
            await command(services, review.id, "approve_papers")
        topic_ids = [row.id for row in waiting.topics]
        await command(services, review.id, "explore_topics", topic_ids=topic_ids,
                      feedback="Compare limitations.")
        explored = await settle(services, review.id)
        assert explored.approval == "topics", explored.error
        assert all(row.exploration for row in explored.topics)
        assert explored.work_used.read_papers == 0
        await command(services, review.id, "pause")
        await command(services, review.id, "resume")
        assert services.reviews.get(review.id).approval == "topics"
        await command(services, review.id, "approve_topics", topic_ids=topic_ids)
        finished = await settle(services, review.id)
        assert finished.status == "completed", finished.error
        assert finished.selected_count == 2
        assert finished.output_path == f"{review.workspace_path}/report.md"
        report = (test_settings.workspace_dir / finished.output_path).read_text()
        for candidate in services.reviews._candidates(review.id, selected=True):
            assert f"[PDF](/library?paper={candidate.document_id})" in report
        assert len(services.tracks.get(track.id).paper_ids) == 2
        assert "Research report" in services.tracks.read_section(track.id, "literature").content
        root = test_settings.workspace_dir
        assert (root / review.workspace_path / "scratch" / "index.md").exists()
        summaries = list((root / review.workspace_path / "papers").glob("*/summary.md"))
        assert len(summaries) == 2
        assert all("FILE_ONLY_PAPER_SUMMARY" in path.read_text() for path in summaries)
        assert len(list((root / review.workspace_path / "papers").glob("*/source.pdf"))) == 2
        services.workspace.refresh_index()
        with services.reviews.repository.sessions() as session:
            jobs = list(session.scalars(select(ReviewJob.content).where(ReviewJob.review_id == review.id)))
            indexed = list(session.scalars(select(WorkspaceEntry.search_content)))
        assert all("FILE_ONLY_PAPER_SUMMARY" not in json.dumps(job) for job in jobs)
        assert all("FILE_ONLY_PAPER_SUMMARY" not in text for text in indexed)
        assert not any(row.get("run_id") for row in services.reviews.repository.jobs(review.id))
        before = len(stub_provider.requests)
        await command(services, review.id, "resume", stage="reading")
        again = await settle(services, review.id)
        assert again.status == "completed", again.error
        assert len(stub_provider.requests) == before
        edited_path = summaries[0].relative_to(root).as_posix()
        services.workspace.write_file(edited_path, "My edited summary stays on disk.")
        cleared = await services.reviews.clear(review.id, again.revision)
        assert edited_path in cleared.preserved_paths
        assert summaries[0].read_text() == "My edited summary stays on disk."
        assert "Research report" not in services.tracks.read_section(track.id, "literature").content
        assert services.tracks.get(track.id).paper_ids == []
    finally:
        await services.close()


@pytest.mark.anyio
async def test_ollama_review_generation_receives_raw_json_schema(test_settings, stub_provider, monkeypatch):
    services = create_services(test_settings)
    captured = []

    async def request(method, path, **kwargs):
        captured.append((path, kwargs["json"]))
        return httpx.Response(200, json={
            "response": json.dumps({
                "questions": ["Compare adaptive methods."],
                "inclusion_criteria": ["Original evidence"],
                "exclusion_criteria": [],
                "queries": [{"provider": "library", "query": "adaptive methods", "pages": 1}],
                "suggested_allowance_tokens": 100000,
                "suggested_work_budget": {
                    "screened_papers": 100, "read_papers": 20, "comparison_passes": 1,
                },
                "rationale": "A bounded local review.",
            }),
            "done": True,
            "done_reason": "stop",
        })

    monkeypatch.setattr(services.ollama, "_request_unlocked", request)
    try:
        main = {"provider_profile_id": services.ollama.profile_id, "model": "local-review"}
        review = await services.reviews.create(ReviewCreateRequest(
            title="Local review", objective="Compare adaptive methods.",
            main_model=main, summary_model=SummaryRole(model_reference=main),
            embedding_model=EmbeddingRole(model_reference=embedding_reference(services, stub_provider)),
        ))
        assert (await settle(services, review.id)).approval == "plan"
        assert len(captured) == 1
        path, payload = captured[0]
        assert path == "/api/generate"
        schema = payload["format"]
        assert schema["type"] == "object"
        assert set(schema["properties"]) == set(ReviewScope.model_fields)
        assert set(schema["required"]) == set(ReviewScope.model_fields)
        assert schema["additionalProperties"] is False
        assert "json_schema" not in schema
    finally:
        await services.close()


@pytest.mark.anyio
async def test_embedding_batches_resume_and_clear_preserves_edited_files(test_settings, stub_provider, monkeypatch):
    services = create_services(test_settings)
    monkeypatch.setattr(stub_provider, "_reply_for", topic_reply)
    try:
        main = configure(services, stub_provider)
        embedding = embedding_reference(services, stub_provider)
        spec = ReviewCreateRequest(
            title="Batched topics", objective="Compare methods.", main_model=main,
            summary_model=SummaryRole(model_reference=main),
            embedding_model=EmbeddingRole(model_reference=embedding),
        )
        services.reviews.repository.create("batch", spec.model_dump(mode="json"), {
            "status": "running", "stage": "screening", "workspace_path": "research/unlinked/batch",
        })
        for index in range(33):
            services.reviews.repository.save_candidate("batch", str(index), Candidate(
                id=f"candidate-{index}", title=f"Method {index}", abstract=f"Evidence for approach {index}.",
            ))
        await services.reviews._screening("batch")
        requests = [row for row in stub_provider.requests if row["model"] == "embed"]
        assert [len(row["input"]) for row in requests] == [16, 16, 1]
        await services.reviews._screening("batch")
        assert len([row for row in stub_provider.requests if row["model"] == "embed"]) == 3
        topics_path = "research/unlinked/batch/topics.json"
        services.workspace.write_file(topics_path, {"edited": "Keep this user edit."})
        result = await services.reviews.clear("batch", services.reviews.get("batch").revision)
        assert topics_path in result.preserved_paths
        assert (test_settings.workspace_dir / topics_path).exists()
    finally:
        await services.close()

@pytest.mark.anyio
async def test_more_topics_fetches_papers_and_approval_retains_unselected_content(test_settings, stub_provider, monkeypatch):
    services = create_services(test_settings)
    monkeypatch.setattr(stub_provider, "_reply_for", topic_reply)
    try:
        main = configure(services, stub_provider)
        embedding = embedding_reference(services, stub_provider)
        original_document = await seed_paper(services, title="Adaptive original", suffix="original")
        review = await services.reviews.create(ReviewCreateRequest(
            title="More topics", objective="Compare adaptive evaluation settings.",
            main_model=main, summary_model=SummaryRole(model_reference=main),
            embedding_model=EmbeddingRole(model_reference=embedding), web_enabled=False,
        ))
        await settle(services, review.id)
        await command(services, review.id, "approve_plan")
        initial = await settle(services, review.id)
        original_topic = initial.topics[0]
        await command(services, review.id, "explore_topics", topic_ids=[original_topic.id])
        explored = await settle(services, review.id)
        original_exploration = explored.topics[0].exploration
        added_document = await seed_paper(services, title="Orthogonal evaluation", suffix="new")
        before = len([row for row in stub_provider.requests if row["model"] == "embed"])
        await command(services, review.id, "find_more_topics", topic_ids=[original_topic.id], feedback="Find adjacent methods.")
        expanded = await settle(services, review.id)
        assert expanded.approval == "topics", expanded.error
        assert len(expanded.topics) == 2
        assert expanded.topics[0].id == original_topic.id
        assert expanded.topics[0].exploration == original_exploration
        assert expanded.selected_topic_ids == [original_topic.id]
        requests = [row for row in stub_provider.requests if row["model"] == "embed"]
        assert len(requests) == before + 1
        assert requests[-1]["input"] == ["Orthogonal evaluation\n\n"]
        assert expanded.work_used.read_papers == 0
        assert any(row.get("query") == "Orthogonal" for row in services.reviews.repository.jobs(review.id, "discovery"))
        assert not (test_settings.workspace_dir / review.workspace_path / "papers").exists()
        chosen = expanded.topics[1]
        await command(services, review.id, "explore_topics", topic_ids=[chosen.id])
        await settle(services, review.id)
        discarded_path = f"{review.workspace_path}/topics/{original_topic.id}.md"
        assert (test_settings.workspace_dir / discarded_path).exists()
        await command(services, review.id, "approve_topics", topic_ids=[chosen.id])
        completed = await settle(services, review.id)
        assert completed.status == "completed", completed.error
        assert [row.id for row in completed.topics] == [original_topic.id, chosen.id]
        assert completed.candidate_count == 2 and completed.selected_count == 1
        assert (test_settings.workspace_dir / discarded_path).exists()
        assert services.documents.get_document(original_document) is not None
        assert services.documents.get_document(added_document) is not None
        await command(services, review.id, "revise_selection")
        assert services.reviews.get(review.id).approval == "topics"
    finally:
        await services.close()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["edited", "interrupted"])
async def test_approval_preserves_edited_unselected_files_without_cleanup(test_settings, stub_provider, monkeypatch, failure):
    services = create_services(test_settings)
    monkeypatch.setattr(stub_provider, "_reply_for", topic_reply)
    try:
        main = configure(services, stub_provider)
        embedding = embedding_reference(services, stub_provider)
        for index in range(2):
            await seed_paper(services, title=f"Adaptive cleanup {index}", suffix=f"cleanup-{index}")
            stub_provider.embedding_vectors[f"Adaptive cleanup {index}\n\n"] = [1, 0] if index == 0 else [0, 1]
        review = await services.reviews.create(ReviewCreateRequest(
            title="Cleanup", objective="Compare adaptive methods.", main_model=main,
            summary_model=SummaryRole(model_reference=main),
            embedding_model=EmbeddingRole(model_reference=embedding), web_enabled=False,
        ))
        await settle(services, review.id)
        await command(services, review.id, "approve_plan")
        waiting = await settle(services, review.id)
        ids = [row.id for row in waiting.topics]
        await command(services, review.id, "explore_topics", topic_ids=ids)
        await settle(services, review.id)
        path = f"{review.workspace_path}/topics/{ids[1]}.md"
        original = services.workspace.read_file(path).content
        delete_file = services.workspace.delete_file
        if failure == "edited":
            services.workspace.write_file(path, "My personal edit must not be deleted.")
        else:
            def interrupted_delete(target):
                if target == path:
                    raise OSError("Cleanup interrupted.")
                return delete_file(target)

            monkeypatch.setattr(services.workspace, "delete_file", interrupted_delete)
        await command(services, review.id, "approve_topics", topic_ids=[ids[0]])
        completed = await settle(services, review.id)
        assert completed.status == "completed", completed.error
        if failure == "edited":
            assert services.workspace.read_file(path).content == "My personal edit must not be deleted."
        else:
            assert services.workspace.read_file(path).content == original
        assert completed.candidate_count == 2
        assert (test_settings.workspace_dir / path).exists()
        assert len(services.documents.list_documents()) == 2
    finally:
        await services.close()

@pytest.mark.anyio
async def test_more_topics_with_only_duplicate_papers_retains_menu(test_settings, stub_provider, monkeypatch):
    services = create_services(test_settings)
    monkeypatch.setattr(stub_provider, "_reply_for", topic_reply)
    try:
        main = configure(services, stub_provider)
        await seed_paper(services, title="Orthogonal adaptive methods", suffix="duplicate")
        review = await services.reviews.create(ReviewCreateRequest(
            title="Duplicate discovery", objective="Compare adaptive methods.", main_model=main,
            summary_model=SummaryRole(model_reference=main),
            embedding_model=EmbeddingRole(model_reference=embedding_reference(services, stub_provider)),
            web_enabled=False,
        ))
        await settle(services, review.id)
        await command(services, review.id, "approve_plan")
        initial = await settle(services, review.id)
        before = len([row for row in stub_provider.requests if row["model"] == "embed"])
        await command(services, review.id, "find_more_topics")
        waiting = await settle(services, review.id)
        assert waiting.approval == "topics", waiting.error
        assert waiting.topics == initial.topics
        assert waiting.candidate_count == 1
        assert any("No additional papers" in notice for notice in waiting.notices)
        assert len([row for row in stub_provider.requests if row["model"] == "embed"]) == before
    finally:
        await services.close()


@pytest.mark.anyio
async def test_more_topics_extends_full_initial_corpus_by_at_most_100(test_settings, stub_provider, monkeypatch):
    services = create_services(test_settings)

    def reply(payload):
        schema = (payload.get("response_format") or {}).get("json_schema") or {}
        if schema.get("name") == "TopicSearchPlan":
            return json.dumps({"queries": [{"provider": "arxiv", "query": "new adjacent methods", "pages": 1}]})
        return topic_reply(payload)

    monkeypatch.setattr(stub_provider, "_reply_for", reply)
    calls = []

    async def query(provider, query, page, **kwargs):
        calls.append((provider, query, page, kwargs["limit"]))
        return {"results": [
            {"title": f"Adjacent method {index}", "url": f"https://example.test/new-{index}"}
            for index in range(110)
        ]}

    monkeypatch.setattr(services.reviews.discovery, "query", query)
    try:
        main = configure(services, stub_provider)
        spec = ReviewCreateRequest(
            title="Full initial corpus", objective="Compare methods.", main_model=main,
            summary_model=SummaryRole(model_reference=main),
            embedding_model=EmbeddingRole(model_reference=embedding_reference(services, stub_provider)),
        )
        scope = ReviewScope(questions=["Compare methods"], inclusion_criteria=[], exclusion_criteria=[],
                            queries=[{"provider": "library", "query": "methods", "pages": 1}], rationale="Compare.")
        services.reviews.repository.create("full", spec.model_dump(mode="json"), {
            "status": "running", "stage": "screening", "workspace_path": "research/unlinked/full",
            "scope": scope.model_dump(mode="json"),
        })
        for index in range(100):
            services.reviews.repository.save_candidate("full", str(index), Candidate(
                id=f"initial-{index}", title=f"Original method {index}", url=f"https://example.test/initial-{index}",
            ))
        await services.reviews._screening("full")
        initial = services.reviews.get("full")
        assert initial.candidate_count == 100
        await command(services, "full", "find_more_topics")
        expanded = await settle(services, "full")
        assert expanded.approval == "topics", expanded.error
        assert expanded.candidate_count == 200
        assert calls == [("arxiv", "new adjacent methods", 0, 100)]
        assert expanded.topics[0] == initial.topics[0]
        assert len(expanded.topics) == 2
        assert len(expanded.topics[1].candidate_ids) == 100
    finally:
        await services.close()


@pytest.mark.anyio
async def test_large_summary_corpus_is_fully_reduced_through_scratch_files(test_settings, stub_provider, monkeypatch):
    services = create_services(test_settings)
    summary_text = "The method depends on evaluation assumptions, data selection, and the comparison baseline. " * 500

    def reply(payload):
        return summary_text if payload["model"] == "summary" else topic_reply(payload)

    monkeypatch.setattr(stub_provider, "_reply_for", reply)
    try:
        provider = services.providers.create(ProviderCreate(
            name="Main with small context", kind="openai_compatible", api_key="stub-key",
            base_url=f"{stub_provider.base_url}/v1",
            models=[ProviderModel(name="gpt-4o-mini", capabilities={"chat"}, context_window_tokens=16384)],
        ))
        main = {"provider_profile_id": provider.id, "model": "gpt-4o-mini"}
        summary = configure(services, stub_provider, name="summary")
        embedding = embedding_reference(services, stub_provider)
        spec = ReviewCreateRequest(
            title="Many summaries", objective="Compare all methods.", main_model=main,
            embedding_model=EmbeddingRole(model_reference=embedding),
            summary_model=SummaryRole(model_reference=summary),
        )
        services.reviews.repository.create("large", spec.model_dump(mode="json"), {
            "status": "running", "stage": "reading_plan", "workspace_path": "research/unlinked/large",
            "selected_topic_ids": [], "references_skipped": True,
            "work_budget": {"screened_papers": 100, "read_papers": 100, "comparison_passes": 1},
        })
        for index in range(3):
            document_id = await seed_paper(services, title=f"Adaptive corpus {index}", suffix=f"large-{index}")
            candidate = Candidate(id=f"large-{index}", title=f"Adaptive corpus {index}", selected=True,
                                  document_id=document_id)
            services.reviews.repository.save_candidate("large", candidate.id, candidate)
        await services.reviews._reading_plan("large")
        with pytest.raises(ValidationError, match="Finish every selected paper"):
            await services.reviews._synthesis("large")
        await services.reviews._reading("large")
        await services.reviews._synthesis("large")
        root = test_settings.workspace_dir / "research" / "unlinked" / "large"
        assert len(list((root / "scratch").glob("inferences-*.md"))) > 1
        assert list((root / "scratch").glob("combined-*.md"))
        report = (root / "report.md").read_text()
        ledger = (root / "scratch" / "index.md").read_text()
        assert all(f"large-{index}" in report and f"large-{index}" in ledger for index in range(3))
        inference_notes = "\n".join(path.read_text() for path in (root / "scratch").glob("inferences-*.md"))
        assert all(f"(../papers/large-{index}/summary.md)" in inference_notes for index in range(3))
        before = len(stub_provider.requests)
        await services.reviews._synthesis("large")
        assert len(stub_provider.requests) == before
    finally:
        await services.close()
