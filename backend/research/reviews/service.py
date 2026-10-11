from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import AsyncIterator
from typing import Literal
from urllib.parse import quote

import httpx
from openai import OpenAIError

from backend.core.errors import ConflictError, NotFoundError, ValidationError
from backend.documents import DocumentService
from backend.documents.formatting import manifest_pages
from backend.persistence.files import StorageError
from backend.providers.types import ModelReference, ProviderRuntimeError
from backend.research.reviews.discovery import ScholarlyDiscovery, candidate_key, normalize_candidate, normalized_doi, source_url
from backend.research.sources import _arxiv_key
from backend.research.reviews.jobs import InvalidReviewOutput, ReviewJobs, ReviewPaused, digest, json_text
from backend.research.reviews.repository import ReviewRepository
from backend.research.reviews.schemas import (
    STAGES, Candidate, CandidatePage, EvidencePacket, PaperReview, ReadingPlan, ReadingGroups, SelectiveSummary,
    DecisionRole, ReviewClearResponse, ReviewControlRequest, ReviewCreateRequest, ReviewResponse, ReviewScope, Screening,
    ScreeningBatch, Synthesis, ReferenceExtraction, ReviewReference, WorkBudget,
)
from backend.research.sources import SourceDownloadService
from backend.tracks.service import TrackService
from backend.utils import as_utc
from backend.workspace.layout import WorkspaceLayout
from backend.workspace.service import WorkspaceService

_logger = logging.getLogger(__name__)
_MAX_DISCOVERED_PAPERS = 500
_AUTO_RELEVANCE = {"HIGHLY_RELEVANT", "RELEVANT"}
_SCREEN_QUESTIONS = {
    "kind": {
        "type": "choice", "instructions": "Classify this publication from the supplied metadata.",
        "criteria": {
            "research": "original methods or results", "review": "survey or review of prior research",
            "other": "not a research publication", "unknown": "insufficient metadata",
        },
    },
    "relevance": {
        "type": "choice", "instructions": "How does the metadata relate to the research objective?",
        "criteria": {
            "HIGHLY_RELEVANT": "directly answers the research question and meets the scope",
            "RELEVANT": "useful supporting research within the scope",
            "SOMEWHAT_RELEVANT": "peripheral context or insufficient metadata; retain for human review",
            "IRRELEVANT": "clearly outside the objective or explicitly excluded by the scope",
        },
    },
}
_PAGE_QUESTIONS = {
    "priority": {
        "type": "choice", "instructions": "Is this page of an already relevant paper substantively worth reading, based on content and section function, not research-objective matching?",
        "criteria": {
            "read": "abstract, introduction, core method/setup, main results, discussion, conclusion, meaningful limitations; peripheral sections only when they contain the only substantive core information",
            "defer": "peripheral appendix, extra ablations, trailing diagram-only material with little substantive explanation, or bibliography handled independently for paper discovery",
            "uncertain": "outline is insufficient; read rather than assume no substantive content",
        },
    },
    "references": {
        "type": "choice", "instructions": "Does the supplied page outline contain a bibliography or reference list?",
        "criteria": {"yes": "bibliographic entries or reference section", "no": "not a reference list", "uncertain": "insufficient outline"},
    },
}
_READING_POLICY = "content-v2"
_SUMMARY_POLICY = "selective-v1"


class LiteratureReviewService:
    def __init__(
        self, repository: ReviewRepository, jobs: ReviewJobs, discovery: ScholarlyDiscovery,
        documents: DocumentService, downloads: SourceDownloadService,
        workspace: WorkspaceService, tracks: TrackService,
    ) -> None:
        self.repository = repository
        self.jobs = jobs
        self.discovery = discovery
        self.documents = documents
        self.downloads = downloads
        self.workspace = workspace
        self.tracks = tracks
        self._tasks: dict[str, asyncio.Task] = {}
        self._closing = False
        self._commands: dict[str, asyncio.Lock] = {}
        self._progress_subscribers: dict[str, set[asyncio.Queue[ReviewResponse | None]]] = {}

    async def start(self) -> None:
        for record in self.repository.list():
            self._migrate_work_budget(record.id)
            record = self.repository.get(record.id)
            if (
                record.state["status"] == "blocked"
                and record.state.get("stage") == "reading"
                and not record.state.get("grouping_recovery_applied")
                and record.state.get("error") == "ValidationError: An isolated worker returned invalid evidence: "
                "Reading groups must contain every admitted page exactly once.. Resume to retry this task."
                and any(
                    job.get("status") == "invalid" and job["key"].startswith("group-pages:")
                    for job in self.repository.jobs(record.id)
                )
            ):
                self.jobs.checkpoint(
                    record.id, status="running", error=None, grouping_recovery_applied=True,
                )
                self._notice(record.id, "Automatically continuing a legacy invalid page-grouping task; prior approvals and budgets are retained.")
                record = self.repository.get(record.id)
            if record.state.get("control") == "stop_stage":
                self._stop_stage(record.id)
                continue
            if record.state["status"] == "awaiting_approval" and record.state.get("approval") in {"papers", "references", "reference_pdfs"}:
                self._continue_paper_gate(record.id)
            if self.repository.get(record.id).state["status"] == "running":
                self._schedule(record.id)

    async def recover(self) -> None:
        for record in self.repository.list():
            await self.jobs.recover(record.id)

    async def close(self) -> None:
        self._closing = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for queues in self._progress_subscribers.values():
            for queue in queues:
                queue.put_nowait(None)
        await self.discovery.close()

    async def progress_events(self, review_id: str) -> AsyncIterator[ReviewResponse]:
        queue: asyncio.Queue[ReviewResponse | None] = asyncio.Queue()
        subscribers = self._progress_subscribers.setdefault(review_id, set())
        subscribers.add(queue)
        try:
            yield self.get(review_id)
            while True:
                try:
                    review = await asyncio.wait_for(queue.get(), timeout=3)
                except TimeoutError:
                    try:
                        review = self.get(review_id)
                    except NotFoundError:
                        return
                if review is None:
                    return
                yield review
        finally:
            subscribers.discard(queue)
            if not subscribers:
                self._progress_subscribers.pop(review_id, None)

    def _publish_progress(self, review_id: str) -> None:
        queues = self._progress_subscribers.get(review_id)
        if queues:
            review = self.get(review_id)
            for queue in queues:
                queue.put_nowait(review)

    async def create(self, request: ReviewCreateRequest, *, origin_key: str | None = None) -> ReviewResponse:
        if origin_key:
            previous = next((item for item in self.repository.list() if item.state.get("origin_key") == origin_key), None)
            if previous:
                return self.get(previous.id)
        if request.decision_model is None:
            reference = self.jobs.runtime.settings.default_model_references.get("decision")
            if reference:
                request = request.model_copy(update={"decision_model": DecisionRole(model_reference=reference)})
        if request.conversation_id and not request.track_id:
            request = request.model_copy(update={
                "track_id": self.tracks.conversation_track_id(request.conversation_id),
            })
        self.jobs.runtime.resolve("chat", model_reference=ModelReference.model_validate(request.main_model.model_dump()))
        if request.decision_model:
            capability = "decision" if request.decision_model.protocol == "systemone" else "chat"
            resolved = self.jobs.runtime.resolve(
                capability, model_reference=ModelReference.model_validate(request.decision_model.model_reference.model_dump()),
            )
            if request.decision_model.protocol == "systemone" and resolved.kind != "openai_compatible":
                raise ValidationError("Native System One requires an OpenAI-compatible profile; choose structured generation for this provider.")
        if request.track_id:
            self.tracks.get(request.track_id)
        identifier = str(uuid.uuid4())
        self.repository.create(identifier, request.model_dump(mode="json"), {
            "status": "running", "stage": "scope", "approval": None,
            "scope": None, "allowance_tokens": None, "active_run_id": None,
            "stage_history": {}, "notices": [], "error": None, "output_path": None,
            "origin_key": origin_key,
        })
        self._schedule(identifier)
        return self.get(identifier)

    def get(self, review_id: str) -> ReviewResponse:
        record = self.repository.get(review_id)
        spec = ReviewCreateRequest.model_validate(record.specification)
        state = record.state
        _, total = self.repository.candidates(review_id, limit=1)
        _, selected = self.repository.candidates(review_id, limit=1, selected=True)
        screened, excluded = self.repository.screening_counts(review_id)
        used, estimated = self.jobs.usage(review_id)
        return ReviewResponse(
            id=record.id, revision=record.revision, title=spec.title, objective=spec.objective,
            main_model=spec.main_model, decision_model=spec.decision_model,
            conversation_id=spec.conversation_id, track_id=spec.track_id,
            status=state["status"], stage=state["stage"], approval=state.get("approval"),
            work_budget=state.get("work_budget"), work_used=self.jobs.work_usage(review_id),
            suggested_work_budget=state.get("suggested_work_budget"),
            scope=state.get("scope"), allowance_tokens=state.get("allowance_tokens"),
            used_tokens=used, usage_estimated=estimated,
            suggested_allowance_tokens=state.get("suggested_allowance_tokens"),
            candidate_count=total, selected_count=selected, evidence_count=len(self._evidence(review_id)),
            screened_count=screened, excluded_count=excluded,
            active_run_id=state.get("active_run_id"), stage_history=state.get("stage_history", {}),
            notices=state.get("notices", []), error=state.get("error"), output_path=state.get("output_path"),
            created_at=as_utc(record.created_at), updated_at=as_utc(record.updated_at),
        )

    def list(self, *, conversation_id: str | None = None, track_id: str | None = None) -> list[ReviewResponse]:
        return [
            self.get(record.id) for record in self.repository.list()
            if (conversation_id is None or record.specification.get("conversation_id") == conversation_id)
            and (track_id is None or record.specification.get("track_id") == track_id)
        ]

    async def clear(self, review_id: str, expected_revision: int) -> ReviewClearResponse:
        async with self._commands.setdefault(review_id, asyncio.Lock()):
            record = self.repository.get(review_id)
            if record.revision != expected_revision:
                raise ConflictError("The review changed. Reload before clearing it.")
            self.jobs.checkpoint(review_id, status="paused", control="clear", error="Cleanup in progress. Retry clear if interrupted.")
            task = self._tasks.get(review_id)
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            result = ReviewClearResponse(deleted_document_ids=[], preserved_document_ids=[], preserved_paths=[])
            with self.workspace.mutation_lock:
                jobs = self.repository.jobs(review_id)
                for job in jobs:
                    if job.get("kind") != "publication_artifact":
                        continue
                    if job.get("track_id"):
                        removable = [
                            identifier for identifier in job.get("added_paper_ids", [])
                            if not self.repository.document_used_elsewhere(review_id, identifier)
                        ]
                        if not self.tracks.remove_literature_review(job["track_id"], job["body"], remove_paper_ids=removable):
                            result.preserved_paths.append(job.get("path") or f"track:{job['track_id']}/literature")
                if record.specification.get("track_id") and record.state.get("output_path") and not any(
                    job.get("kind") == "publication_artifact" for job in jobs
                ):
                    result.preserved_paths.append(record.state["output_path"])
                for note in self.workspace.search(tags=[f"literature-review:{review_id}"], limit=100):
                    current = self.workspace.read_file(note.path)
                    receipt = next((job for job in jobs if job.get("path") == note.path), None)
                    if receipt and current.sha256 == receipt.get("sha256"):
                        self.workspace.delete_file(note.path)
                    else:
                        result.preserved_paths.append(note.path)
                candidates = self._candidates(review_id)
                candidate_documents = {item.document_id for item in candidates if item.document_id}
                candidate_documents.update(job["reference"]["document_id"] for job in jobs
                                           if job.get("kind") == "reference" and job["reference"].get("document_id"))
                linked = {paper for track in self.tracks.list() for paper in track.paper_ids}
                for document in self.documents.list_documents():
                    metadata = document.metadata_json or {}
                    owned = metadata.get("owner_review_id") == review_id
                    if not owned:
                        if document.id in candidate_documents:
                            result.preserved_document_ids.append(document.id)
                        continue
                    if (document.id in linked or metadata.get("folder_id")
                            or self.repository.document_used_elsewhere(review_id, document.id)):
                        result.preserved_document_ids.append(document.id)
                        continue
                    if self.workspace.paper_has_annotations(document.id, document.title):
                        result.preserved_document_ids.append(document.id)
                        continue
                    if any(artifact.kind.startswith("summary") for artifact in self.documents.get_document_artifacts(document.id)):
                        result.preserved_document_ids.append(document.id)
                        continue
                    self.documents.delete_document(document.id)
                    result.deleted_document_ids.append(document.id)
            for run_id in {job["run_id"] for job in jobs if job.get("run_id")}:
                try:
                    await self.jobs.runs.cancel(run_id)
                    self.jobs.runs.delete(run_id)
                except NotFoundError:
                    pass
            self.repository.delete(review_id)
            for queue in self._progress_subscribers.get(review_id, ()):
                queue.put_nowait(None)
            return result

    def candidates(
        self, review_id: str, *, limit: int = 50, offset: int = 0,
        sort: Literal["relevance", "year", "title"] = "relevance",
    ) -> CandidatePage:
        self.repository.get(review_id)
        items, total = self.repository.candidates(review_id, limit=limit, offset=offset, sort=sort, shortlist=True)
        return CandidatePage(
            items=items, total=total, next_offset=offset + len(items) if offset + len(items) < total else None,
            selected_ids=self.repository.selected_ids(review_id),
        )

    def paper(self, review_id: str, candidate_id: str) -> PaperReview:
        candidate = next((row for row in self._supplemental_candidates(review_id) if row.id == candidate_id), None)
        candidate = candidate or self.repository.candidate(review_id, candidate_id)
        version = None
        pages = []
        document = self.documents.get_document(candidate.document_id) if candidate.document_id else None
        if candidate.selected and document and document.status == "ready":
            pages, _, revision = self._paper(candidate)
            version = revision["source_version"]
        plan = self.repository.job(review_id, f"reading-plan:{candidate.id}:{version}:{_READING_POLICY}") or {}
        receipts = [
            job for job in self.repository.jobs(review_id, "reading")
            if job.get("kind") == "evidence" and job["candidate_id"] == candidate_id and job["source_version"] == version
            and job.get("reading_policy") == _READING_POLICY
        ]
        read, partial = [], []
        for page in pages:
            spans = sorted(
                (span["start"], span["end"]) for job in receipts for span in job["spans"] if span["page"] == page["page"]
            )
            end = 0
            for start, stop in spans:
                if start > end:
                    break
                end = max(end, stop)
            if spans:
                (read if end >= len(page["text"]) else partial).append(page["page"])
        return PaperReview(
            candidate_id=candidate_id, title=candidate.title, source_version=version, pages=plan.get("pages", []),
            read_pages=read, partial_pages=partial,
            evidence_count=sum(len(job["claims"]) for job in receipts),
            findings=[claim for job in receipts for claim in job["claims"]],
            partial_summaries=[{
                "sections": job["partial_summary"], "spans": job["spans"], "limitations": job["limitations"],
            } for job in receipts if job.get("partial_summary")],
            selective_summary=self._current_summary(review_id, candidate_id, version, receipts),
            operation=(self.repository.job(review_id, f"paper-operation:{candidate_id}") or {}).get("operation")
            if self.repository.get(review_id).state["status"] == "running" else None,
        )

    def _paper_operation(self, review_id: str, candidate_id: str, operation: str | None) -> None:
        key = f"paper-operation:{candidate_id}"
        self.repository.save_job(review_id, key, "progress", {"key": key, "operation": operation})
        self.jobs.checkpoint(review_id)
        self._publish_progress(review_id)

    def papers(self, review_id: str) -> list[PaperReview]:
        self.repository.get(review_id)
        return [self.paper(review_id, identifier) for identifier in [
            *self.repository.selected_ids(review_id), *(row.id for row in self._supplemental_candidates(review_id)),
        ]]

    def export(self, review_id: str) -> dict:
        return {
            "schema": "scholarweave.literature-review", "schema_version": 1,
            "review": self.get(review_id).model_dump(mode="json"),
            "candidates": [item.model_dump(mode="json") for item in self._candidates(review_id)],
            "records": self.repository.jobs(review_id),
            "references": [row.model_dump(mode="json") for row in self.references(review_id)],
        }

    @staticmethod
    def _reference_title(title: str) -> str:
        return " ".join(re.findall(r"\w+", title.casefold()))

    def references(self, review_id: str) -> list[ReviewReference]:
        state = self.repository.get(review_id).state
        originals = {row.id: row for row in self._candidates(review_id, selected=True)}
        versions = {row.id: self.documents.source_revision(row.document_id)["source_version"]
                    for row in originals.values() if row.document_id and self.documents.get_document(row.document_id)}
        entries = []
        for job in self.repository.jobs(review_id, "references"):
            if job.get("kind") != "reference_extraction" or job.get("source_version") != versions.get(job.get("candidate_id")):
                continue
            for reference in job["references"]:
                entries.append((reference, {"candidate_id": job["candidate_id"], "document_id": job["document_id"],
                                            "title": originals[job["candidate_id"]].title,
                                            "source_version": job["source_version"], "page": reference["page"],
                                            "quote": reference["quote"]}))
        groups: dict[str, dict] = {}
        title_keys: dict[str, set[str]] = {}
        # Resolve identifier-bearing entries before title-only entries. Conflicting IDs stay distinct.
        for reference, citation in sorted(entries, key=lambda entry: not bool(entry[0].get("doi") or entry[0].get("arxiv_id"))):
            doi = normalized_doi(reference.get("doi"))
            arxiv = _arxiv_key(reference.get("arxiv_id") or "")
            arxiv = re.sub(r"v\d+$", "", arxiv) if arxiv else None
            title = self._reference_title(reference["title"])
            aliases = ([f"doi:{doi}"] if doi else []) + ([f"arxiv:{arxiv}"] if arxiv else [])
            existing = [key for key, row in groups.items() if set(aliases) & set(row["aliases"])]
            if existing:
                key = existing[0]
                for other_key in existing[1:]:
                    other = groups.pop(other_key)
                    groups[key]["aliases"].extend(other["aliases"])
                    groups[key]["citations"].extend(citation for citation in other["citations"]
                                                     if citation not in groups[key]["citations"])
                    groups[key]["doi"] = groups[key]["doi"] or other["doi"]
                    groups[key]["arxiv_id"] = groups[key]["arxiv_id"] or other["arxiv_id"]
                    for known in title_keys.values():
                        if other_key in known:
                            known.discard(other_key)
                            known.add(key)
            elif aliases:
                key = aliases[0]
            else:
                known = title_keys.get(title, set())
                key = next(iter(known)) if len(known) == 1 else f"title:{title}"
            row = groups.setdefault(key, {"title": reference["title"], "authors": reference["authors"], "year": reference["year"],
                                          "doi": doi, "arxiv_id": arxiv,
                                          "aliases": [], "citations": []})
            row["aliases"] = sorted(set(row["aliases"]) | set(aliases))
            row["doi"] = row["doi"] or doi
            row["arxiv_id"] = row["arxiv_id"] or arxiv
            if citation not in row["citations"]:
                row["citations"].append(citation)
            title_keys.setdefault(title, set()).add(key)
        result = []
        for key, row in groups.items():
            identifier = digest([review_id, key])
            citing = sorted({citation["candidate_id"] for citation in row["citations"]})
            saved = self.repository.job(review_id, f"reference:{identifier}") or {}
            # Metadata/approvals do not survive a changed source/provenance set silently.
            fingerprint = digest(row["citations"])
            supplemental = saved.get("reference", {}) if saved.get("source_fingerprint") == fingerprint else {}
            result.append(ReviewReference(
                **{name: row[name] for name in ("title", "authors", "year", "doi", "arxiv_id", "citations")},
                id=identifier, citing_paper_ids=citing, citing_count=len(citing),
                selected=len(citing) > 1 and identifier in state.get("selected_reference_ids", []),
                **{name: supplemental[name] for name in ("matches", "metadata", "screening", "decision", "document_id", "error")
                   if name in supplemental},
            ))
        return sorted(result, key=lambda row: (-row.citing_count, row.title.casefold()))

    def _save_reference(self, review_id: str, row: ReviewReference) -> None:
        key = f"reference:{row.id}"
        self.repository.save_job(review_id, key, "reference_screening", {
            "key": key, "kind": "reference", "source_fingerprint": digest(row.citations),
            "reference": row.model_dump(mode="json"), "status": "completed",
        })
        self.jobs.checkpoint(review_id)
        self._publish_progress(review_id)

    async def _references(self, review_id: str) -> None:
        prepared = self.jobs.prepare(self._spec(review_id).main_model, "literature-references", ReferenceExtraction)
        for candidate in self._candidates(review_id, selected=True):
            self._paper_operation(review_id, candidate.id, "Extracting references")
            # A missing original cannot silently disappear from reference coverage.
            pages, _, revision = await asyncio.to_thread(self._paper, candidate)
            plan = self.repository.job(review_id, f"reading-plan:{candidate.id}:{revision['source_version']}:{_READING_POLICY}")
            if not plan or plan.get("status") != "completed":
                await self._reading_plan(review_id, candidates=[candidate])
                plan = self.repository.job(review_id, f"reading-plan:{candidate.id}:{revision['source_version']}:{_READING_POLICY}")
            numbers = {row["page"] for row in plan["pages"] if row.get("reference_page")}
            if not numbers:
                self._notice(review_id, f"{candidate.title}: decision model identified no reference pages; reference coverage is limited.")
            frame = {**self._frame(review_id), "title": candidate.title, **revision}
            for packet in self._source_packets([page for page in pages if page["page"] in numbers], frame, prepared):
                def validate(result: dict) -> None:
                    for row in result["references"]:
                        if not row["quote"].strip() or not any(span["page"] == row["page"] and row["quote"] in span["text"]
                                                             for span in packet["records"]):
                            raise ValidationError("A reference lacks a verbatim quote on an admitted reference page.")
                        if not self._reference_title(row["title"]) or self._reference_title(row["title"]) not in self._reference_title(row["quote"]):
                            raise ValidationError("The extracted reference title must be supported by its quoted citation.")
                        if row.get("doi"):
                            doi = normalized_doi(row["doi"])
                            if not doi or doi not in row["quote"].casefold():
                                raise ValidationError("The reference DOI must be printed in its citation quote.")
                        if row.get("arxiv_id"):
                            arxiv = _arxiv_key(row["arxiv_id"])
                            if not arxiv or arxiv not in row["quote"].casefold():
                                raise ValidationError("The arXiv identifier must be printed in its citation quote.")
                output = await self.jobs.call(review_id, "references", f"extract:{candidate.id}", packet,
                                              prepared, ReferenceExtraction, validate)
                key = f"references:{candidate.id}:{revision['source_version']}:{digest(packet)}"
                self.repository.save_job(review_id, key, "references", {
                    "key": key, "kind": "reference_extraction", "status": "completed", "candidate_id": candidate.id,
                    "document_id": candidate.document_id, **revision, "references": output["references"],
                })
                self.jobs.checkpoint(review_id)
                self._publish_progress(review_id)
            self._paper_operation(review_id, candidate.id, None)

    async def _reference_screening(self, review_id: str) -> None:
        spec = self._spec(review_id)
        role = spec.decision_model
        if role is None:
            self._notice(review_id, "No decision model configured: reference metadata is screened by the main model.")
        for row in self.references(review_id):
            if not row.selected or row.screening is not None:
                continue
            try:
                metadata = row.metadata
                if metadata is None:
                    self._notice(review_id, f"{row.title}: ambiguous or unresolved arXiv identity; skipped without guessing.")
                    if not spec.web_enabled:
                        raise ValidationError("External metadata lookup is disabled for this review.")
                    query = f'id:{row.arxiv_id}' if row.arxiv_id else f'ti:"{row.title.replace(chr(34), "")}"'
                    response = await self.discovery.query("arxiv", query, 0, limit=10)
                    matches = [normalize_candidate(review_id, raw, "arxiv", f"Reference metadata for {row.id}")
                               for raw in response["results"]]
                    confirmed = [item for item in matches if
                                 (row.arxiv_id and item.arxiv_id == row.arxiv_id) or
                                 (row.doi and item.doi == row.doi)]
                    metadata = confirmed[0] if len(confirmed) == 1 else None
                    row = row.model_copy(update={"matches": matches, "metadata": metadata})
                    if metadata is None:
                        self._save_reference(review_id, row.model_copy(update={
                            "error": "Unresolved arXiv match. Skipped without guessing publication identity.",
                        }))
                        continue
                packet = {**self._frame(review_id), "title": metadata.title, "abstract": metadata.abstract}
                self.jobs.admit_work(review_id, "screened_papers", self._work_identity(review_id, metadata))
                decision = None
                if role and role.protocol == "systemone":
                    decision = await self.jobs.native(review_id, "reference_screening", f"reference-screen:{row.id}",
                                                      role, packet, _SCREEN_QUESTIONS)
                    answers = decision["answers"]
                    screening = Screening(kind=answers["kind"]["choice"], relevance=answers["relevance"]["choice"],
                                          reason=json_text(answers), matched_questions=[])
                else:
                    prepared = self.jobs.prepare(role.model_reference if role else spec.main_model, "literature-screen", ScreeningBatch,
                                                 input_tokens=role.input_tokens if role else None)
                    output = await self.jobs.call(
                        review_id, "reference_screening", f"reference-screen:{row.id}",
                        {**self._frame(review_id), "records": [{"candidate_id": row.id, "title": metadata.title,
                                                               "abstract": metadata.abstract}]},
                        prepared, ScreeningBatch,
                        lambda result: self._validate_reference_screen(result, row.id),
                    )
                    screening = Screening.model_validate({key: value for key, value in output["candidates"][0].items()
                                                          if key != "candidate_id"})
                self._save_reference(review_id, row.model_copy(update={
                    "metadata": metadata, "screening": screening, "decision": decision, "error": None,
                }))
            except (httpx.HTTPError, OpenAIError, ProviderRuntimeError, ValueError, RuntimeError) as exc:
                self._save_reference(review_id, row.model_copy(update={"error": str(exc)}))
                self._notice(review_id, f"Reference metadata/screening failed for {row.title}: {exc}")

    @staticmethod
    def _validate_reference_screen(result: dict, identifier: str) -> None:
        if [row["candidate_id"] for row in result["candidates"]] != [identifier]:
            raise ValidationError("Screen exactly the supplied reference.")

    async def _reference_collection(self, review_id: str) -> None:
        for row in self.references(review_id):
            if not row.selected or not row.screening or row.screening.relevance not in _AUTO_RELEVANCE:
                continue
            if not row.metadata or not row.metadata.pdf_url:
                raise ValidationError("A relevant reference has no confirmed accessible PDF.")
            try:
                self.jobs.admit_work(review_id, "read_papers", self._work_identity(review_id, row.metadata))
                document = self.documents.get_document(row.document_id) if row.document_id else None
                document = document or self.downloads.find_document(row.metadata.pdf_url)
                if document is None:
                    document = await self.downloads.download_pdf(
                        row.metadata.pdf_url, title=row.metadata.title, enhance_with_llm=False, match_title=False,
                        owner_review_id=review_id,
                    )
                self._save_reference(review_id, row.model_copy(update={"document_id": document.id, "error": None}))
                if document.status != "ready":
                    await self.documents.ingest_document(document.id, enhance_with_llm=False)
            except (httpx.HTTPError, ValueError, RuntimeError) as exc:
                self._save_reference(review_id, row.model_copy(update={"error": str(exc)}))
                raise ValidationError(f"Reference PDF collection failed: {exc}. Resume retries unfinished downloads.") from exc
        supplemental = self._supplemental_candidates(review_id)
        if supplemental:
            await self._reading_plan(review_id, candidates=supplemental)
            await self._reading(review_id, candidates=supplemental)

    def _supplemental_candidates(self, review_id: str) -> list[Candidate]:
        original_documents = {row.document_id for row in self._candidates(review_id, selected=True) if row.document_id}
        return [
            row.metadata.model_copy(update={"id": row.id, "selected": True, "document_id": row.document_id,
                                            "screening": row.screening})
            for row in self.references(review_id)
            if row.selected and row.metadata and row.document_id and row.screening
            and row.screening.relevance in _AUTO_RELEVANCE
            and row.document_id not in original_documents
        ]

    def _work_identity(self, review_id: str, candidate: Candidate) -> str:
        for original in self._candidates(review_id):
            if any(getattr(original, name) and getattr(candidate, name)
                   and getattr(original, name) != getattr(candidate, name)
                   for name in ("doi", "arxiv_id", "openalex_id")):
                continue
            if original.id == candidate.id or any(
                getattr(original, name) and getattr(original, name) == getattr(candidate, name)
                for name in ("doi", "arxiv_id", "openalex_id", "url", "pdf_url", "document_id")
            ):
                return original.id
        return candidate.id

    def _continue_paper_gate(self, review_id: str) -> None:
        record = self.repository.get(review_id)
        gate = record.state.get("approval")
        if gate == "papers":
            identifiers = [row.id for row in self._candidates(review_id)
                           if row.screening and row.screening.relevance in _AUTO_RELEVANCE]
            self.repository.select_candidates(
                review_id, identifiers, revision=record.revision,
                state={**record.state, "status": "running", "approval": None, "stage": "reading_plan",
                       "approval_revision": record.revision, "error": None},
            )
        elif gate == "references":
            self.jobs.checkpoint(review_id, status="running", approval=None, stage="reference_screening",
                                 selected_reference_ids=[row.id for row in self.references(review_id) if row.citing_count > 1],
                                 error=None)
        elif gate == "reference_pdfs":
            self.jobs.checkpoint(review_id, status="running", approval=None, stage="reference_screening", error=None)

    def _migrate_work_budget(self, review_id: str) -> None:
        state = self.repository.get(review_id).state
        if state.get("work_budget") or not state.get("allowance_tokens") or state.get("approval") == "plan":
            return
        units = []
        for row in [*self._candidates(review_id), *[
            reference.metadata.model_copy(update={"selected": True, "document_id": reference.document_id,
                                                   "screening": reference.screening})
            for reference in self.references(review_id) if reference.metadata and reference.screening
        ]]:
            if row.screening:
                units.append(("screened_papers", self._work_identity(review_id, row)))
            if row.selected and row.document_id:
                units.append(("read_papers", self._work_identity(review_id, row)))
        if state.get("synthesis"):
            units.append(("comparison_passes", digest([
                self._evidence(review_id), self._frame(review_id), self._spec(review_id).main_model.model_dump(),
            ])))
        for kind, identifier in units:
            key = f"work:{kind}:{digest(identifier)}"
            self.repository.save_job(review_id, key, "work_budget", {"key": key, "work_kind": kind})
        used = self.jobs.work_usage(review_id)
        defaults = WorkBudget().model_dump()
        self.jobs.checkpoint(
            review_id, work_budget={kind: max(defaults[kind], used[kind]) for kind in defaults},
            allowance_tokens=None, suggested_allowance_tokens=None,
            **({"status": "running", "approval": None, "error": None} if state.get("approval") == "budget"
               and state["status"] == "awaiting_approval" else {}),
        )

    async def control(self, review_id: str, command: ReviewControlRequest) -> ReviewResponse:
        lock = self._commands.setdefault(review_id, asyncio.Lock())
        async with lock:
            record = self.repository.get(review_id)
            if command.expected_revision != record.revision:
                raise ConflictError("The review changed. Reload before applying this decision.")
            state = dict(record.state)
            action = command.action
            if state.get("control") == "clear":
                raise ConflictError("Review cleanup is incomplete. Retry Clear review before starting fresh.")
            if action in {"pause", "stop_stage"}:
                if state["status"] == "completed":
                    raise ConflictError("This review is already completed.")
                state["status"] = "paused"
                state["control"] = action
                self.repository.update(review_id, state, record.revision)
                task = self._tasks.get(review_id)
                if task and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if action == "stop_stage":
                    self._stop_stage(review_id)
                else:
                    self.jobs.checkpoint(review_id, active_run_id=None, control=None, error="Paused by the researcher. Completed work is retained.")
            elif action == "approve_plan":
                self._require_approval(state, "plan")
                scope = ReviewScope.model_validate(state["scope"])
                self.jobs.checkpoint(
                    review_id, status="running", approval=None, stage="discovery",
                    allowance_tokens=None, error=None, suggested_allowance_tokens=None,
                    work_budget=(command.work_budget or scope.suggested_work_budget).model_dump(),
                    suggested_work_budget=None,
                )
                self._schedule(review_id)
            elif action == "approve_papers":
                self._require_approval(state, "papers")
                self._continue_paper_gate(review_id)
                self._schedule(review_id)
            elif action in {"select_references", "approve_reference_pdfs"}:
                gate = "references" if action == "select_references" else "reference_pdfs"
                self._require_approval(state, gate)
                self._continue_paper_gate(review_id)
                self._schedule(review_id)
            elif action == "extend_budget":
                self._require_approval(state, "budget")
                budget = (command.work_budget or WorkBudget.model_validate(
                    state.get("suggested_work_budget") or state.get("work_budget") or {}
                )).model_dump()
                old = state.get("work_budget") or {}
                used = self.jobs.work_usage(review_id)
                if any(budget[kind] < max(old.get(kind, 0), used[kind]) for kind in budget):
                    raise ValidationError("Work limits cannot decrease below approved limits or work already admitted.")
                if old and not any(used[kind] >= old[kind] and budget[kind] > old[kind] for kind in budget):
                    raise ValidationError("Increase at least one exhausted work limit.")
                self.jobs.checkpoint(
                    review_id, status="running", approval=None, work_budget=budget, allowance_tokens=None,
                    suggested_allowance_tokens=None, suggested_work_budget=None, error=None,
                )
                self._schedule(review_id)
            elif action == "link_paper":
                if state["status"] == "running":
                    raise ConflictError("Pause the review before changing a paper's local source.")
                if not command.candidate_ids or len(command.candidate_ids) != 1 or not command.document_id:
                    raise ValidationError("Link exactly one candidate to an existing local paper.")
                if self.documents.get_document(command.document_id) is None:
                    raise ValidationError("The chosen local paper does not exist.")
                if STAGES.index(state["stage"]) >= STAGES.index("reading_plan"):
                    state = {
                        **state, "stage": "reading_plan", "synthesis": None, "output_path": None, "output_hash": None,
                        "stage_history": {key: value for key, value in state.get("stage_history", {}).items()
                                          if STAGES.index(key) < STAGES.index("reading_plan")},
                    }
                if state.get("approval") not in {"plan", "papers"}:
                    state.update(status="paused", approval=None)
                state["error"] = "Local source linked. Resume or approve the shortlist to prepare it."
                self.repository.link_document(
                    review_id, command.candidate_ids[0], command.document_id,
                    revision=record.revision, state=state,
                )
            elif action == "resume":
                if state["status"] == "running":
                    raise ConflictError("The review is already running.")
                self._migrate_work_budget(review_id)
                state = dict(self.repository.get(review_id).state)
                if state["status"] == "running":
                    self._schedule(review_id)
                    return self.get(review_id)
                if state.get("approval") in {"papers", "references", "reference_pdfs"} and (
                    command.stage is None or STAGES.index(command.stage) >= STAGES.index(state["stage"])
                ):
                    self._continue_paper_gate(review_id)
                    state = dict(self.repository.get(review_id).state)
                    if state["status"] == "running":
                        self._schedule(review_id)
                        return self.get(review_id)
                stage = command.stage or state["stage"]
                rewinding = STAGES.index(stage) < STAGES.index(state["stage"])
                if state.get("approval") and (not rewinding or state["approval"] == "budget"):
                    self.jobs.checkpoint(review_id, status="awaiting_approval", error=None, control=None)
                    return self.get(review_id)
                if STAGES.index(stage) > STAGES.index(state["stage"]):
                    raise ValidationError("Resume cannot skip a required stage.")
                history = dict(state.get("stage_history", {}))
                for later in STAGES[STAGES.index(stage):]:
                    history.pop(later, None)
                for job in self.repository.jobs(review_id):
                    if job.get("status") == "invalid":
                        self.repository.save_job(review_id, job["key"], job["stage"], {
                            **job, "status": "retry_requested",
                        })
                self.jobs.checkpoint(
                    review_id, status="running", stage=stage, control=None, error=None,
                    stage_history=history, output_path=None, output_hash=None,
                    approval=None,
                    **({"approval_revision": None} if STAGES.index(stage) < STAGES.index("reading_plan") else {}),
                    **({"synthesis": None} if stage != "publication" else {}),
                )
                self._schedule(review_id)
            return self.get(review_id)

    @staticmethod
    def _require_approval(state: dict, expected: str) -> None:
        if state["status"] != "awaiting_approval" or state.get("approval") != expected:
            raise ConflictError("That approval is no longer pending.")

    def _schedule(self, review_id: str) -> None:
        current = self._tasks.get(review_id)
        if current and not current.done():
            return
        task = asyncio.create_task(self._execute(review_id))
        self._tasks[review_id] = task
        task.add_done_callback(lambda completed: self._tasks.pop(review_id, None) if self._tasks.get(review_id) is completed else None)

    async def _execute(self, review_id: str) -> None:
        try:
            for job in self.repository.jobs(review_id, "progress"):
                if job.get("operation"):
                    self.repository.save_job(review_id, job["key"], "progress", {**job, "operation": None})
            while self.repository.get(review_id).state["status"] == "running":
                stage = self.repository.get(review_id).state["stage"]
                await getattr(self, f"_{stage}")(review_id)
                current = self.repository.get(review_id).state
                if current["status"] != "running":
                    break
                self._advance(review_id)
        except ReviewPaused:
            pass
        except asyncio.CancelledError:
            self.jobs.checkpoint(review_id, active_run_id=None)
            if self._closing:
                self._notice(review_id, "Execution was interrupted by shutdown; completed checkpoints are retained.")
            raise
        except Exception as exc:
            _logger.exception("Literature review %s stopped at a durable checkpoint", review_id)
            self.jobs.checkpoint(
                review_id, status="blocked", active_run_id=None,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _advance(self, review_id: str, *, stopped: bool = False) -> None:
        record = self.repository.get(review_id)
        state = record.state
        stage = state["stage"]
        history = {**state.get("stage_history", {}), stage: "stopped" if stopped else "completed"}
        if stage == "publication":
            self.jobs.checkpoint(review_id, status="completed", stage_history=history, active_run_id=None, control=None)
        else:
            next_stage = STAGES[STAGES.index(stage) + 1]
            approval = {"scope": "plan", "screening": "papers", "references": "references"}.get(stage)
            if stage == "reference_screening":
                rows = [row for row in self.references(review_id) if row.selected]
                if any(row.error and not row.error.startswith("Unresolved arXiv match.") for row in rows):
                    raise ValidationError("Reference lookup or screening failed. Resume reference screening to retry.")
            if stage == "references" and not any(row.citing_count > 1 for row in self.references(review_id)):
                self._notice(review_id, "No references cited by multiple original papers were captured; supplemental collection is skipped.")
            self.jobs.checkpoint(
                review_id, stage=next_stage, status="awaiting_approval" if approval else "running",
                approval=approval, stage_history=history, active_run_id=None, control=None, error=None,
            )
            if approval in {"papers", "references"}:
                self._continue_paper_gate(review_id)

    def _stop_stage(self, review_id: str) -> None:
        state = self.repository.get(review_id).state
        stage = state["stage"]
        can_advance = {
            "scope": lambda: bool(state.get("scope")),
            "discovery": lambda: bool(self.repository.candidates(review_id, limit=1)[1]),
            "screening": lambda: bool(self.repository.candidates(review_id, limit=1)[1]),
            "reading_plan": lambda: any(
                any(page.action == "read" for page in self.paper(review_id, candidate.id).pages)
                for candidate in self._candidates(review_id, selected=True)
            ),
            "reading": lambda: bool(self._evidence(review_id)),
            "references": lambda: False,
            "reference_screening": lambda: False,
            "reference_collection": lambda: False,
            "synthesis": lambda: bool(state.get("synthesis")),
            "publication": lambda: bool(state.get("output_path")),
        }[stage]()
        if state.get("approval") in {"plan", "budget"}:
            self.jobs.checkpoint(
                review_id, status="awaiting_approval", control=None,
                error="Stopped by the researcher. Scope and budget approval cannot be skipped.",
            )
            return
        if can_advance:
            self._notice(review_id, f"The researcher stopped {stage}; downstream work uses explicitly partial results.")
            self._advance(review_id, stopped=True)
            if self.repository.get(review_id).state["status"] == "running":
                self._schedule(review_id)
        else:
            self.jobs.checkpoint(
                review_id, status="paused", approval=None, active_run_id=None, control=None,
                error=f"Stopped by the researcher during {stage}. There is not enough valid output to advance; resume this stage to continue.",
            )

    def _notice(self, review_id: str, message: str) -> None:
        state = self.repository.get(review_id).state
        notices = state.get("notices", [])
        if message not in notices:
            self.jobs.checkpoint(review_id, notices=[*notices, message])

    def _spec(self, review_id: str) -> ReviewCreateRequest:
        return ReviewCreateRequest.model_validate(self.repository.get(review_id).specification)

    def _frame(self, review_id: str) -> dict:
        record = self.repository.get(review_id)
        scope = record.state.get("scope") or {}
        return {
            "objective": record.specification["objective"],
            "questions": scope.get("questions", []),
            "inclusion_criteria": scope.get("inclusion_criteria", []),
            "exclusion_criteria": scope.get("exclusion_criteria", []),
        }

    def _candidates(self, review_id: str, *, selected: bool = False) -> list[Candidate]:
        output: list[Candidate] = []
        offset = 0
        while True:
            batch, total = self.repository.candidates(review_id, limit=100, offset=offset, selected=selected)
            output.extend(batch)
            offset += len(batch)
            if offset >= total or not batch:
                return output

    def _track_context(self, review_id: str, prepared: tuple) -> dict | None:
        saved = self.repository.job(review_id, "track-context")
        if saved is not None:
            return saved["context"]
        spec = self._spec(review_id)
        if not spec.track_id:
            return None
        track = self.tracks.get(spec.track_id)
        paths = [item.path for item in [*track.sections, *track.exploration]]
        for document_id in track.paper_ids:
            if self.documents.get_document(document_id) is not None:
                folder = self.workspace.paper_folder(document_id)
                paths.extend([f"{folder}/summary.md", f"{folder}/notes.md"])
        hits = self.workspace.search(
            query=spec.objective[:2000], kinds=["track_section", "paper_summary", "paper_notes"], limit=100,
        )
        matches = {item.path: item.excerpt for item in hits if item.path in paths}
        priority = {item.path: index for index, item in enumerate(hits)}
        paths.sort(key=lambda path: (
            0 if path.endswith("/problem.md") else 1 if path.endswith("/literature.md") else 2,
            priority.get(path, len(priority)),
        ))
        compiled, budget, _ = prepared
        context = {
            "track_id": track.id, "title": track.title,
            "evidence_status": "Unverified prior work; verify claims against current paper sources.",
            "sources": [], "omitted_source_count": 0,
        }
        # Leave most of each worker's request for its objective, schema and source evidence.
        limit = max(0, (budget.limit - budget.count(compiled.entry_agent.instructions, spec.objective)) // 4)
        per_source = max(256, limit // max(1, len(paths)))
        for path in paths:
            try:
                document = self.workspace.read_file(path)
            except (FileNotFoundError, StorageError, UnicodeError) as exc:
                self._notice(review_id, f"Prior work could not be read at {path}: {exc}")
                context["omitted_source_count"] += 1
                continue
            if not isinstance(document.content, str):
                raise ValidationError(f"Prior research at {path} must be Markdown text.")
            source = {
                "path": path, "sha256": document.sha256, "kind": document.kind,
                "excerpt": document.content, "truncated": False,
            }
            if budget.count("", json_text(source)) > per_source and matches.get(path):
                source.update(excerpt=matches[path], truncated=True)
            text = source["excerpt"]
            low, high, admitted = 0, len(text), None
            while low <= high:
                size = (low + high) // 2
                candidate = {**source, "excerpt": text[:size], "truncated": source["truncated"] or size < len(text)}
                packet = {**context, "sources": [*context["sources"], candidate]}
                if budget.count("", json_text(candidate)) <= per_source and budget.count("", json_text(packet)) <= limit:
                    admitted = candidate
                    low = size + 1
                else:
                    high = size - 1
            if admitted is None or (text and not admitted["excerpt"]):
                context["omitted_source_count"] += 1
            else:
                context["sources"].append(admitted)
        if context["omitted_source_count"] or any(source["truncated"] for source in context["sources"]):
            self._notice(
                review_id, "Notebook context was limited to fit the model. Excerpts are prior-work leads, not complete coverage; full files remain in the track.",
            )
        for warning in track.warnings:
            self._notice(review_id, warning)
        self.repository.save_job(review_id, "track-context", "scope", {
            "key": "track-context", "kind": "track_context", "status": "completed",
            "paper_ids": track.paper_ids, "context": context,
        })
        return context

    async def _scope(self, review_id: str) -> None:
        spec = self._spec(review_id)
        providers = ["library", *(["arxiv", "web"] if spec.web_enabled else [])]
        if self._openalex_enabled(spec):
            providers.append("openalex")
        prepared = self.jobs.prepare(spec.main_model, "literature-scope", ReviewScope)
        track_context = self._track_context(review_id, prepared)

        def validate(result: dict) -> None:
            if any(query["provider"] not in providers for query in result["queries"]):
                raise ValidationError("The proposed search plan used a disabled source.")

        scope = await self.jobs.call(
            review_id, "scope", "scope", {
                "objective": spec.objective, "providers": providers,
                "max_discovered_papers": _MAX_DISCOVERED_PAPERS,
                **({"track_context": track_context} if track_context is not None else {}),
            }, prepared, ReviewScope, validate,
        )
        self.jobs.checkpoint(
            review_id, scope=scope, suggested_work_budget=scope["suggested_work_budget"],
        )

    def _openalex_enabled(self, spec: ReviewCreateRequest) -> bool:
        return bool(spec.web_enabled and spec.openalex_enabled and self.discovery.settings.openalex_api_key)

    def _discovery_remaining(self, review_id: str) -> int:
        _, count = self.repository.candidates(review_id, limit=1)
        remaining = max(0, _MAX_DISCOVERED_PAPERS - count)
        if not remaining:
            self._notice(review_id, "Discovery stopped at the 500-paper cap. Screen and consume the retrieved papers before starting another review; no further sources were requested.")
        return remaining

    async def _discovery(self, review_id: str) -> None:
        spec = self._spec(review_id)
        scope = ReviewScope.model_validate(self.repository.get(review_id).state["scope"])
        if not self._discovery_remaining(review_id):
            return
        context = self.repository.job(review_id, "track-context")
        if context is not None:
            for document_id in context["paper_ids"]:
                if not self._discovery_remaining(review_id):
                    return
                document = self.documents.get_document(document_id)
                if document is None:
                    self._notice(review_id, f"Previously linked paper {document_id} no longer exists.")
                    continue
                candidate = normalize_candidate(review_id, {
                    **(document.metadata_json or {}), "document_id": document.id,
                    "title": document.title, "url": (document.metadata_json or {}).get("source_url"),
                }, "library", f"Linked to research track {spec.track_id} when this review was scoped.")
                self.repository.save_candidate(review_id, candidate_key(candidate), candidate)
        failed_sources: set[str] = set()
        for query in scope.queries:
            if query.provider == "openalex" and not self._openalex_enabled(spec):
                self._notice(review_id, "OpenAlex discovery was skipped: it is disabled or no API key is configured.")
                continue
            cursor = None
            for page in range(query.pages):
                remaining = self._discovery_remaining(review_id)
                if not remaining:
                    return
                key = f"search:{digest([query.provider, query.query, page])}"
                saved = self.repository.job(review_id, key)
                if saved and saved.get("status") == "completed":
                    cursor = saved.get("next_cursor")
                    if not saved.get("has_more"):
                        break
                    continue
                if query.provider in failed_sources:
                    break
                try:
                    if query.provider == "library":
                        hits = self.documents.search_documents(query.query, limit=min(100, remaining), offset=page * 100)
                        rows = [{
                            **(document.metadata_json or {}), "document_id": document.id,
                            "title": document.title, "url": (document.metadata_json or {}).get("source_url"),
                        } for document, *_ in hits]
                    else:
                        if not spec.web_enabled or (query.provider == "openalex" and not spec.openalex_enabled):
                            raise ValidationError("This review does not allow that external source.")
                        if query.provider == "web":
                            state = self.repository.get(review_id).state
                            used = state.get("web_search_requests_used", 0)
                            if used >= self.discovery.settings.web_search_max_requests_per_session:
                                self._notice(review_id, "DuckDuckGo review request limit reached. Discovery will use the papers already gathered.")
                                failed_sources.add("web")
                                break
                            self.jobs.checkpoint(review_id, web_search_requests_used=used + 1)
                        result = await self.discovery.query(query.provider, query.query, page, cursor=cursor, limit=min(100, remaining))
                        rows = result["results"]
                        cursor = (result.get("meta") or {}).get("next_cursor")
                        if result.get("warning"):
                            self._notice(review_id, str(result["warning"]))
                    for raw in rows:
                        if not self._discovery_remaining(review_id):
                            break
                        candidate = normalize_candidate(review_id, raw, query.provider, f"{query.provider}: {query.query}; page {page + 1}")
                        self.repository.save_candidate(review_id, candidate_key(candidate), candidate)
                    has_more = bool(cursor) if query.provider == "openalex" else query.provider != "web" and len(rows) == 100
                    self.repository.save_job(review_id, key, "discovery", {
                        "key": key, "status": "completed", "provider": query.provider,
                        "query": query.query, "page": page, "result_count": len(rows),
                        "has_more": has_more, "next_cursor": cursor,
                    })
                    if not has_more:
                        break
                except (httpx.HTTPError, ValueError, RuntimeError) as exc:
                    failed_sources.add(query.provider)
                    self._notice(review_id, f"{query.provider} discovery stopped: {exc}. Available candidates are retained.")
                    break
                await asyncio.sleep(0)
        if not self.repository.candidates(review_id, limit=1)[1]:
            raise ValidationError("No candidates are available. Resume discovery after fixing source access or use local papers.")
        # Bibliographic expansion is exclusively from the explicitly selected original PDFs.

    async def _screening(self, review_id: str) -> None:
        spec = self._spec(review_id)
        pending = [item for item in self._candidates(review_id) if item.screening is None]
        budget = self.repository.get(review_id).state.get("work_budget")
        deferred = []
        if budget:
            remaining = budget["screened_papers"] - self.jobs.work_usage(review_id)["screened_papers"]
            admitted, fresh = [], []
            for item in pending:
                key = f"work:screened_papers:{digest(self._work_identity(review_id, item))}"
                (admitted if self.repository.job(review_id, key) else fresh).append(item)
            pending = [*admitted, *fresh[:max(0, remaining)]]
            deferred = fresh[max(0, remaining):]
        for item in pending:
            self.jobs.admit_work(review_id, "screened_papers", self._work_identity(review_id, item))
        fallback: list[Candidate] = []
        role = spec.decision_model
        if role and role.protocol == "systemone":
            available = True

            async def screen(candidate: Candidate) -> None:
                nonlocal available
                if not available:
                    fallback.append(candidate)
                    return
                try:
                    result = await self.jobs.native(
                        review_id, "screening", f"screen:{candidate.id}", role,
                        {**self._frame(review_id), "title": candidate.title, "abstract": candidate.abstract},
                        _SCREEN_QUESTIONS,
                    )
                    answers = result["answers"]
                    screening = Screening(
                        kind=answers["kind"]["choice"], relevance=answers["relevance"]["choice"],
                        reason="Native metadata-only decision; scores are not calibrated evidence of relevance or quality.",
                        matched_questions=[],
                    )
                    uncertain = screening.kind == "unknown"
                    self.repository.update_candidate(review_id, candidate.model_copy(update={
                        "screening": None if uncertain else screening, "decision": result,
                    }))
                    self.jobs.checkpoint(review_id)
                    self._publish_progress(review_id)
                    if uncertain:
                        fallback.append(candidate)
                except (OpenAIError, ProviderRuntimeError, ValidationError, ValueError) as exc:
                    self._notice(review_id, f"Decision screening uses the main model for records it cannot classify: {exc}")
                    fallback.append(candidate)
                    if isinstance(exc, (OpenAIError, ProviderRuntimeError)):
                        available = False

            offset = 0
            while offset < len(pending):
                parallelism = self.jobs.runtime.settings.decision_parallel_requests
                tasks = [asyncio.create_task(screen(candidate)) for candidate in pending[offset:offset + parallelism]]
                offset += parallelism
                try:
                    await asyncio.gather(*tasks)
                except BaseException:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise
        else:
            fallback = pending
        if fallback and role and role.protocol == "generation":
            failed = await self._screen_batches(review_id, fallback, role.model_reference, optional=True)
            fallback = failed
        if fallback:
            await self._screen_batches(review_id, fallback, spec.main_model, optional=False)
        if deferred:
            self.jobs.admit_work(review_id, "screened_papers", self._work_identity(review_id, deferred[0]))

    async def _screen_batches(self, review_id: str, candidates: list[Candidate], reference, *, optional: bool) -> list[Candidate]:
        try:
            role = self._spec(review_id).decision_model
            prepared = self.jobs.prepare(reference, "literature-screen", ScreeningBatch,
                                         input_tokens=role.input_tokens if optional and role else None)
            packets = self._output_packets([{
                "candidate_id": item.id, "title": item.title, "abstract": item.abstract,
                "source_kind": item.source_kind, "year": item.year,
            } for item in candidates], self._frame(review_id), prepared, 400)
        except (ProviderRuntimeError, ValidationError) as exc:
            if not optional:
                raise
            self._notice(review_id, f"Decision screening is unavailable; the main model handles these candidates: {exc}")
            return candidates
        failed: set[str] = set()
        for packet in packets:
            try:
                offered = {row["candidate_id"] for row in packet["records"]}

                def validate(result: dict) -> None:
                    returned = [row["candidate_id"] for row in result["candidates"]]
                    if set(returned) != offered or len(returned) != len(offered):
                        raise ValidationError("Screening did not account for each candidate exactly once.")

                result = await self.jobs.call(review_id, "screening", "screen-batch", packet, prepared, ScreeningBatch, validate)
                for row in result["candidates"]:
                    candidate = self.repository.candidate(review_id, row["candidate_id"])
                    screening = Screening.model_validate({key: value for key, value in row.items() if key != "candidate_id"})
                    self.repository.update_candidate(review_id, candidate.model_copy(update={"screening": screening}))
                    self.jobs.checkpoint(review_id)
                    self._publish_progress(review_id)
            except (ProviderRuntimeError, ValidationError) as exc:
                if not optional:
                    raise
                self._notice(review_id, f"Decision screening failed; queued for the main model: {exc}")
                failed.update(row["candidate_id"] for row in packet["records"])
        return [item for item in candidates if item.id in failed]

    def _output_packets(self, records: list[dict], frame: dict, prepared: tuple, tokens_per_record: int) -> list[dict]:
        result = []
        width = max(1, prepared[2] // tokens_per_record)
        for offset in range(0, len(records), width):
            result.extend(self.jobs.packets(records[offset:offset + width], frame, prepared))
        return result

    async def _prepare_paper(self, review_id: str, candidate: Candidate) -> Candidate:
        if not candidate.selected:
            raise ConflictError("Unapproved papers cannot be acquired or read.")
        self.jobs.admit_work(review_id, "read_papers", self._work_identity(review_id, candidate))
        document = self.documents.get_document(candidate.document_id) if candidate.document_id else None
        if document is None:
            for url in (candidate.url, candidate.pdf_url):
                if url:
                    document = self.downloads.find_document(url)
                    if document:
                        break
        if document is None:
            if not candidate.pdf_url:
                raise ValidationError("No accessible PDF is known. Upload it in the paper library, pause, link the local paper from this shortlist, then resume.")
            document = await self.downloads.download_pdf(
                candidate.pdf_url, title=candidate.title, enhance_with_llm=False, match_title=False,
                owner_review_id=review_id,
            )
        if candidate.document_id != document.id:
            candidate = candidate.model_copy(update={"document_id": document.id, "error": None})
            self.repository.update_candidate(review_id, candidate)
        if document.status != "ready":
            await self.documents.ingest_document(document.id, enhance_with_llm=False)
        if candidate.error:
            candidate = candidate.model_copy(update={"error": None})
            self.repository.update_candidate(review_id, candidate)
        return candidate

    def _paper(self, candidate: Candidate) -> tuple[list[dict], dict, dict]:
        if not candidate.selected or not candidate.document_id:
            raise ConflictError("Only approved, prepared papers can be read.")
        artifacts = self.documents.get_document_artifacts(candidate.document_id)
        manifest = next((item for item in artifacts if item.kind == "extracted_manifest"), None)
        if manifest is None:
            raise ValidationError("The approved paper has no extracted page manifest.")
        content = self.documents.artifact_content(manifest)
        pages = manifest_pages(content)
        return pages, content, self.documents.source_revision(candidate.document_id)

    async def _reading_plan(self, review_id: str, *, candidates: list[Candidate] | None = None) -> None:
        spec = self._spec(review_id)
        prepared_papers: list[tuple[Candidate, list[dict], dict]] = []
        for candidate in candidates if candidates is not None else self._candidates(review_id, selected=True):
            try:
                self._paper_operation(review_id, candidate.id, "Preparing source and deciding pages")
                if candidates is None:
                    candidate = await self._prepare_paper(review_id, candidate)
                pages, manifest, revision = await asyncio.to_thread(self._paper, candidate)
                outlines = [{
                    "page": page["page"],
                    "headings": list(dict.fromkeys(
                        section["title"] for section in manifest.get("sections", [])
                        if section.get("page_start", 0) <= page["page"] <= section.get("page_end", 0)
                    )),
                    "opening_excerpt": page["text"][:600], "closing_excerpt": page["text"][-600:], "excerpt_only": True,
                } for page in pages]
                prepared_papers.append((candidate, outlines, revision))
                self._paper_operation(review_id, candidate.id, None)
            except (ValueError, RuntimeError, httpx.HTTPError) as exc:
                if candidates is not None:
                    raise
                self.repository.update_candidate(review_id, candidate.model_copy(update={"error": str(exc)}))
                self._notice(review_id, f"{candidate.title}: {exc}")
        if not prepared_papers:
            raise ValidationError("None of the approved papers is readable. Available metadata is not full-paper evidence.")
        role = spec.decision_model
        if role is None:
            self._notice(review_id, "No decision model configured: the main model owns page decisions and reference screening.")
        for candidate, outlines, revision in prepared_papers:
            self._paper_operation(review_id, candidate.id, "Deciding pages")
            key = f"reading-plan:{candidate.id}:{revision['source_version']}:{_READING_POLICY}"
            if (self.repository.job(review_id, key) or {}).get("status") == "completed":
                self._paper_operation(review_id, candidate.id, None)
                continue
            choices = []
            def capture_choices() -> None:
                self.repository.save_job(review_id, key, "reading_plan", {
                    "key": key, "kind": "reading_plan", "status": "running", "candidate_id": candidate.id,
                    "document_id": candidate.document_id, **revision, "pages": list(choices),
                })
                self.jobs.checkpoint(review_id)
                self._publish_progress(review_id)
            if role and role.protocol == "systemone":
                async def decide(outline: dict) -> dict:
                    result = await self.jobs.native(
                        review_id, "reading_plan", f"page:{candidate.id}:{revision['source_version']}:{_READING_POLICY}:{outline['page']}",
                        role, {"title": candidate.title, "outline": outline, "reading_policy": _READING_POLICY}, _PAGE_QUESTIONS,
                    )
                    answers = result["answers"]
                    priority = answers["priority"]["choice"]
                    reference_page = answers["references"]["choice"] != "no"
                    return {"page": outline["page"], "action": "read" if priority != "defer" else "defer",
                            "reference_page": reference_page,
                            "reason": json_text(answers)}
                width = self.jobs.runtime.settings.decision_parallel_requests
                for offset in range(0, len(outlines), width):
                    tasks = [asyncio.create_task(decide(row)) for row in outlines[offset:offset + width]]
                    try:
                        choices.extend(await asyncio.gather(*tasks))
                        capture_choices()
                    except BaseException:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        raise
            else:
                prepared = self.jobs.prepare(role.model_reference if role else spec.main_model, "literature-pages", ReadingPlan,
                                             input_tokens=role.input_tokens if role else None)
                for packet in self._output_packets(
                    outlines, {"title": candidate.title, "reading_policy": _READING_POLICY}, prepared, 160,
                ):
                    offered = {page["page"] for page in packet["records"]}
                    result = await self.jobs.call(
                        review_id, "reading_plan", "page-plan", packet, prepared, ReadingPlan,
                        lambda output: self._validate_pages(output, offered),
                    )
                    choices.extend(result["pages"])
                    capture_choices()
            by_page = {row["page"]: row for row in choices}
            # Ask for a contextual decision on isolated holes only, never fill whole gaps.
            for number in self._isolated_gaps(choices):
                context = [by_page[number - 1], by_page[number], by_page[number + 1]]
                outline = next(row for row in outlines if row["page"] == number)
                frame = {"title": candidate.title, "reading_policy": _READING_POLICY, "neighbor_decisions": context,
                         "instruction": "Reconsider this isolated skipped page between two read pages. Keep defer only if justified; do not fill separated blocks."}
                if role and role.protocol == "systemone":
                    result = await self.jobs.native(review_id, "reading_plan", f"gap:{candidate.id}:{revision['source_version']}:{_READING_POLICY}:{number}",
                                                    role, {**frame, "outline": outline}, _PAGE_QUESTIONS)
                    answers = result["answers"]
                    by_page[number] = {"page": number, "action": "defer" if answers["priority"]["choice"] == "defer" else "read",
                                       "reference_page": answers["references"]["choice"] != "no", "reason": json_text(answers)}
                else:
                    result = await self.jobs.call(review_id, "reading_plan", "page-gap", {**frame, "records": [outline]},
                                                  prepared, ReadingPlan, lambda output: self._validate_pages(output, {number}))
                    by_page[number] = result["pages"][0]
            choices = [by_page[number] for number in sorted(by_page)]
            self.repository.save_job(review_id, key, "reading_plan", {
                "key": key, "kind": "reading_plan", "status": "completed",
                "candidate_id": candidate.id, "document_id": candidate.document_id,
                **revision, "pages": choices,
            })
            self.jobs.checkpoint(review_id)
            self._publish_progress(review_id)
            self._paper_operation(review_id, candidate.id, None)

    @staticmethod
    def _isolated_gaps(choices: list[dict]) -> list[int]:
        actions = {row["page"]: row["action"] for row in choices}
        return [page for page, action in actions.items() if action == "defer"
                and actions.get(page - 1) == "read" and actions.get(page + 1) == "read"]

    @staticmethod
    def _validate_pages(result: dict, offered: set[int]) -> None:
        returned = [page["page"] for page in result["pages"]]
        if set(returned) != offered or len(returned) != len(offered):
            raise ValidationError("A reading plan must account for every supplied page exactly once.")
        if any(not page["reason"].strip() for page in result["pages"]):
            raise ValidationError("Every read/defer decision needs a reason.")

    def _source_packets(self, pages: list[dict], frame: dict, prepared: tuple) -> list[dict]:
        compiled, budget, _ = prepared
        spans = []
        for page in pages:
            text = page["text"]
            offset = 0
            while offset < len(text):
                low, high = 1, len(text) - offset
                fits = 0
                while low <= high:
                    size = (low + high) // 2
                    span = {"page": page["page"], "start": offset, "end": offset + size, "text": text[offset:offset + size]}
                    packet = {**frame, "records": [span]}
                    if budget.count(compiled.entry_agent.instructions, json_text(packet)) <= budget.limit:
                        fits = size
                        low = size + 1
                    else:
                        high = size - 1
                if not fits:
                    raise ValidationError("The reader framing and schema leave no context for source evidence.")
                spans.append({"page": page["page"], "start": offset, "end": offset + fits, "text": text[offset:offset + fits]})
                offset += fits
        return self.jobs.packets(spans, frame, prepared)

    async def _reading(self, review_id: str, *, candidates: list[Candidate] | None = None) -> None:
        spec = self._spec(review_id)
        prepared = self.jobs.prepare(spec.main_model, "literature-evidence", EvidencePacket)
        for candidate in candidates if candidates is not None else self._candidates(review_id, selected=True):
            self._paper_operation(review_id, candidate.id, "Reading source packets and writing partial summaries")
            try:
                pages, _, revision = await asyncio.to_thread(self._paper, candidate)
            except (ValueError, RuntimeError) as exc:
                if candidates is not None:
                    raise
                self._notice(review_id, f"{candidate.title} remains unread: {exc}")
                continue
            plan = self.repository.job(review_id, f"reading-plan:{candidate.id}:{revision['source_version']}:{_READING_POLICY}")
            if not plan or plan.get("status") != "completed":
                await self._reading_plan(review_id, candidates=[candidate])
                plan = self.repository.job(review_id, f"reading-plan:{candidate.id}:{revision['source_version']}:{_READING_POLICY}")
            page_map = {page["page"]: page for page in pages}
            queue = [row["page"] for row in plan["pages"] if row["action"] == "read"]
            seen: set[int] = set()
            while queue:
                numbers = list(dict.fromkeys(number for number in queue if number not in seen))
                queue = []
                if not numbers:
                    break
                selected_pages = [page_map[number] for number in numbers]
                frame = {
                    "title": candidate.title, "page_count": len(pages), "reading_policy": _READING_POLICY,
                    "source_version": revision["source_version"],
                }
                grouping = self.jobs.prepare(spec.main_model, "literature-page-groups", ReadingGroups)
                packets = []
                self._paper_operation(review_id, candidate.id, "Grouping admitted pages into bounded source packets")
                outlines = [{"page": page["page"], "opening_excerpt": page["text"][:600]} for page in selected_pages]
                for group_packet in self.jobs.packets(outlines, frame, grouping):
                    offered = {row["page"] for row in group_packet["records"]}
                    def validate_groups(result: dict) -> None:
                        grouped = [number for group in result["groups"] for number in group]
                        if set(grouped) != offered or len(grouped) != len(offered) or any(not group for group in result["groups"]):
                            raise ValidationError(
                                "Reading groups must contain every admitted page exactly once, with no empty groups. "
                                f"Missing pages: {sorted(offered - set(grouped))}; "
                                f"unexpected pages: {sorted(set(grouped) - offered)}; "
                                f"duplicated pages: {sorted(number for number in set(grouped) if grouped.count(number) > 1)}; "
                                f"empty groups: {sum(not group for group in result['groups'])}."
                            )
                    try:
                        groups = await self.jobs.call(review_id, "reading", "group-pages", group_packet, grouping,
                                                      ReadingGroups, validate_groups)
                    except InvalidReviewOutput:
                        fallback_pages = [page for page in pages if page["page"] in offered]
                        fallback = {
                            "status": "completed", "kind": "FALLBACK", "pages": [page["page"] for page in fallback_pages],
                            "input_hash": digest(group_packet), "reason": "Invalid page grouping exhausted automatic repairs.",
                        }
                        self.repository.save_job(
                            review_id, f"grouping-fallback:{digest(group_packet)}", "reading", fallback,
                        )
                        self._notice(review_id, f"{candidate.title}: FALLBACK to bounded source-order packets after invalid page grouping; every admitted page is retained.")
                        packets.extend(self._source_packets(fallback_pages, frame, prepared))
                        continue
                    for group in groups["groups"]:
                        packets.extend(self._source_packets([page_map[number] for number in group], frame, prepared))
                for packet in packets:
                    source_pages = ", ".join(str(number) for number in dict.fromkeys(span["page"] for span in packet["records"]))
                    self._paper_operation(review_id, candidate.id, f"Reading pages {source_pages} and writing a partial summary")
                    def validate(result: dict) -> None:
                        for claim in result["claims"]:
                            if not claim["quote"].strip() or not any(
                                span["page"] == claim["page"] and claim["quote"] in span["text"] for span in packet["records"]
                            ):
                                raise ValidationError("An evidence claim did not quote the admitted source page verbatim.")
                        if any(number not in page_map for number in result["follow_up_pages"]):
                            raise ValidationError("The evidence worker requested a nonexistent page.")
                        for section in result["partial_summary"]:
                            if not section["claim_indexes"] or any(
                                index < 0 or index >= len(result["claims"]) for index in section["claim_indexes"]
                            ):
                                raise ValidationError("A partial summary must cite claims from its own source packet.")
                        if result["claims"] and not result["partial_summary"]:
                            raise ValidationError("Substantive evidence requires a genuine partial summary.")

                    result = await self.jobs.call(
                        review_id, "reading", f"read:{candidate.document_id}", packet, prepared, EvidencePacket, validate,
                    )
                    queue.extend(result["follow_up_pages"])
                    key = f"evidence-receipt:{candidate.id}:{revision['source_version']}:{_SUMMARY_POLICY}:{digest(packet)}"
                    claims = [{
                        **claim, "id": digest([key, index]), "candidate_id": candidate.id,
                        "document_id": candidate.document_id, "source_version": revision["source_version"],
                        "title": candidate.title, "citation": f"p.{claim['page']}",
                    } for index, claim in enumerate(result["claims"])]
                    self.repository.save_job(review_id, key, "reading", {
                        "key": key, "kind": "evidence", "status": "completed",
                        "candidate_id": candidate.id, "document_id": candidate.document_id, **revision,
                        "spans": [{name: value for name, value in span.items() if name != "text"} for span in packet["records"]],
                        "claims": claims, "limitations": result["limitations"], "follow_up_pages": result["follow_up_pages"],
                        "reading_policy": _READING_POLICY, "summary_policy": _SUMMARY_POLICY,
                        "partial_summary": [{
                            "title": section["title"], "content": section["content"],
                            "evidence_ids": [claims[index]["id"] for index in section["claim_indexes"]],
                        } for section in result["partial_summary"]],
                    })
                    self.jobs.checkpoint(review_id)
                    self._publish_progress(review_id)
                seen.update(numbers)
            await self._summarize_paper(review_id, candidate, revision)
            self._paper_operation(review_id, candidate.id, None)
        if not self._evidence(review_id):
            raise ValidationError("No source-backed findings are available. A gap analysis would be unsupported; resume reading or adjust the approved set.")

    def _current_summary(self, review_id: str, candidate_id: str, version: str | None, receipts: list[dict]) -> dict | None:
        key = f"paper-summary:{candidate_id}:{version}:{_SUMMARY_POLICY}:{digest(receipts)}"
        return (self.repository.job(review_id, key) or {}).get("summary")

    @staticmethod
    def _validate_selective_summary(result: dict, ids: set[str], title: str | None = None) -> None:
        outline = {"Motivation, contribution, and novelty", "Method", "Results", "Open questions"}
        for section in result["sections"]:
            if section["title"] not in outline or (title is not None and section["title"] != title):
                raise ValidationError("Selective summaries must preserve the supplied shared outline.")
            if not section["content"].strip() or not section["evidence_ids"] or not set(section["evidence_ids"]) <= ids:
                raise ValidationError("Selective summary sections must cite their supplied source evidence.")
        if not result["sections"] and title is None:
            raise ValidationError("Selective summary has no source-supported content.")

    async def _summarize_paper(self, review_id: str, candidate: Candidate, revision: dict) -> None:
        receipts = [
            job for job in self.repository.jobs(review_id, "reading")
            if job.get("kind") == "evidence" and job["candidate_id"] == candidate.id
            and job["source_version"] == revision["source_version"]
            and job.get("reading_policy") == _READING_POLICY
        ]
        if not any(job.get("partial_summary") for job in receipts):
            return
        if self._current_summary(review_id, candidate.id, revision["source_version"], receipts):
            return
        self._paper_operation(review_id, candidate.id, "Combining and source-checking selective paper summary")
        prepared = self.jobs.prepare(self._spec(review_id).main_model, "literature-paper-summary", SelectiveSummary)
        frame = {"title": candidate.title, **revision, "reading_policy": _READING_POLICY,
                 "summary_policy": _SUMMARY_POLICY, "stage": "combine",
                 "coverage": "Selective admitted source spans only; unread pages are not evidence."}
        records = [{"sections": job["partial_summary"], "limitations": job["limitations"]}
                   for job in receipts if job.get("partial_summary")]
        parts = []
        while True:
            for packet in self.jobs.packets(records, frame, prepared):
                ids = {identifier for record in packet["records"] for section in record["sections"]
                       for identifier in section["evidence_ids"]}
                parts.append(await self.jobs.call(review_id, "reading", "combine-paper-summary", packet,
                                                  prepared, SelectiveSummary,
                                                  lambda output: self._validate_selective_summary(output, ids)))
            if len(parts) == 1:
                break
            next_packets = self.jobs.packets(parts, {**frame, "stage": "reduction"}, prepared)
            if len(next_packets) >= len(parts):
                raise ValidationError("Selective paper summary cannot be reduced within the selected model context.")
            records, parts = parts, []
            frame = {**frame, "stage": "reduction"}
        summary = parts[0]
        claims = {claim["id"]: claim for job in receipts for claim in job["claims"]}
        sections, limitations = [], list(summary["limitations"])
        for section in summary["sections"]:
            supported = []
            for packet in self.jobs.packets([claims[identifier] for identifier in section["evidence_ids"]],
                                           {**frame, "stage": "audit", "proposal": section}, prepared):
                ids = {claim["id"] for claim in packet["records"]}
                audited = await self.jobs.call(review_id, "reading", "audit-paper-summary", packet,
                                               prepared, SelectiveSummary,
                                               lambda output: self._validate_selective_summary(output, ids, section["title"]))
                supported.extend(audited["sections"])
                limitations.extend(audited["limitations"])
            sections.extend(supported)
        if not sections:
            raise ValidationError("No selective paper summary survived original-source checking.")
        summary = {"sections": sections, "limitations": list(dict.fromkeys([
            "Selective summary of admitted spans, not full-paper coverage.", *limitations,
        ]))}
        key = f"paper-summary:{candidate.id}:{revision['source_version']}:{_SUMMARY_POLICY}:{digest(receipts)}"
        self.repository.save_job(review_id, key, "reading", {
            "key": key, "kind": "selective_summary", "status": "completed",
            "candidate_id": candidate.id, **revision, "reading_policy": _READING_POLICY,
            "summary_policy": _SUMMARY_POLICY, "receipt_keys": [job["key"] for job in receipts], "summary": summary,
        })
        self.jobs.checkpoint(review_id)
        self._publish_progress(review_id)

    def _evidence(self, review_id: str) -> list[dict]:
        candidates = {item.id: item for item in [*self._candidates(review_id, selected=True),
                                                *self._supplemental_candidates(review_id)]}
        versions: dict[str, str] = {}
        for item in candidates.values():
            document = self.documents.get_document(item.document_id) if item.document_id else None
            if document and document.status == "ready":
                versions[item.id] = self.documents.source_revision(item.document_id)["source_version"]
        return [
            claim for job in self.repository.jobs(review_id, "reading")
            if job.get("kind") == "evidence" and job.get("source_version") == versions.get(job.get("candidate_id"))
            and job.get("reading_policy") == _READING_POLICY
            for claim in job["claims"]
        ]

    def _validate_synthesis(self, result: dict, evidence_ids: set[str]) -> None:
        for section in result["sections"]:
            if not section["evidence_ids"] or not set(section["evidence_ids"]).issubset(evidence_ids):
                raise ValidationError("A synthesis section has missing or unknown source evidence.")
        for gap in result["gaps"]:
            if not gap["evidence_ids"] or not set([*gap["evidence_ids"], *gap["counter_evidence_ids"]]).issubset(evidence_ids):
                raise ValidationError("A gap hypothesis has missing or unknown source evidence.")
            if not gap["caveat"].strip() or not gap["next_check"].strip():
                raise ValidationError("A gap hypothesis needs an uncertainty caveat and a next check.")

    async def _synthesis(self, review_id: str) -> None:
        evidence = self._evidence(review_id)
        if not evidence:
            raise ValidationError("Current source evidence is missing or changed. Resume reading before synthesizing.")
        spec = self._spec(review_id)
        self.jobs.admit_work(review_id, "comparison_passes", digest([evidence, self._frame(review_id), spec.main_model.model_dump()]))
        prepared = self.jobs.prepare(spec.main_model, "literature-synthesis", Synthesis)
        frame = {**self._frame(review_id), "stage": "synthesis", "coverage": "Selective approved corpus; not exhaustive."}
        context = self.repository.job(review_id, "track-context")
        if context is not None:
            frame["track_context"] = context["context"]
        parts = []
        for packet in self.jobs.packets(evidence, frame, prepared):
            candidate_ids = {row["candidate_id"] for row in packet["records"]}
            ids = {row["id"] for row in packet["records"]}
            summaries = []
            for candidate_id in candidate_ids:
                paper = self.paper(review_id, candidate_id)
                if paper.selective_summary:
                    sections = [section for section in paper.selective_summary["sections"]
                                if set(section["evidence_ids"]) <= ids]
                    if sections:
                        summaries.append({"candidate_id": candidate_id, **paper.selective_summary, "sections": sections})
            # Summaries are leads; the same packet's original evidence remains the audit authority.
            summary_frame = {**frame, "stage": "paper-summary-context"}
            for context_packet in self.jobs.packets(summaries, summary_frame, prepared):
                packet_with_summaries = {**packet, "paper_summaries": context_packet["records"]}
                if prepared[1].count(prepared[0].entry_agent.instructions, json_text(packet_with_summaries)) <= prepared[1].limit:
                    packet = packet_with_summaries
                    break
            result = await self.jobs.call(
                review_id, "synthesis", "synthesize", packet, prepared, Synthesis,
                lambda output: self._validate_synthesis(output, ids),
            )
            parts.append(result)
        while len(parts) > 1:
            packets = self.jobs.packets(parts, {**frame, "stage": "reduction"}, prepared)
            if len(packets) >= len(parts):
                raise ValidationError("Synthesis cannot be reduced within the selected context; use a larger model context.")
            reduced = []
            for packet in packets:
                ids = {
                    identifier for part in packet["records"] for unit in [*part["sections"], *part["gaps"]]
                    for identifier in [*unit["evidence_ids"], *unit.get("counter_evidence_ids", [])]
                }
                result = await self.jobs.call(
                    review_id, "synthesis", "reduce", packet, prepared, Synthesis,
                    lambda output: self._validate_synthesis(output, ids),
                )
                reduced.append(result)
            parts = reduced
        synthesis = parts[0]
        if not synthesis["sections"]:
            raise ValidationError("The model returned no substantive literature synthesis.")
        # Audit one section/gap at a time against original quotes, not a summary of a summary.
        audited_sections, audited_gaps, limitations = [], [], list(synthesis["limitations"])
        by_id = {row["id"]: row for row in evidence}
        units = [("section", section) for section in synthesis["sections"]] + [("gap", gap) for gap in synthesis["gaps"]]
        for kind, unit in units:
            ids = list(dict.fromkeys([*unit["evidence_ids"], *unit.get("counter_evidence_ids", [])]))
            audit_frame = {**frame, "stage": "audit", "proposal": {kind: unit}}
            audit_packets = self.jobs.packets([by_id[identifier] for identifier in ids], audit_frame, prepared)
            for packet in audit_packets:
                admitted_ids = {row["id"] for row in packet["records"]}
                result = await self.jobs.call(
                    review_id, "synthesis", "audit", packet, prepared, Synthesis,
                    lambda output: self._validate_synthesis(output, admitted_ids),
                )
                audited_sections.extend(result["sections"])
                audited_gaps.extend(result["gaps"])
                limitations.extend(result["limitations"])
        if not audited_sections:
            raise ValidationError("Source auditing retained no supported review sections.")
        self.jobs.checkpoint(review_id, synthesis={
            "sections": list({digest(item): item for item in audited_sections}.values()),
            "gaps": list({digest(item): item for item in audited_gaps}.values()),
            "limitations": list(dict.fromkeys(limitations)),
        })

    async def _publication(self, review_id: str) -> None:
        record = self.repository.get(review_id)
        synthesis = record.state.get("synthesis")
        if not synthesis:
            raise ValidationError("No audited synthesis is ready to save.")
        evidence = self._evidence(review_id)
        self._validate_synthesis(synthesis, {item["id"] for item in evidence})
        content = self._render(review_id, synthesis, evidence)
        marker = f"<!-- scholarweave-literature-review:{review_id}:{digest(content)} -->"
        body = f"{marker}\n{content}"
        spec = self._spec(review_id)
        tag = f"literature-review:{review_id}"
        artifact_key = f"publication-artifact:{digest(content)}"
        artifact = {"key": artifact_key, "kind": "publication_artifact", "status": "pending",
                    "body": body, "track_id": spec.track_id}
        previous_artifact = self.repository.job(review_id, artifact_key)
        paper_ids = sorted({
            item.document_id for item in self._candidates(review_id, selected=True)
            if item.document_id and self.documents.get_document(item.document_id) is not None
        })
        if spec.track_id:
            artifact["added_paper_ids"] = (
                previous_artifact.get("added_paper_ids", []) if previous_artifact else
                sorted(set(paper_ids) - set(self.tracks.get(spec.track_id).paper_ids))
            )
        if not previous_artifact:
            self.repository.save_job(review_id, artifact_key, "publication", artifact)
        if spec.track_id:
            saved = self.tracks.append_literature_review(
                spec.track_id, body, marker=marker,
                paper_ids=paper_ids,
            )
            path = saved.path
        else:
            existing = self.workspace.search(tags=[tag], limit=100, offset=0)
            matching = next((item for item in existing if marker in str(self.workspace.read_file(item.path).content)), None)
            if matching:
                path = matching.path
            else:
                saved = self.workspace.create_note(
                    name=spec.title, content=f"{marker}\n{content.split(chr(10), 1)[1].lstrip()}", tags=[tag, "literature-review"],
                    idempotency_key=f"literature-review:{review_id}:{digest(content)}",
                )
                path = saved.path
        self.repository.save_job(review_id, artifact_key, "publication", {
            **artifact, "status": "completed", "path": path,
            "sha256": (previous_artifact or {}).get("sha256") or self.workspace.read_file(path).sha256,
        })
        self.jobs.checkpoint(review_id, output_path=path, output_hash=digest(content))

    def _render(self, review_id: str, synthesis: dict, evidence: list[dict]) -> str:
        record = self.repository.get(review_id)
        spec = self._spec(review_id)
        scope = ReviewScope.model_validate(record.state["scope"])
        by_id = {item["id"]: item for item in evidence}
        candidates = [*self._candidates(review_id, selected=True), *self._supplemental_candidates(review_id)]
        edition = digest([synthesis, evidence, scope.model_dump(), [item.model_dump() for item in candidates]])[:12]
        labels = {identifier: f"R{review_id[:8]}-{edition}-E{index + 1}" for index, identifier in enumerate(sorted(by_id))}

        def citations(identifiers: list[str]) -> str:
            return " ".join(f"[^{labels[identifier]}]" for identifier in dict.fromkeys(identifiers))

        def findings(claims: list[dict], *, table: bool = False) -> str:
            parts = []
            for claim in claims:
                value = claim["finding"]
                if table:
                    value = value.replace("\\", "\\\\").replace("|", r"\|").replace("\n", " ")
                parts.append(f"{value} {citations([claim['id']])}")
            return (" " if table else "\n\n").join(parts) or "Not established from the inspected pages."

        lines = [
            f"# {spec.title}", "",
            "> Selective evidence, not a complete paper summary. Existing canonical summaries and notes remain separate and unchanged.",
            "", "## Key findings", "",
        ]
        for section in synthesis["sections"]:
            lines.extend([f"### {section['title']}", "", section["content"], "", citations(section["evidence_ids"]), ""])
        lines.extend(["## Next checks", "", "These are suggested checks, not completed experiments or established claims of novelty.", ""])
        if not synthesis["gaps"]:
            lines.append("No sufficiently supported research gap was established from the inspected evidence.")
        bases = {"author_stated": "Author-stated limitation", "inferred": "Assistant inference", "coverage_gap": "Not covered by the inspected sources"}
        for gap in synthesis["gaps"]:
            lines.extend([
                f"### {gap['hypothesis']}", "", f"{bases[gap['basis']]}. {citations(gap['evidence_ids'])}",
                f"Limit: {gap['caveat']}", f"Next check: {gap['next_check']}", "",
            ])
            if gap["counter_evidence_ids"]:
                lines.append(f"Counter-evidence: {citations(gap['counter_evidence_ids'])}")
        lines.extend([
            "", "## Scope and method", "", spec.objective, "",
            "This is a selective review of researcher-approved papers, not an exhaustive systematic review or a guarantee of novelty.",
            "", "Research questions:", *[f"- {question}" for question in scope.questions],
            "", "Search strategy:", *[f"- {query.provider}: {query.query} (planned pages: {query.pages})" for query in scope.queries],
            "", "Inclusion criteria:", *[f"- {criterion}" for criterion in scope.inclusion_criteria],
            "", "Exclusion criteria:", *[f"- {criterion}" for criterion in scope.exclusion_criteria],
            "", "## Paper comparison and coverage", "",
            "| Paper / year | Summary of inspected evidence | Method / setup | Findings | Limitations | Relevance | Sources / coverage |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ])
        from_path = (
            self.tracks.read_section(spec.track_id, "literature").path if spec.track_id
            else f"{WorkspaceLayout.knowledge_root}/review.md"
        )
        details = []
        for candidate in candidates:
            claims = [item for item in evidence if item["candidate_id"] == candidate.id]
            coverage = self.paper(review_id, candidate.id)
            screening = candidate.screening
            title = candidate.title.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]").replace("|", "\\|").replace("\n", " ")
            target = source_url(candidate.url) or (f"/library?paper={candidate.document_id}" if candidate.document_id else "")
            link = f"[{title}]({quote(target, safe=':/?&=+#%')})" if target else title
            relevance = (
                f"{screening.relevance}: {screening.reason} (metadata screening)"
                if screening else "Not screened"
            ).replace("|", r"\|").replace("\n", " ")
            links = self.tracks.paper_links(candidate.document_id, from_path=from_path) if (
                candidate.document_id and self.documents.get_document(candidate.document_id) is not None
            ) else "No local paper"
            coverage_text = (
                f"Fully read pages: {', '.join(map(str, coverage.read_pages)) or 'none'}; "
                f"partially read pages: {', '.join(map(str, coverage.partial_pages)) or 'none'}. "
                "See unread pages below."
            )
            groups = {category: [claim for claim in claims if claim["category"] == category]
                      for category in ("context", "method", "result", "limitation", "contradiction")}
            summary_claims = [group[0] for group in groups.values() if group][:4]
            lines.append(
                f"| {link} ({candidate.year or 'year unknown'}) | {findings(summary_claims, table=True)} "
                f"| {findings(groups['method'][:2], table=True)} | {findings(groups['result'][:2], table=True)} "
                f"| {findings([*groups['limitation'], *groups['contradiction']][:2], table=True)} "
                f"| {relevance} | {links}. {coverage_text} |"
            )
            details.extend([
                "", f"### {title} ({candidate.year or 'year unknown'})", "",
                f"Canonical sources: {links}", "", f"Coverage: {coverage_text}", "",
            ])
            if coverage.selective_summary:
                details.extend(["> Selective paper summary; not full-paper coverage.", ""])
                for heading in ("Motivation, contribution, and novelty", "Method", "Results", "Open questions"):
                    details.extend([f"#### {heading}", ""])
                    sections = [section for section in coverage.selective_summary["sections"] if section["title"] == heading]
                    for section in sections:
                        details.extend([section["content"], "", citations(section["evidence_ids"]), ""])
                    if not sections:
                        details.extend(["Not established from the inspected pages.", ""])
                details.extend(f"- {value}" for value in coverage.selective_summary["limitations"])
                details.extend(["", "#### Review follow-up checks", ""])
            else:
                details.extend([
                "#### Motivation, contribution, and novelty", "", findings(groups["context"]), "",
                "#### Method", "", findings(groups["method"]), "",
                "#### Results", "", findings([*groups["result"], *groups["limitation"], *groups["contradiction"]]), "",
                "#### Open questions", "",
                ])
            paper_ids = {item["id"] for item in claims}
            questions = [gap for gap in synthesis["gaps"] if paper_ids.intersection(
                [*gap["evidence_ids"], *gap["counter_evidence_ids"]]
            )]
            if questions:
                details.extend(
                    f"- Suggested check: {gap['next_check']} Limit: {gap['caveat']} "
                    f"{citations([identifier for identifier in gap['evidence_ids'] if identifier in paper_ids])}"
                    for gap in questions
                )
            else:
                details.append("No paper-specific follow-up was established. Unread pages may contain relevant qualifications.")
        lines.extend(["", "## Paper evidence notes", "", *details])
        lines.extend(["", "## Limitations and unfinished work", ""])
        lines.extend(f"- {value}" for value in [*synthesis["limitations"], *record.state.get("notices", [])])
        receipts = self.repository.jobs(review_id, "reading")
        for candidate in candidates:
            if candidate.error:
                lines.append(f"- {candidate.title}: {candidate.error}")
            coverage = self.paper(review_id, candidate.id)
            lines.extend(f"- {candidate.title}: {limitation}" for limitation in dict.fromkeys(
                limitation for receipt in receipts
                if receipt.get("kind") == "evidence" and receipt["candidate_id"] == candidate.id
                and receipt["source_version"] == coverage.source_version
                for limitation in receipt.get("limitations", [])
            ))
            for page in coverage.pages:
                if page.page in coverage.read_pages:
                    continue
                status = "Partially read" if page.page in coverage.partial_pages else "Deferred" if page.action == "defer" else "Unread"
                lines.append(f"- {status} {candidate.title}, p.{page.page}: {page.reason}")
            if not coverage.pages:
                lines.append(f"- {candidate.title}: no current full-text reading plan.")
        lines.extend(["", "## Evidence and bibliography", ""])
        by_candidate = {item.id: item for item in candidates}
        for identifier, label in labels.items():
            item = by_id[identifier]
            candidate = by_candidate[item["candidate_id"]]
            authors = ", ".join(candidate.authors) or "Authors unavailable"
            target = source_url(candidate.url)
            source = f" [Publication]({quote(target, safe=':/?&=+#%')})." if target else ""
            quotation = item["quote"].replace("\n", "\n    ")
            lines.append(
                f"[^{label}]: {authors} ({candidate.year or 'year unknown'}). {item['title']}, {item['citation']}. "
                f"DOI: {candidate.doi or 'unavailable'}.{source} "
                f"(document {item['document_id']}, source {item['source_version']}). \"{quotation}\""
            )
        return "\n".join(lines)
