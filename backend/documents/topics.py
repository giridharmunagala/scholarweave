"""Persistent paper topic shelves with incremental embedding-based assignment."""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

import numpy as np

from backend.agents.blueprint import ModelReferenceSpec
from backend.core.errors import ConflictError, DocumentProcessingError, ValidationError
from backend.documents.service import DocumentService
from backend.persistence.files import SafeStorage
from backend.prompting.registry import PromptRegistry
from backend.providers.inference import inference_priority
from backend.providers.runtime import Capability, ModelRuntime, ResolvedModel
from backend.providers.types import ModelReference
from backend.utils import dumps_json, loads_json, utcnow

MIN_PAPERS = 8
MAX_TOPICS = 12
EXCERPT_CHARS = 1800
EMBED_BATCH = 16
STORE_DIR = "paper_topics"
STORE_LIMIT = 256 * 1024 * 1024
TITLES_PER_TOPIC = 10

TOPIC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["topics"],
    "properties": {
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "name", "description"],
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                },
            },
        },
    },
}


def scope_key(folder_id: str | None) -> str:
    return f"folder:{folder_id}" if folder_id else "all"


def cluster_papers(vectors: np.ndarray, *, max_topics: int = MAX_TOPICS, seed: int = 7) -> list[list[int]]:
    """Spherical k-means sized for browsing.

    Libraries usually sit inside one field, where silhouette alone always prefers two huge groups.
    Aim for about sqrt(n/2) shelves, let silhouette choose only near that size, then fold
    near-empty clusters into their closest shelf.
    """
    count = len(vectors)
    if count < 2:
        return [list(range(count))]
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    if not np.all(np.isfinite(vectors)) or np.any(norms == 0):
        raise ValidationError("The embedding model returned unusable vectors.")
    unit = vectors / norms
    similarity = unit @ unit.T
    upper = min(max_topics, max(2, count // 3))
    target = min(upper, max(2, round((count / 2) ** 0.5)))
    best: tuple[float, np.ndarray] | None = None
    for k in range(max(2, target - 2), min(upper, target + 2) + 1):
        labels = _merge_small(unit, _best_kmeans(unit, k, seed), max(2, count // 40))
        score = _silhouette(similarity, labels)
        if best is None or score > best[0]:
            best = (score, labels)
    assert best is not None
    labels = best[1]
    groups = [sorted(np.flatnonzero(labels == label).tolist()) for label in np.unique(labels)]
    return sorted((group for group in groups if group), key=lambda group: (-len(group), group[0]))


def _best_kmeans(unit: np.ndarray, k: int, seed: int, restarts: int = 6) -> np.ndarray:
    best: tuple[float, np.ndarray] | None = None
    for attempt in range(restarts):
        labels = _kmeans(unit, k, seed + 101 * attempt)
        cohesion = sum(
            float(np.linalg.norm(unit[labels == cluster].sum(axis=0))) for cluster in np.unique(labels)
        )
        if best is None or cohesion > best[0]:
            best = (cohesion, labels)
    assert best is not None
    return best[1]


def _merge_small(unit: np.ndarray, labels: np.ndarray, minimum: int) -> np.ndarray:
    labels = labels.copy()
    while True:
        clusters, sizes = np.unique(labels, return_counts=True)
        if len(clusters) <= 2 or sizes.min() >= minimum:
            return labels
        small = clusters[np.argmin(sizes)]
        others = [cluster for cluster in clusters if cluster != small]
        centroids = np.array([unit[labels == cluster].mean(axis=0) for cluster in others])
        members = np.flatnonzero(labels == small)
        labels[members] = np.array(others)[np.argmax(unit[members] @ centroids.T, axis=1)]


def _kmeans(unit: np.ndarray, k: int, seed: int, iterations: int = 40) -> np.ndarray:
    rng = np.random.default_rng(seed + k)
    centers = [unit[rng.integers(len(unit))]]
    for _ in range(1, k):
        distance = 1 - np.max(unit @ np.array(centers).T, axis=1)
        distance = np.clip(distance, 0, None)
        total = distance.sum()
        index = rng.choice(len(unit), p=distance / total) if total > 0 else rng.integers(len(unit))
        centers.append(unit[index])
    centroids = np.array(centers)
    labels = np.zeros(len(unit), dtype=int)
    for step in range(iterations):
        updated = np.argmax(unit @ centroids.T, axis=1)
        if step and np.array_equal(updated, labels):
            break
        labels = updated
        for cluster in range(k):
            members = unit[labels == cluster]
            if len(members):
                center = members.sum(axis=0)
                centroids[cluster] = center / (np.linalg.norm(center) or 1)
    return labels


def _silhouette(similarity: np.ndarray, labels: np.ndarray) -> float:
    distance = 1 - similarity
    clusters = np.unique(labels)
    if len(clusters) < 2:
        return -1.0
    scores = np.zeros(len(labels))
    for index, label in enumerate(labels):
        own = labels == label
        own_count = own.sum() - 1
        if own_count <= 0:
            continue
        inside = (distance[index, own].sum()) / own_count
        outside = min(distance[index, labels == other].mean() for other in clusters if other != label)
        scores[index] = (outside - inside) / max(inside, outside, 1e-9)
    return float(scores.mean())


class PaperTopicService:
    def __init__(
        self, documents: DocumentService, runtime: ModelRuntime,
        storage: SafeStorage, prompts: PromptRegistry,
    ) -> None:
        self.documents = documents
        self.runtime = runtime
        self.storage = storage
        self.prompts = prompts
        self._lock = asyncio.Lock()

    # Persistence: application state under the data directory, never the user's workspace.
    def _read(self, name: str) -> dict[str, Any]:
        try:
            raw = self.storage.read_stored_bytes(
                self.storage.settings.data_dir, f"{STORE_DIR}/{name}", max_bytes=STORE_LIMIT,
            )
        except FileNotFoundError:
            return {}
        value = loads_json(raw.decode("utf-8"), default={})
        return value if isinstance(value, dict) else {}

    def _write(self, name: str, value: dict[str, Any]) -> None:
        self.storage.write_bytes_atomic(
            self.storage.settings.data_dir, f"{STORE_DIR}/{name}",
            dumps_json(value).encode("utf-8"), max_bytes=STORE_LIMIT,
        )

    def get(self, folder_id: str | None) -> dict[str, Any] | None:
        saved = self._read("maps.json").get(scope_key(folder_id))
        if not saved:
            return None
        existing = {document.id for document in self._scope(folder_id)}
        topics = [
            {**topic, "paper_ids": [paper for paper in topic["paper_ids"] if paper in existing]}
            for topic in saved["topics"]
        ]
        return {**saved, "paper_ids": [paper for paper in saved["paper_ids"] if paper in existing], "topics": topics}

    def _scope(self, folder_id: str | None) -> list[Any]:
        papers = self.documents.list_documents()
        if folder_id is None:
            return papers
        if folder_id not in {folder.id for folder in self.documents.list_folders()}:
            raise ValidationError("Paper folder was not found.")
        return [paper for paper in papers if (paper.metadata_json or {}).get("folder_id") == folder_id]

    def _text(self, paper: Any) -> str:
        try:
            excerpt = self.documents.summary_excerpt(paper.id, EXCERPT_CHARS)
            body = " ".join(" ".join(chunk["text"].split()) for chunk in excerpt["chunks"])[:EXCERPT_CHARS]
        except DocumentProcessingError:
            body = ""
        return f"title: {paper.title} | text: {body or paper.title}"

    def _resolve(self, capability: Capability, reference: ModelReferenceSpec) -> ResolvedModel:
        return self.runtime.resolve(capability, model_reference=ModelReference(
            reference.provider_profile_id, reference.model,
        ))

    async def build(
        self, folder_id: str | None, embedding_model: ModelReferenceSpec, naming_model: ModelReferenceSpec,
    ) -> dict[str, Any]:
        if self._lock.locked():
            raise ConflictError("Topics are already being grouped. Wait for that run to finish.")
        async with self._lock:
            papers = self._scope(folder_id)
            saved = self.get(folder_id)
            if saved and saved["topics"]:
                return await self._enrich(folder_id, papers, saved, embedding_model)
            if len(papers) < MIN_PAPERS:
                raise ValidationError(f"Group papers by topic once this view has at least {MIN_PAPERS} papers.")
            embedder = self._resolve("embedding", embedding_model)
            namer = self._resolve("chat", naming_model)
            vectors = await self._embed(embedder, papers)
            groups = await asyncio.to_thread(cluster_papers, np.array(vectors, dtype=np.float64))
            clusters = [
                {"id": f"t{index + 1}", "papers": [papers[item] for item in group]}
                for index, group in enumerate(groups)
            ]
            names = await self._name(namer, clusters)
            result = {
                "folder_id": folder_id,
                "created_at": utcnow().isoformat(),
                "paper_ids": [paper.id for paper in papers],
                "topics": [{
                    "id": cluster["id"], "name": names[cluster["id"]]["name"],
                    "description": names[cluster["id"]]["description"],
                    "paper_ids": [paper.id for paper in cluster["papers"]],
                } for cluster in clusters],
                "embedding_model": {"provider_profile_id": embedder.profile_id, "model": embedder.model},
                "naming_model": {"provider_profile_id": namer.profile_id, "model": namer.model},
            }
            self._save_vectors(folder_id, result["embedding_model"], dict(zip(result["paper_ids"], vectors, strict=True)))
            self._save_map(folder_id, result)
            return result

    def _save_map(self, folder_id: str | None, result: dict[str, Any]) -> None:
        maps = self._read("maps.json")
        maps[scope_key(folder_id)] = result
        self._write("maps.json", maps)

    def _save_vectors(self, folder_id: str | None, model: dict[str, Any], vectors: dict[str, Any]) -> None:
        saved = self._read("vectors.json")
        saved[scope_key(folder_id)] = {"model": model, "papers": vectors}
        self._write("vectors.json", saved)

    async def _enrich(
        self, folder_id: str | None, papers: list[Any], saved: dict[str, Any],
        embedding_model: ModelReferenceSpec,
    ) -> dict[str, Any]:
        grouped = {paper for topic in saved["topics"] for paper in topic["paper_ids"]}
        new_papers = [paper for paper in papers if paper.id not in grouped]
        if not new_papers:
            return saved
        reference = embedding_model if embedding_model.model or embedding_model.provider_profile_id else (
            ModelReferenceSpec.model_validate(saved["embedding_model"])
        )
        embedder = self._resolve("embedding", reference)
        model = {"provider_profile_id": embedder.profile_id, "model": embedder.model}
        cached = self._read("vectors.json").get(scope_key(folder_id), {})
        vectors = cached.get("papers", {}) if cached.get("model") == model else {}
        vectors = {paper.id: vectors[paper.id] for paper in papers if paper.id in vectors}
        missing = [paper for paper in papers if paper.id not in vectors]
        if missing:
            vectors.update(zip([paper.id for paper in missing], await self._embed(embedder, missing), strict=True))
        units = self._unit_vectors([vectors[paper.id] for paper in papers])
        by_id = dict(zip([paper.id for paper in papers], units, strict=True))
        centers = []
        for topic in saved["topics"]:
            members = [by_id[paper] for paper in topic["paper_ids"]]
            if members:
                center = np.mean(members, axis=0)
            else:
                # Empty shelves retain their identity and can attract papers again.
                text = f"{topic['name']}: {topic['description']}"
                center = self._unit_vectors(await self._embed_texts(embedder, [text]))[0]
            centers.append(center)
        centers_array = self._unit_vectors(centers)
        assignments = np.argmax(np.array([by_id[paper.id] for paper in new_papers]) @ centers_array.T, axis=1)
        for paper, index in zip(new_papers, assignments, strict=True):
            saved["topics"][int(index)]["paper_ids"].append(paper.id)
        saved["paper_ids"] = [paper.id for paper in papers]
        saved["embedding_model"] = model
        self._save_vectors(folder_id, model, vectors)
        self._save_map(folder_id, saved)
        return saved

    @staticmethod
    def _unit_vectors(vectors: Any) -> np.ndarray:
        try:
            array = np.asarray(vectors, dtype=np.float64)
        except (TypeError, ValueError) as exc:
            raise ValidationError("The embedding model returned inconsistent vector sizes.") from exc
        if array.ndim != 2 or not array.shape[1] or not np.all(np.isfinite(array)):
            raise ValidationError("The embedding model returned unusable vectors.")
        norms = np.linalg.norm(array, axis=1, keepdims=True)
        if np.any(norms == 0):
            raise ValidationError("The embedding model returned unusable vectors.")
        return array / norms

    async def assign(self, folder_id: str | None, topic_id: str, paper_ids: list[str]) -> dict[str, Any]:
        if self._lock.locked():
            raise ConflictError("Topics are already being updated. Wait for that run to finish.")
        async with self._lock:
            saved = self.get(folder_id)
            if not saved or topic_id not in {topic["id"] for topic in saved["topics"]}:
                raise ValidationError("Paper topic was not found in this view.")
            selected = set(paper_ids)
            if not selected or not selected <= {paper.id for paper in self._scope(folder_id)}:
                raise ValidationError("Choose existing papers from this topic view.")
            for topic in saved["topics"]:
                topic["paper_ids"] = [paper for paper in topic["paper_ids"] if paper not in selected]
                if topic["id"] == topic_id:
                    topic["paper_ids"].extend(dict.fromkeys(paper_ids))
            saved["paper_ids"] = list(dict.fromkeys(paper for topic in saved["topics"] for paper in topic["paper_ids"]))
            self._save_map(folder_id, saved)
            return saved

    def clear(self, folder_id: str | None) -> None:
        if self._lock.locked():
            raise ConflictError("Topics are already being updated. Wait for that run to finish.")
        maps = self._read("maps.json")
        if maps.pop(scope_key(folder_id), None) is not None:
            self._write("maps.json", maps)

    async def _embed(self, resolved: ResolvedModel, papers: list[Any]) -> list[list[float]]:
        texts = await asyncio.to_thread(lambda: [self._text(paper) for paper in papers])
        return await self._embed_texts(resolved, texts)

    async def _embed_texts(self, resolved: ResolvedModel, texts: list[str]) -> list[list[float]]:
        """Cache embeddings per text and model, including upgrades of older saved maps."""
        cache = self._read("embeddings.json")
        model_key = f"{resolved.profile_id}/{resolved.model}"
        keys = [hashlib.sha256(f"{model_key}\n{text}".encode()).hexdigest() for text in texts]
        missing = [index for index, key in enumerate(keys) if key not in cache]
        for offset in range(0, len(missing), EMBED_BATCH):
            batch = missing[offset:offset + EMBED_BATCH]
            with inference_priority("background"):
                vectors = await self.runtime.embed(resolved, [texts[index] for index in batch])
            if len(vectors) != len(batch):
                raise ValidationError("The embedding model did not return one vector per paper.")
            for index, vector in zip(batch, vectors, strict=True):
                cache[keys[index]] = [round(value, 6) for value in vector]
        vectors = [cache[key] for key in keys]
        self._unit_vectors(vectors)
        if missing:
            self._write("embeddings.json", cache)
        return vectors

    async def _name(self, resolved: ResolvedModel, clusters: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
        packet = {"clusters": [{
            "id": cluster["id"],
            "paper_count": len(cluster["papers"]),
            "titles": [paper.title for paper in cluster["papers"][:TITLES_PER_TOPIC]],
        } for cluster in clusters]}
        expected = {cluster["id"] for cluster in clusters}
        system = self.prompts.render("paper-topics")
        last_error = "The naming model did not return topic names."
        for _attempt in range(2):
            response = await self.runtime.generate(
                resolved, json.dumps(packet, ensure_ascii=False), system=system,
                format_=TOPIC_SCHEMA, temperature=0.2, think=False, max_output_tokens=4096,
            )
            text = response.get("response") or response.get("message", {}).get("content") or ""
            try:
                parsed = json.loads(_strip_fence(text))
                rows = {row["id"]: row for row in parsed["topics"]}
                if set(rows) != expected:
                    raise ValueError("Name every cluster exactly once.")
                return {
                    key: {
                        "name": " ".join(str(row["name"]).split())[:80] or key,
                        "description": " ".join(str(row["description"]).split())[:300],
                    }
                    for key, row in rows.items()
                }
            except (ValueError, KeyError, TypeError) as exc:
                last_error = f"The naming model returned unusable topic names: {exc}"
        raise ValidationError(last_error)


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        stripped = stripped.rsplit("```", 1)[0]
    return stripped
