from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from backend.agents.blueprint import ModelReferenceSpec
from backend.app import create_app
from backend.bootstrap import create_services
from backend.core.errors import ConflictError, ValidationError
from backend.documents.topics import MIN_PAPERS, PaperTopicService, cluster_papers
from backend.persistence.files import SafeStorage
from backend.prompting.registry import PromptRegistry
from backend.providers.schemas import ProviderCreate, ProviderModel


def test_cluster_papers_separates_distinct_directions() -> None:
    rng = np.random.default_rng(0)
    axes = np.eye(8)[:3]
    vectors = np.vstack([axis + rng.normal(0, 0.05, (6, 8)) for axis in axes])
    groups = cluster_papers(vectors)
    assert sorted(len(group) for group in groups) == [6, 6, 6]
    assert {frozenset(group) for group in groups} == {
        frozenset(range(0, 6)), frozenset(range(6, 12)), frozenset(range(12, 18)),
    }


class FakeDocuments:
    def __init__(self, count: int) -> None:
        self.papers = [
            SimpleNamespace(id=f"p{i}", title=f"{'Vision' if i % 2 else 'Language'} paper {i}",
                            metadata_json={"folder_id": "f1"} if i < 3 else {})
            for i in range(count)
        ]

    def list_documents(self):
        return self.papers

    def list_folders(self):
        return [SimpleNamespace(id="f1")]

    def summary_excerpt(self, document_id: str, max_chars: int):
        return {"chunks": [{"text": f"Opening text for {document_id}"}]}


class FakeRuntime:
    def __init__(self) -> None:
        self.embedded: list[str] = []
        self.prompts: list[dict] = []

    def resolve(self, capability, model_reference=None):
        return SimpleNamespace(profile_id="local", model="embed" if capability == "embedding" else "namer")

    async def embed(self, resolved, texts):
        self.embedded.extend(texts)
        return [[1.0, 0.01] if "Vision" in text else [0.01, 1.0] for text in texts]

    async def generate(self, resolved, prompt, **kwargs):
        packet = json.loads(prompt)
        self.prompts.append(packet)
        return {"response": json.dumps({"topics": [
            {"id": cluster["id"], "name": cluster["titles"][0].split()[0], "description": "Shared theme."}
            for cluster in packet["clusters"]
        ]})}


@pytest.mark.anyio
async def test_topic_names_use_main_model_and_saved_names_need_no_model_calls(test_settings, stub_provider, monkeypatch):
    services = create_services(test_settings)
    try:
        profile = services.providers.create(ProviderCreate(
            name="Llama router", kind="openai_compatible", base_url=f"{stub_provider.base_url}/v1",
            models=[ProviderModel(name=name, capabilities={"embedding"} if name == "embed" else {"chat"})
                    for name in ("large", "small", "embed")],
        ))
        test_settings.default_model_references = {
            role: {"provider_profile_id": profile.id, "model": name}
            for role, name in (("chat", "large"), ("small_tasks", "small"), ("embedding", "embed"))
        }

        def reply(payload):
            packet = json.loads(payload["messages"][-1]["content"])
            return json.dumps({"topics": [
                {"id": row["id"], "name": f"Topic {row['id']}", "description": "A shared research theme."}
                for row in packet["clusters"]
            ]})

        monkeypatch.setattr(stub_provider, "_reply_for", reply)
        storage = SafeStorage(test_settings)
        service = PaperTopicService(
            FakeDocuments(MIN_PAPERS), services.model_runtime, storage,
            PromptRegistry(test_settings.prompt_config_dir, storage=storage),
        )
        result = await service.build(None, ModelReferenceSpec(), ModelReferenceSpec())
        assert result["naming_model"] == {"provider_profile_id": profile.id, "model": "large"}
        assert result["embedding_model"]["model"] == "embed"
        assert stub_provider.requests[-1]["model"] == "large"
        request_count = len(stub_provider.requests)
        assert await service.build(None, ModelReferenceSpec(), ModelReferenceSpec()) == result
        assert len(stub_provider.requests) == request_count
        service.clear(None)
        explicit = ModelReferenceSpec(provider_profile_id=profile.id, model="small")
        result = await service.build(None, ModelReferenceSpec(), explicit)
        assert result["naming_model"]["model"] == "small"
        assert stub_provider.requests[-1]["model"] == "small"
        service.clear(None)
        result = await service.build(None, ModelReferenceSpec(), ModelReferenceSpec())
        assert result["naming_model"]["model"] == "large"
        assert len([row for row in stub_provider.requests if row["model"] == "embed"]) == 1
    finally:
        await services.close()


def test_topic_service_groups_names_caches_and_scopes(test_settings) -> None:
    asyncio.run(_scenario(test_settings))


async def _scenario(test_settings) -> None:
    storage = SafeStorage(test_settings)
    runtime = FakeRuntime()
    service = PaperTopicService(
        FakeDocuments(10), runtime, storage, PromptRegistry(test_settings.prompt_config_dir, storage=storage),
    )
    spec = ModelReferenceSpec()
    result = await service.build(None, spec, spec)

    assert {topic["name"] for topic in result["topics"]} == {"Vision", "Language"}
    assert sorted(p for topic in result["topics"] for p in topic["paper_ids"]) == sorted(f"p{i}" for i in range(10))
    assert result["naming_model"] == {"provider_profile_id": "local", "model": "namer"}
    assert service.get(None)["topics"] == result["topics"]
    assert service.get("f1") is None

    await service.build(None, spec, spec)
    assert len(runtime.embedded) == 10  # second run reuses cached embeddings

    with pytest.raises(ValidationError, match=str(MIN_PAPERS)):
        await service.build("f1", spec, spec)

    service.clear(None)
    assert service.get(None) is None


@pytest.mark.anyio
async def test_enrichment_persists_names_assignments_and_embeds_only_new_papers(test_settings, monkeypatch):
    documents = FakeDocuments(MIN_PAPERS)
    runtime = FakeRuntime()
    storage = SafeStorage(test_settings)
    prompts = PromptRegistry(test_settings.prompt_config_dir, storage=storage)
    service = PaperTopicService(documents, runtime, storage, prompts)
    spec = ModelReferenceSpec()
    original = await service.build(None, spec, spec)
    names = [(topic["id"], topic["name"], topic["description"]) for topic in original["topics"]]
    vision = next(topic["id"] for topic in original["topics"] if topic["name"] == "Vision")
    await service.assign(None, vision, ["p0"])

    # A fresh service must reuse the saved map and vectors, not recluster or rename.
    service = PaperTopicService(documents, runtime, storage, prompts)
    monkeypatch.setattr("backend.documents.topics.cluster_papers", lambda *_args, **_kwargs: pytest.fail("Reclustered saved topics"))
    runtime.prompts.clear()
    runtime.embedded.clear()
    documents.papers.append(SimpleNamespace(id="new", title="Vision paper", metadata_json={}))
    result = await service.build(None, spec, spec)
    assert [(topic["id"], topic["name"], topic["description"]) for topic in result["topics"]] == names
    assert len(runtime.embedded) == 1
    assert runtime.prompts == []
    assert {"p0", "new"} <= set(next(topic["paper_ids"] for topic in result["topics"] if topic["id"] == vision))
    assert service.get(None) == result
    assert await service.build(None, spec, spec) == result
    assert len(runtime.embedded) == 1


@pytest.mark.anyio
async def test_legacy_maps_enrich_without_vectors_and_empty_topics_remain_assignable(test_settings):
    documents = FakeDocuments(MIN_PAPERS)
    for paper in documents.papers:
        paper.metadata_json = {"folder_id": "f1"}
    runtime = FakeRuntime()
    storage = SafeStorage(test_settings)
    service = PaperTopicService(
        documents, runtime, storage, PromptRegistry(test_settings.prompt_config_dir, storage=storage),
    )
    spec = ModelReferenceSpec()
    original = await service.build("f1", spec, spec)
    service._write("vectors.json", {})
    documents.papers.append(SimpleNamespace(id="new", title="Vision paper", metadata_json={"folder_id": "f1"}))
    enriched = await service.build("f1", spec, spec)
    assert len(runtime.embedded) == MIN_PAPERS + 1
    assert len(runtime.prompts) == 1
    assert enriched["topics"][0]["name"] == original["topics"][0]["name"]
    topic_ids = [topic["id"] for topic in enriched["topics"]]
    await service.assign("f1", topic_ids[0], [paper.id for paper in documents.papers])
    assert service.get("f1")["topics"][1]["paper_ids"] == []
    documents.papers[-1].metadata_json = {}
    scoped = service.get("f1")
    assert "new" not in scoped["paper_ids"]
    assert all("new" not in topic["paper_ids"] for topic in scoped["topics"])
    with pytest.raises(ValidationError, match="existing papers"):
        await service.assign("f1", topic_ids[1], ["new"])
    with pytest.raises(ValidationError, match="not found"):
        await service.assign("f1", "unknown", ["p0"])
    with pytest.raises(ValidationError, match="existing papers"):
        await service.assign("f1", topic_ids[1], ["p0", "missing"])
    assert service.get("f1") == scoped
    reassigned = await service.assign("f1", topic_ids[1], ["p0"])
    assert reassigned["topics"][1]["paper_ids"] == ["p0"]
    assert "p0" not in reassigned["topics"][0]["paper_ids"]
    # A small remaining scope can still update saved topics.
    documents.papers = documents.papers[:2]
    documents.papers.append(SimpleNamespace(id="fresh", title="Vision paper", metadata_json={"folder_id": "f1"}))
    result = await service.build("f1", spec, spec)
    assert set(result["paper_ids"]) == {"p0", "p1", "fresh"}
    assert len(result["topics"]) == 2


@pytest.mark.anyio
async def test_manual_assignment_and_clear_refuse_inflight_updates(test_settings):
    storage = SafeStorage(test_settings)
    service = PaperTopicService(
        FakeDocuments(MIN_PAPERS), FakeRuntime(), storage,
        PromptRegistry(test_settings.prompt_config_dir, storage=storage),
    )
    async with service._lock:
        with pytest.raises(ConflictError):
            await service.assign(None, "t1", ["p0"])
        with pytest.raises(ConflictError):
            service.clear(None)


@pytest.mark.anyio
async def test_failed_enrichment_keeps_saved_topics_and_does_not_poison_embedding_cache(test_settings, monkeypatch):
    storage = SafeStorage(test_settings)
    documents = FakeDocuments(MIN_PAPERS)
    runtime = FakeRuntime()
    service = PaperTopicService(
        documents, runtime, storage, PromptRegistry(test_settings.prompt_config_dir, storage=storage),
    )
    spec = ModelReferenceSpec()
    original = await service.build(None, spec, spec)
    documents.papers.append(SimpleNamespace(id="new", title="Vision new paper", metadata_json={}))
    valid_embed = runtime.embed

    async def unusable_vectors(_resolved, texts):
        return [[0.0, 0.0] for _text in texts]

    monkeypatch.setattr(runtime, "embed", unusable_vectors)
    with pytest.raises(ValidationError, match="unusable vectors"):
        await service.build(None, spec, spec)
    assert service.get(None) == original
    monkeypatch.setattr(runtime, "embed", valid_embed)
    result = await service.build(None, spec, spec)
    assert "new" in result["paper_ids"]
    assert len(runtime.prompts) == 1


def test_topic_api_persists_manual_memberships_and_incremental_updates(test_settings, stub_provider, monkeypatch):
    def reply(payload):
        packet = json.loads(payload["messages"][-1]["content"])
        return json.dumps({"topics": [
            {"id": row["id"], "name": row["titles"][0].split()[0], "description": "Shared theme."}
            for row in packet["clusters"]
        ]})

    monkeypatch.setattr(stub_provider, "_reply_for", reply)
    app = create_app(test_settings)
    with TestClient(app) as client:
        services = app.state.services
        profile = services.providers.create(ProviderCreate(
            name="Local router", kind="openai_compatible", base_url=f"{stub_provider.base_url}/v1",
            models=[ProviderModel(name=name, capabilities={"embedding"} if name == "embed" else {"chat"})
                    for name in ("main", "helper", "embed")],
        ))
        test_settings.default_model_references = {
            role: {"provider_profile_id": profile.id, "model": name}
            for role, name in (("chat", "main"), ("small_tasks", "helper"), ("embedding", "embed"))
        }
        papers = [
            services.documents.create_document_from_bytes(
                f"%PDF-1.4\npaper {index}\n%%EOF".encode(), filename=f"paper-{index}.pdf",
                title=f"{'Vision' if index % 2 else 'Language'} paper {index}",
            ) for index in range(MIN_PAPERS)
        ]
        for paper in papers:
            stub_provider.embedding_vectors[services.paper_topics._text(paper)] = (
                [1.0, 0.0] if "Vision" in paper.title else [0.0, 1.0]
            )
        result = client.post("/api/paper-topics", json={})
        assert result.status_code == 200, result.text
        original = result.json()
        assert original["naming_model"]["model"] == "main"
        assert len(original["topics"]) == 2
        language = next(topic["id"] for topic in original["topics"] if topic["name"] == "Language")
        vision_paper = papers[1]
        assigned = client.patch("/api/paper-topics/assign", json={
            "topic_id": language, "paper_ids": [vision_paper.id],
        })
        assert assigned.status_code == 200, assigned.text
        assert vision_paper.id in next(topic["paper_ids"] for topic in assigned.json()["topics"] if topic["id"] == language)
        for payload in (
            {"topic_id": "missing", "paper_ids": [vision_paper.id]},
            {"topic_id": language, "paper_ids": ["missing"]},
        ):
            assert client.patch("/api/paper-topics/assign", json=payload).status_code == 400
        assert client.patch("/api/paper-topics/assign", json={"topic_id": language, "paper_ids": []}).status_code == 422
        saved = client.get("/api/paper-topics").json()
        acquired = client.get(f"/api/documents/{vision_paper.id}").json()["created_at"]
        assert acquired.endswith("Z")
        services.documents.repository.mark_ready(vision_paper.id, page_count=2, metadata={})
        assert client.get(f"/api/documents/{vision_paper.id}").json()["created_at"] == acquired
        assert next(paper["created_at"] for paper in client.get("/api/documents").json() if paper["id"] == vision_paper.id) == acquired

    with TestClient(create_app(test_settings)) as client:
        assert client.get("/api/paper-topics").json() == saved
        services = client.app.state.services
        new_paper = services.documents.create_document_from_bytes(
            b"%PDF-1.4\nnew acquisition\n%%EOF", filename="new.pdf", title="Language new paper",
        )
        stub_provider.embedding_vectors[services.paper_topics._text(new_paper)] = [0.0, 1.0]
        stub_provider.requests.clear()
        enriched = client.post("/api/paper-topics", json={})
        assert enriched.status_code == 200, enriched.text
        updated = enriched.json()
        assert [topic["name"] for topic in updated["topics"]] == [topic["name"] for topic in original["topics"]]
        members = next(topic["paper_ids"] for topic in updated["topics"] if topic["id"] == language)
        assert {vision_paper.id, new_paper.id} <= set(members)
        assert len(stub_provider.requests) == 1
        assert stub_provider.requests[0]["model"] == "embed"
        assert len(stub_provider.requests[0]["input"]) == 1
