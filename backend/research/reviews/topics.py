from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING

from backend.agents.blueprint import ModelReferenceSpec
from backend.core.errors import ValidationError
from backend.providers.inference import inference_priority
from backend.providers.types import ModelReference
from backend.research.reviews.jobs import digest
from backend.research.reviews.schemas import BriefPacket, Candidate, ResearchTopic, SearchQuery, TopicLabels, TopicSearchPlan, UnderstandingSnapshot

if TYPE_CHECKING:
    from backend.research.reviews.service import LiteratureReviewService


def cluster_embeddings(vectors: list[list[float]]) -> list[list[int]]:
    """Deterministic average-link cosine groups, with a bounded topic menu."""
    if not vectors:
        return []
    dimensions = len(vectors[0])
    normalized = []
    for vector in vectors:
        if not dimensions or len(vector) != dimensions or any(not math.isfinite(value) for value in vector):
            raise ValidationError("The embedding model returned invalid or inconsistent vectors.")
        norm = math.sqrt(sum(value * value for value in vector))
        if not math.isfinite(norm) or norm == 0:
            raise ValidationError("The embedding model returned an unusable zero-length vector.")
        normalized.append([value / norm for value in vector])
    similarities = {
        (left, right): sum(a * b for a, b in zip(normalized[left], normalized[right], strict=True))
        for left in range(len(vectors)) for right in range(left + 1, len(vectors))
    }
    groups = [[index] for index in range(len(vectors))]
    while len(groups) > 1:
        def similarity(left: list[int], right: list[int]) -> float:
            return sum(similarities[min(a, b), max(a, b)] for a in left for b in right) / (len(left) * len(right))

        score, left, right = max(
            (similarity(groups[left], groups[right]), -left, -right)
            for left in range(len(groups)) for right in range(left + 1, len(groups))
        )
        if score < 0.72 and len(groups) <= 12:
            break
        left, right = -left, -right
        groups[left] = sorted([*groups[left], *groups[right]])
        groups.pop(right)
    return groups


class TopicResearch:
    def __init__(self, reviews: LiteratureReviewService) -> None:
        self.reviews = reviews

    def write(self, review_id: str, relative_path: str, content: object) -> str:
        service = self.reviews
        storage = service.repository.storage
        if storage is None:
            raise ValidationError("Research workspace storage is unavailable.")
        root = service.repository.get(review_id).state["workspace_path"]
        path = f"{root}/{relative_path}"
        manifest = service.repository.job(review_id, "workspace-files") or {"key": "workspace-files", "files": {}}
        previous = manifest["files"].get(path)
        if previous:
            try:
                current = storage.file_hash(storage.settings.workspace_dir, path)
            except FileNotFoundError:
                current = None
            if current is not None and current != previous:
                raise ValidationError(f"Research file was edited outside this task: {path}. Preserve the edit before resuming.")
        if isinstance(content, bytes):
            if relative_path.lower().endswith(".pdf"):
                stored = storage.write_workspace_document(path, content)
            else:
                stored = storage.write_bytes_atomic(storage.settings.workspace_dir, path, content)
        else:
            stored = storage.write_workspace_file(path, content)
        manifest["files"][path] = stored.sha256
        service.repository.save_job(review_id, "workspace-files", "workspace", manifest)
        return path

    def topics(self, review_id: str) -> list[ResearchTopic]:
        saved = self.reviews.repository.job(review_id, "topics")
        return [ResearchTopic.model_validate(row) for row in saved["topics"]] if saved else []

    async def search_queries(self, review_id: str) -> list[SearchQuery]:
        service = self.reviews
        state = service.repository.get(review_id).state
        if state.get("topic_discovery_queries"):
            return [SearchQuery.model_validate(row) for row in state["topic_discovery_queries"]]
        spec = service._spec(review_id)
        providers = ["library", *(["arxiv", "web"] if spec.web_enabled else [])]
        if service._openalex_enabled(spec):
            providers.append("openalex")
        searches = [
            {"provider": row["provider"], "query": row["query"]}
            for row in service.repository.jobs(review_id, "discovery") if row.get("query")
        ]
        previous = {(row["provider"], " ".join(row["query"].split()).casefold()) for row in searches}
        prepared = service.jobs.prepare(spec.main_model, "literature-topic-search", TopicSearchPlan)

        def validate(result: dict) -> None:
            seen = set(previous)
            for query in result["queries"]:
                key = (query["provider"], " ".join(query["query"].split()).casefold())
                if query["provider"] not in providers or query["pages"] != 1 or key in seen:
                    raise ValidationError("Use enabled sources with distinct new queries and one page per query.")
                seen.add(key)

        result = await service.jobs.call(review_id, "discovery", f"topic-search:{state['topic_round']}", {
            **service._frame(review_id), "providers": providers,
            "covered_topics": [{"title": row.title, "description": row.description} for row in self.topics(review_id)],
            "previous_searches": searches, "feedback": state.get("topic_feedback"),
            "max_additional_papers": 100,
        }, prepared, TopicSearchPlan, validate)
        service.jobs.checkpoint(review_id, topic_discovery_queries=result["queries"])
        return [SearchQuery.model_validate(row) for row in result["queries"]]

    async def categorize(self, review_id: str) -> None:
        service = self.reviews
        spec = service._spec(review_id)
        role = spec.embedding_model
        if role is None:
            raise ValidationError("Choose an embedding model before grouping papers.")
        candidates = service._candidates(review_id)
        missing_abstracts = sum(not row.abstract for row in candidates)
        if missing_abstracts:
            service._notice(review_id, f"{missing_abstracts} discovered papers have no abstract; those embeddings use titles only.")
        corpus_hash = digest([row.model_dump(mode="json", exclude={"selected", "error"}) for row in candidates])
        saved_topics = service.repository.job(review_id, "topics")
        if saved_topics and saved_topics.get("corpus_hash") == corpus_hash:
            if service.repository.get(review_id).state.get("topic_action") == "more":
                service._notice(review_id, "No additional papers were found; the existing topic choices are unchanged.")
            self.write(review_id, "topics.json", {"embedding_model": role.model_dump(mode="json"), "topics": saved_topics["topics"]})
            self.write(review_id, "candidates.json", [row.model_dump(mode="json") for row in candidates])
            return
        existing = self.topics(review_id)
        covered = {identifier for topic in existing for identifier in topic.candidate_ids}
        new_candidates = [row for row in candidates if row.id not in covered]
        if not new_candidates:
            service._notice(review_id, "No additional papers were found; the existing topic choices are unchanged.")
        resolved = service.jobs.runtime.resolve(
            "embedding", model_reference=ModelReference.model_validate(role.model_reference.model_dump()),
        )
        vectors = []
        for offset in range(0, len(new_candidates), role.batch_size):
            batch = new_candidates[offset:offset + role.batch_size]
            inputs = [f"{row.title}\n\n{row.abstract or ''}" for row in batch]
            key = f"embeddings:{digest([role.model_dump(mode='json'), inputs, resolved.base_url])}"
            saved = service.repository.job(review_id, key)
            if saved is None:
                service.jobs.reserve(review_id, 0)
                with inference_priority("background"):
                    embedded = await service.jobs.runtime.embed(resolved, inputs)
                if len(embedded) != len(inputs):
                    raise ValidationError("The embedding model did not return one vector per paper.")
                cluster_embeddings(embedded)
                saved = {"key": key, "status": "completed", "vectors": embedded}
                service.repository.save_job(review_id, key, "screening", saved)
                service.jobs.checkpoint(review_id)
                service._publish_progress(review_id)
            vectors.extend(saved["vectors"])
        groups = cluster_embeddings(vectors)
        records = [{
            "id": f"topic-{len(existing) + index + 1}",
            "candidate_ids": [new_candidates[item].id for item in group],
            "paper_count": len(group),
            "papers": [{"candidate_id": new_candidates[item].id, "title": new_candidates[item].title,
                        "abstract": new_candidates[item].abstract} for item in group[:3]],
            "sample_only": len(group) > 3,
        } for index, group in enumerate(groups)]
        naming_reference = service.jobs.runtime.settings.default_model_references.get("small_tasks")
        naming_model = ModelReferenceSpec.model_validate(naming_reference) if naming_reference else spec.main_model
        prepared = service.jobs.prepare(naming_model, "literature-topics", TopicLabels)
        labels = {}
        for packet in service.jobs.packets(records, service._frame(review_id), prepared):
            offered = {row["id"] for row in packet["records"]}

            def validate(result: dict) -> None:
                ids = [row["id"] for row in result["topics"]]
                if len(ids) != len(offered) or set(ids) != offered:
                    raise ValidationError("Name every supplied embedding topic exactly once without moving papers.")

            result = await service.jobs.call(review_id, "screening", "topic-labels", packet, prepared, TopicLabels, validate)
            labels.update({row["id"]: row for row in result["topics"]})
        topics = [row.model_dump(mode="json") for row in existing] + [ResearchTopic(
            **labels[row["id"]], candidate_ids=row["candidate_ids"],
        ).model_dump(mode="json") for row in records]
        service.repository.save_job(review_id, "topics", "screening", {
            "key": "topics", "topics": topics, "corpus_hash": corpus_hash,
        })
        service.jobs.checkpoint(
            review_id, selected_topic_ids=service.repository.get(review_id).state.get("selected_topic_ids", []),
        )
        self.write(review_id, "topics.json", {"embedding_model": role.model_dump(mode="json"), "topics": topics})
        self.write(review_id, "candidates.json", [row.model_dump(mode="json") for row in candidates])

    def prune_selection(self, review_id: str) -> None:
        service = self.reviews
        storage = service.repository.storage
        state = service.repository.get(review_id).state
        root = state["workspace_path"]
        selected_ids = set(state["selected_topic_ids"])
        topics = [row for row in self.topics(review_id) if row.id in selected_ids]
        candidates = service._candidates(review_id, selected=True)
        candidate_ids = {row.id for row in candidates}
        manifest = service.repository.job(review_id, "workspace-files") or {"key": "workspace-files", "files": {}}
        discard = {
            path: expected for path, expected in manifest["files"].items()
            if (path.startswith(f"{root}/topics/") and path.removeprefix(f"{root}/topics/").removesuffix(".md") not in selected_ids)
            or (path.startswith(f"{root}/papers/") and path.removeprefix(f"{root}/papers/").split("/")[0] not in candidate_ids)
        }
        with service.workspace.mutation_lock:
            # Preflight before pruning: a user edit must block cleanup, never be silently deleted.
            receipts = {
                path: path.rsplit("-", 1)[-1].removesuffix(".json")
                for path in storage.list_workspace_files() if path.startswith(f"{root}/receipts/")
            }
            for path, expected in {**discard, **receipts}.items():
                try:
                    current = storage.file_hash(storage.settings.workspace_dir, path)
                except FileNotFoundError:
                    continue
                if current != expected:
                    raise ValidationError(f"Research cleanup found an edited file: {path}. Preserve the edit outside this task before resuming.")
            self.write(review_id, "topics.json", {"topics": [row.model_dump(mode="json") for row in topics]})
            self.write(review_id, "candidates.json", [row.model_dump(mode="json") for row in candidates])
            service.repository.save_job(review_id, "topics", "screening", {
                "key": "topics", "topics": [row.model_dump(mode="json") for row in topics],
                "corpus_hash": digest([row.model_dump(mode="json", exclude={"selected", "error"}) for row in candidates]),
            })
            service.repository.prune_topic_selection(review_id, selected_ids)
            for path in discard:
                if path.endswith(".pdf"):
                    storage.delete_stored_file(storage.settings.workspace_dir, path)
                else:
                    try:
                        service.workspace.delete_file(path)
                    except FileNotFoundError:
                        pass
            manifest = service.repository.job(review_id, "workspace-files")
            manifest["files"] = {path: expected for path, expected in manifest["files"].items() if path not in discard}
            service.repository.save_job(review_id, "workspace-files", "workspace", manifest)
            retained = service.repository.workspace_receipts(review_id)
            for path in storage.list_workspace_files():
                if path.startswith(f"{root}/receipts/") and path not in retained:
                    expected = path.rsplit("-", 1)[-1].removesuffix(".json")
                    if storage.file_hash(storage.settings.workspace_dir, path) != expected:
                        raise ValidationError(f"Research cleanup found an edited receipt: {path}. Preserve it outside this task before resuming.")
                    service.workspace.delete_file(path)
        service.jobs.checkpoint(review_id, topic_cleanup_pending=False, topic_selection_pruned=True)

    async def explore(self, review_id: str) -> None:
        service = self.reviews
        state = service.repository.get(review_id).state
        selected = set(state["selected_topic_ids"])
        candidates = {row.id: row for row in service._candidates(review_id)}
        prepared = service.jobs.prepare(service._spec(review_id).main_model, "literature-topic-explore")
        topics = self.topics(review_id)
        # Explore every selected topic in the same run before returning to the user.
        for topic in topics:
            if topic.id not in selected:
                continue
            key = f"topic-exploration:{state['topic_round']}:{topic.id}"
            completed = service.repository.job(review_id, key)
            if completed:
                topic.exploration = completed["exploration"]
                continue
            frame = {
                **service._frame(review_id), "topic": topic.title,
                "feedback": state.get("topic_feedback", ""), "previous_exploration": topic.exploration,
            }
            parts = []
            for packet in service.jobs.packets([
                {"candidate_id": identifier, "title": candidates[identifier].title,
                 "abstract": candidates[identifier].abstract} for identifier in topic.candidate_ids
            ], frame, prepared):
                parts.append(await service.jobs.call(review_id, "screening", "topic-explore", packet, prepared))
            topic.exploration = "\n\n".join(parts)
            service.repository.save_job(review_id, key, "screening", {
                "key": key, "status": "completed", "exploration": topic.exploration,
            })
            self.write(review_id, f"topics/{topic.id}.md", f"# {topic.title}\n\n{topic.exploration}")
            service.repository.save_job(review_id, "topics", "screening", {
                **service.repository.job(review_id, "topics"), "topics": [row.model_dump(mode="json") for row in topics],
            })
            service.jobs.checkpoint(review_id)
            service._publish_progress(review_id)
        service.repository.save_job(review_id, "topics", "screening", {
            **service.repository.job(review_id, "topics"), "topics": [row.model_dump(mode="json") for row in topics],
        })
        self.write(review_id, "topics.json", {"topics": [row.model_dump(mode="json") for row in topics]})
        service.jobs.checkpoint(review_id, topic_feedback=None, topic_action=None)

    def capture_paper(self, review_id: str, candidate: Candidate) -> None:
        service = self.reviews
        artifact = next((row for row in service.documents.get_document_artifacts(candidate.document_id)
                         if row.kind == "source_pdf"), None)
        if artifact is None:
            raise ValidationError("The collected paper has no original PDF.")
        self.write(review_id, f"papers/{candidate.id}/source.pdf", service.documents.artifact_bytes(artifact))
        pages, manifest, revision = service._paper(candidate)
        self.write(review_id, f"papers/{candidate.id}/source.json", {
            "candidate": candidate.model_dump(mode="json"), "revision": revision, "pages": pages, "manifest": manifest,
        })

    def save_summary(self, review_id: str, candidate: Candidate, summary: dict) -> None:
        body = f"# {candidate.title}\n\nPaper ID: `{candidate.id}`\n\n"
        body += "\n\n".join(f"## {row['title']}\n\n{row['content']}" for row in summary["sections"])
        body += "\n\n## Coverage and limitations\n\n" + "\n".join(f"- {item}" for item in summary["limitations"])
        if summary.get("reading_mode") == "targeted":
            body += "\n\n## Verified source quotations\n\n" + "\n".join(
                f"- `{claim['id']}`, p.{claim['page']}: {claim['finding']} — “{claim['quote']}”"
                for claim in summary["findings"]
            )
        self.write(review_id, f"papers/{candidate.id}/summary.md", body)

    @staticmethod
    def targeted_spans(pages: list[dict], question: str) -> list[dict]:
        terms = set(re.findall(r"\w{3,}", question.casefold())) - {
            "the", "and", "for", "with", "this", "that", "what", "how", "paper", "research",
        }
        spans = []
        for page in pages:
            text = page["text"]
            start = 0
            while start < len(text):
                end = min(start + 1800, len(text))
                if end < len(text):
                    # Prefer paragraph/sentence boundaries, keeping exact source offsets.
                    paragraphs = [match.end() for match in re.finditer(r"\n\s*\n", text[start:end]) if match.end() >= 900]
                    sentences = [match.end() for match in re.finditer(r"(?<=[.!?])\s+", text[start:end]) if match.end() >= 900]
                    usable = paragraphs or sentences
                    if usable:
                        end = start + usable[-1]
                if text[start:end].strip():
                    spans.append({"page": page["page"], "start": start, "end": end, "text": text[start:end]})
                start = end
        ranked = sorted(spans, key=lambda span: (
            -len(terms & set(re.findall(r"\w{3,}", span["text"].casefold()))),
            span["page"], span["start"],
        ))
        return sorted(ranked[:6], key=lambda span: (span["page"], span["start"]))

    def understanding(self, review_id: str) -> UnderstandingSnapshot | None:
        self.reviews.repository.get(review_id)
        saved = self.reviews.repository.job(review_id, "understanding")
        return UnderstandingSnapshot.model_validate(saved["snapshot"]) if saved else None

    async def refine_understanding(self, review_id: str) -> None:
        service = self.reviews
        state = service.repository.get(review_id).state
        records = []
        for candidate in service._candidates(review_id, selected=True):
            brief = service.paper(review_id, candidate.id).brief
            if brief and brief.question_focus == state.get("question_focus"):
                records.append({"candidate_id": candidate.id, "title": candidate.title,
                                **brief.model_dump(mode="json")})
        if not records:
            raise ValidationError("Read a focused paper batch before comparing evidence.")
        frame = {**service._frame(review_id), "stage": "summary_inferences",
                 "instruction": "Use headings Agreements, Disagreements, Open questions, and Evidence coverage. "
                                "Compare papers explicitly. Distinguish inference from verified source quotations."}
        prepared = service.jobs.prepare(service._spec(review_id).main_model, "literature-topic-synthesis")
        fingerprint = digest([records, frame, prepared[0].entry_agent.instructions,
                              service._spec(review_id).main_model.model_dump(mode="json")])
        previous = self.understanding(review_id)
        if previous and previous.fingerprint == fingerprint:
            return
        packets = service.jobs.packets(records, frame, prepared)
        service.jobs.admit_work(review_id, "comparison_passes", fingerprint)
        parts = []
        for packet in packets:
            parts.append(await service.jobs.call(review_id, "synthesis", "workspace-understanding", packet, prepared))
        # A context-limited comparison is labelled per packet rather than pretending it covered every relationship.
        markdown = "\n\n".join(
            f"## Comparison group {index + 1}\n\n{text}" for index, text in enumerate(parts)
        )
        snapshot = UnderstandingSnapshot(
            markdown=markdown, candidate_ids=[row["candidate_id"] for row in records],
            question_focus=state.get("question_focus"), fingerprint=fingerprint,
        )
        service.repository.save_job(review_id, "understanding", "synthesis", {
            "key": "understanding", "snapshot": snapshot.model_dump(mode="json"),
        })
        self.write(review_id, "understanding.md", "# Cross-paper understanding\n\n"
                   "Model inferences from selected verified excerpts; not exhaustive source coverage.\n\n" + markdown)

    async def brief(self, review_id: str, candidate: Candidate) -> None:
        service = self.reviews
        spec = service._spec(review_id)
        role = spec.summary_model
        pages, _, revision = service._paper(candidate)
        frame = {
            **service._frame(review_id), **revision, "document_id": candidate.document_id, "title": candidate.title,
            "summary_method": "targeted-v2",
            "summary_role": role.model_dump(mode="json", exclude={"parallel_requests"}),
        }
        prepared = service.jobs.prepare(
            role.model_reference, "literature-evidence", BriefPacket,
            max_output_tokens=min(role.max_output_tokens, 4096), reasoning_effort=role.reasoning_effort,
        )
        binding = prepared[0].entry_agent.binding
        fingerprint = digest([
            frame, prepared[0].entry_agent.instructions, BriefPacket.model_json_schema(),
            prepared[0].context_window_tokens, binding.api_mode, binding.provider_kind,
            digest(str(binding.client.base_url)),
        ])
        key = f"paper-brief:{candidate.id}:{revision['source_version']}:{fingerprint}"
        plan = next(row for row in service.repository.paper_jobs(review_id, candidate.id)
                    if row["key"].startswith(f"reading-plan:{candidate.id}:{revision['source_version']}:"))
        plan["brief_key"] = key
        spans = self.targeted_spans(pages, " ".join([
            spec.objective, frame.get("question_focus") or "", *frame["questions"],
        ]))
        if not spans:
            raise ValidationError("No readable source spans are available for a targeted brief.")
        admitted = {span["page"] for span in spans}
        plan["pages"] = [
            {"page": row["page"], "action": "read" if row["page"] in admitted else "defer",
             "reason": "Bounded question-focused excerpts only; not a full-paper read."
                       if row["page"] in admitted else "Outside this targeted brief's bounded coverage.",
             "reference_page": False}
            for row in pages
        ]
        service.repository.save_job(review_id, plan["key"], "reading_plan", plan)
        if service.repository.job(review_id, key) is None:
            service._paper_operation(review_id, candidate.id, "Reading question-focused source passages")
            packets = service.jobs.packets(spans, frame, prepared)
            receipts = []
            for packet in packets:
                def validate(result: dict) -> None:
                    if len(result["sections"]) != 5 or {section["title"] for section in result["sections"]} != {
                        "Fit to objective", "Approach", "Pertinent findings",
                        "Limitations and unknowns", "Why and when to read",
                    }:
                        raise ValidationError("A brief must address fit, approach, findings, unknowns, and reading advice.")
                    for claim in result["claims"]:
                        if not any(span["page"] == claim["page"] and claim["quote"].strip()
                                   and claim["quote"] in span["text"] for span in packet["records"]):
                            raise ValidationError("Brief claims must quote an admitted source page verbatim.")
                    for section in result["sections"]:
                        if any(index < 0 or index >= len(result["claims"]) for index in section["claim_indexes"]):
                            raise ValidationError("Brief sections must cite supplied claim indexes.")
                result = await service.jobs.call(
                    review_id, "reading", f"targeted-brief:{candidate.document_id}",
                    packet, prepared, BriefPacket, validate,
                )
                receipts.append(service._save_evidence_receipt(review_id, candidate, revision, packet, {
                    "claims": result["claims"], "follow_up_pages": [], "limitations": result["limitations"],
                    "partial_summary": result["sections"],
                }))
            sections = [{
                "title": title,
                "content": "\n\n".join(dict.fromkeys(section["content"] for receipt in receipts
                                                    for section in receipt["partial_summary"] if section["title"] == title)),
                "evidence_ids": list(dict.fromkeys(identifier for receipt in receipts
                                                  for section in receipt["partial_summary"] if section["title"] == title
                                                  for identifier in section["evidence_ids"])),
            } for title in dict.fromkeys(section["title"] for receipt in receipts for section in receipt["partial_summary"])]
            summary = {
                "sections": sections, "source_pages": sorted(admitted),
                "question_focus": frame.get("question_focus"), "reading_mode": "targeted",
                "findings": [claim for receipt in receipts for claim in receipt["claims"]],
                "limitations": list(dict.fromkeys([
                    "Targeted excerpts only, not a full-paper read. Passage selection is a lexical retrieval lead, "
                    "not a relevance judgment; missing results and limitations remain unknown. Reading advice is an inference.",
                    *(item for receipt in receipts for item in receipt["limitations"]),
                ])),
            }
            service.repository.save_job(review_id, key, "reading", {
                "key": key, "kind": "selective_summary", "status": "completed", "candidate_id": candidate.id,
                **revision, "summary": summary, "summary_method": "targeted-v2",
                "receipt_keys": [receipt["key"] for receipt in receipts],
            })
        summary = service.repository.job(review_id, key)["summary"]
        self.save_summary(review_id, candidate, summary)
        self.publish_provisional(review_id)
        service._paper_operation(review_id, candidate.id, None)

    def publish_provisional(self, review_id: str) -> None:
        service = self.reviews
        state = service.repository.get(review_id).state
        if not state.get("workspace_path"):
            return
        body = "# Provisional reading findings\n\n"
        body += "Selected-batch coverage only; incomplete work is not a final literature conclusion.\n\n"
        body += f"Question focus: {state.get('question_focus') or service._spec(review_id).objective}\n\n"
        for candidate in service._candidates(review_id, selected=True):
            paper = service.paper(review_id, candidate.id)
            summary = paper.brief.model_dump(mode="json") if paper.brief else paper.selective_summary
            body += f"## {candidate.title}\n\n"
            if summary and (
                summary.get("question_focus") == state.get("question_focus")
                or (service._spec(review_id).reading_mode == "full" and paper.brief is None)
            ):
                body += "\n\n".join(f"### {section['title']}\n\n{section['content']}" for section in summary["sections"])
                body += "\n\n" + "\n".join(f"- {item}" for item in summary["limitations"])
                body += "\n\n" + "\n".join(
                    f"- [{claim['id']}] p.{claim['page']}: {claim['finding']} — “{claim['quote']}”"
                    for claim in paper.findings
                )
                body += f"\n\n[Saved brief](papers/{candidate.id}/summary.md)\n\n"
            else:
                body += f"{paper.error or 'Pending for this question focus; no findings claimed yet.'}\n\n"
        path = self.write(review_id, "provisional.md", body)
        service.jobs.checkpoint(review_id, provisional_path=path)
        service._publish_progress(review_id)

    def unavailable_sources(self, review_id: str) -> dict[str, dict]:
        return {
            row["candidate_id"]: row for row in self.reviews.repository.jobs(review_id, key_prefix="source-unavailable:")
            if row.get("status") == "skipped"
        }

    def skip_source(self, review_id: str, candidate: Candidate, error: Exception) -> None:
        service = self.reviews
        reason = f"{type(error).__name__}: {error}"
        current = service.repository.candidate(review_id, candidate.id)
        service.repository.update_candidate(review_id, current.model_copy(update={"error": reason}))
        key = f"source-unavailable:{candidate.id}"
        service.repository.save_job(review_id, key, "reading_plan", {
            "key": key, "candidate_id": candidate.id, "title": candidate.title,
            "status": "skipped", "reason": reason,
        })
        self.write(review_id, "skipped-sources.json", list(self.unavailable_sources(review_id).values()))
        self.publish_provisional(review_id)
        service._notice(review_id, f"Skipped unreadable or unavailable paper '{candidate.title}': {reason}. Continuing with readable papers.")
        service._paper_operation(review_id, candidate.id, None)

    async def synthesize(self, review_id: str) -> None:
        service = self.reviews
        selected_ids = {row.id for row in service._candidates(review_id, selected=True)}
        unavailable = {key: value for key, value in self.unavailable_sources(review_id).items() if key in selected_ids}
        summaries = [row for row in service._analysis_summaries(review_id) if row["candidate_id"] not in unavailable]
        selected = [row for row in service._candidates(review_id, selected=True) if row.id not in unavailable]
        if not selected:
            raise ValidationError("No readable papers are available for a report. Link a readable source and resume collection.")
        if len(summaries) != len(selected):
            raise ValidationError("Finish every selected paper summary before writing the overall report, except explicitly unavailable sources. Model failures still require a retry.")
        prepared = service.jobs.prepare(service._spec(review_id).main_model, "literature-topic-synthesis")
        frame = {**service._frame(review_id), "unavailable_sources": list(unavailable.values()), "topics": [
            {"id": row.id, "title": row.title} for row in self.topics(review_id)
            if row.id in service.repository.get(review_id).state.get("selected_topic_ids", [])
        ]}
        service.jobs.admit_work(review_id, "comparison_passes", digest([summaries, frame]))
        parts = []
        ledger = ["# Research synthesis scratch ledger", "", "All entries below are model inferences from saved summaries, not verified quotations.", ""]
        for index, packet in enumerate(service.jobs.packets(summaries, {**frame, "stage": "summary_inferences"}, prepared)):
            text = await service.jobs.call(review_id, "synthesis", "topic-inferences", packet, prepared)
            path = f"scratch/inferences-{index + 1}.md"
            ids = [row["candidate_id"] for row in packet["records"]]
            source_links = "\n".join(f"- [{identifier}](../papers/{identifier}/summary.md)" for identifier in ids)
            self.write(review_id, path, f"{text}\n\n## Papers considered\n\n{source_links}\n")
            parts.append({"inferences": text, "paper_ids": ids})
            ledger.extend([f"## Batch {index + 1}", f"[Inference notes](inferences-{index + 1}.md)", f"Papers: {', '.join(ids)}", ""])
            self.write(review_id, "scratch/index.md", "\n".join(ledger))
            service.jobs.checkpoint(review_id)
            service._publish_progress(review_id)
        level = 0
        while len(parts) > 1:
            packets = service.jobs.packets(parts, {**frame, "stage": "combine_inferences"}, prepared)
            if len(packets) >= len(parts):
                raise ValidationError("The synthesis notes cannot be combined within this model context. Choose a larger main-model context.")
            level += 1
            combined = []
            for index, packet in enumerate(packets):
                text = await service.jobs.call(review_id, "synthesis", "topic-combine", packet, prepared)
                ids = list(dict.fromkeys(identifier for row in packet["records"] for identifier in row["paper_ids"]))
                path = f"scratch/combined-{level}-{index + 1}.md"
                source_links = "\n".join(f"- [{identifier}](../papers/{identifier}/summary.md)" for identifier in ids)
                self.write(review_id, path, f"{text}\n\n## Papers considered\n\n{source_links}\n")
                ledger.extend([f"## Reduction {level}.{index + 1}", f"[Combined notes](combined-{level}-{index + 1}.md)", f"Papers: {', '.join(ids)}", ""])
                self.write(review_id, "scratch/index.md", "\n".join(ledger))
                combined.append({"inferences": text, "paper_ids": ids})
            parts = combined
        report_packet = {**frame, "stage": "overall_report", "records": parts}
        report = await service.jobs.call(review_id, "synthesis", "topic-report", report_packet, prepared)
        root = service.repository.get(review_id).state["workspace_path"]
        sources = "\n".join(
            f"- [{row.title}](papers/{row.id}/summary.md) (`{row.id}`); [PDF](/library?paper={row.document_id})"
            for row in selected
        )
        body = f"# {service._spec(review_id).title}\n\n> Synthesis of saved paper summaries; model inferences are not independently source-audited quotations.\n\n{report}\n\n## Paper summaries and sources\n\n{sources}\n\n[Inference ledger](scratch/index.md)\n"
        if service._spec(review_id).reading_mode == "targeted":
            body = body.replace("Synthesis of saved paper summaries;", "Synthesis of targeted excerpts from the selected reading batch, not full-paper coverage;")
            body += "\n[Provisional findings and verified quotations](provisional.md)\n"
        if unavailable:
            body += "\n## Unavailable sources and coverage limitations\n\n"
            body += "These papers were not read or summarized and do not support the report's findings.\n\n"
            body += "\n".join(f"- {row['title']}: {row['reason']}" for row in unavailable.values()) + "\n"
        path = self.write(review_id, "report.md", body)
        snapshot = UnderstandingSnapshot(
            markdown=report, candidate_ids=[row.id for row in selected],
            question_focus=service.repository.get(review_id).state.get("question_focus"),
            fingerprint=digest([summaries, frame]),
        )
        service.repository.save_job(review_id, "understanding", "synthesis", {
            "key": "understanding", "snapshot": snapshot.model_dump(mode="json"),
        })
        service.jobs.checkpoint(review_id, output_path=path, output_hash=digest(body))
        self.write(review_id, "research.json", {
            "review_id": review_id, "workspace_path": root, "selected_topic_ids": service.repository.get(review_id).state["selected_topic_ids"],
            "main_model": service._spec(review_id).main_model.model_dump(mode="json"),
            "summary_model": service._spec(review_id).summary_model.model_dump(mode="json"),
            "report": path,
        })
