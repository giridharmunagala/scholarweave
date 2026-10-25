from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import numpy as np
import pytest

from backend.agents.blueprint import ModelReferenceSpec
from backend.bootstrap import create_services
from backend.core.errors import ValidationError
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
        return [[1.0, 0.02 * len(text)] if "Vision" in text else [0.02 * len(text), 1.0] for text in texts]

    async def generate(self, resolved, prompt, **kwargs):
        packet = json.loads(prompt)
        self.prompts.append(packet)
        return {"response": json.dumps({"topics": [
            {"id": cluster["id"], "name": cluster["titles"][0].split()[0], "description": "Shared theme."}
            for cluster in packet["clusters"]
        ]})}


@pytest.mark.anyio
async def test_topic_names_use_small_tasks_with_explicit_override_and_main_fallback(test_settings, stub_provider, monkeypatch):
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
        assert result["naming_model"] == {"provider_profile_id": profile.id, "model": "small"}
        assert result["embedding_model"]["model"] == "embed"
        assert stub_provider.requests[-1]["model"] == "small"
        explicit = ModelReferenceSpec(provider_profile_id=profile.id, model="large")
        result = await service.build(None, ModelReferenceSpec(), explicit)
        assert result["naming_model"]["model"] == "large"
        assert stub_provider.requests[-1]["model"] == "large"
        del test_settings.default_model_references["small_tasks"]
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
