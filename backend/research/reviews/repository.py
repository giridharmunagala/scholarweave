from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any
from typing import Literal

from sqlalchemy import delete, func, or_, select, update

from backend.core.errors import ConflictError, NotFoundError, ValidationError
from backend.research.reviews.models import LiteratureReview, ReviewCandidate, ReviewJob, ScholarlyCache
from backend.research.reviews.schemas import Candidate
from backend.utils import as_utc, utcnow
from backend.persistence.files import SafeStorage

_RANK = {"HIGHLY_RELEVANT": 5, "RELEVANT": 4, "SOMEWHAT_RELEVANT": 2, "IRRELEVANT": 1}


class ReviewRepository:
    def __init__(self, session_factory: Any, storage: SafeStorage | None = None) -> None:
        self.sessions = session_factory
        self.storage = storage

    def _job_content(self, content: dict) -> dict:
        if "workspace_receipt" not in content:
            return deepcopy(content)
        if self.storage is None:
            raise ValidationError("Workspace storage is unavailable for this research receipt.")
        path = content["workspace_receipt"]
        text = self.storage.read_text(self.storage.settings.workspace_dir, path)
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != content["receipt_hash"]:
            raise ValidationError(f"Research receipt changed on disk: {path}. Restore it before resuming.")
        return json.loads(text)

    def create(self, identifier: str, specification: dict, state: dict) -> LiteratureReview:
        with self.sessions() as session:
            record = LiteratureReview(id=identifier, specification=specification, state=state)
            session.add(record)
            session.commit()
            return record

    def get(self, identifier: str) -> LiteratureReview:
        with self.sessions() as session:
            record = session.get(LiteratureReview, identifier)
            if record is None:
                raise NotFoundError("Literature review was not found.")
            return record

    def list(self) -> list[LiteratureReview]:
        with self.sessions() as session:
            return list(session.scalars(select(LiteratureReview).order_by(LiteratureReview.updated_at.desc())))

    def update(self, identifier: str, state: dict, revision: int) -> LiteratureReview:
        with self.sessions() as session:
            result = session.execute(
                update(LiteratureReview)
                .where(LiteratureReview.id == identifier, LiteratureReview.revision == revision)
                .values(state=deepcopy(state), revision=revision + 1, updated_at=utcnow())
            )
            if result.rowcount != 1:
                raise ConflictError("The review changed. Reload its current state before continuing.")
            session.commit()
        return self.get(identifier)

    def assign_track(self, identifier: str, track_id: str, revision: int) -> None:
        with self.sessions() as session:
            record = session.get(LiteratureReview, identifier)
            if record is None:
                raise NotFoundError("Literature review was not found.")
            result = session.execute(
                update(LiteratureReview)
                .where(LiteratureReview.id == identifier, LiteratureReview.revision == revision)
                .values(specification={**record.specification, "track_id": track_id},
                        revision=revision + 1, updated_at=utcnow())
            )
            if result.rowcount != 1:
                raise ConflictError("The review changed. Reload before moving it.")
            session.commit()

    def candidates(
        self, review_id: str, *, limit: int = 50, offset: int = 0, selected: bool = False,
        sort: Literal["relevance", "year", "title"] = "relevance",
        shortlist: bool = False,
    ) -> tuple[list[Candidate], int]:
        with self.sessions() as session:
            conditions = [ReviewCandidate.review_id == review_id]
            if shortlist or selected:
                relevance = func.json_extract(ReviewCandidate.content, "$.screening.relevance")
                conditions.append(or_(relevance.is_(None), relevance.not_in(["IRRELEVANT", "unrelated"])))
            if selected:
                conditions.append(ReviewCandidate.selected == 1)
            total = session.scalar(select(func.count()).select_from(ReviewCandidate).where(*conditions)) or 0
            ordering = {
                "relevance": ReviewCandidate.rank.desc(),
                "year": func.json_extract(ReviewCandidate.content, "$.year").desc(),
                "title": func.lower(func.json_extract(ReviewCandidate.content, "$.title")).asc(),
            }[sort]
            rows = session.scalars(
                select(ReviewCandidate).where(*conditions)
                .order_by(ordering, ReviewCandidate.id)
                .limit(limit).offset(offset)
            )
            return [Candidate.model_validate(row.content) for row in rows], total

    def screening_counts(self, review_id: str) -> tuple[int, int]:
        with self.sessions() as session:
            relevance = func.json_extract(ReviewCandidate.content, "$.screening.relevance")
            counts = dict(session.execute(
                select(relevance, func.count()).where(ReviewCandidate.review_id == review_id)
                .group_by(relevance),
            ).all())
            return (
                sum(count for rating, count in counts.items() if rating is not None),
                counts.get("IRRELEVANT", 0) + counts.get("unrelated", 0),
            )

    def selected_ids(self, review_id: str) -> list[str]:
        with self.sessions() as session:
            relevance = func.json_extract(ReviewCandidate.content, "$.screening.relevance")
            return list(session.scalars(select(ReviewCandidate.id).where(
                ReviewCandidate.review_id == review_id, ReviewCandidate.selected == 1,
                or_(relevance.is_(None), relevance.not_in(["IRRELEVANT", "unrelated"])),
            )))

    def document_used_elsewhere(self, review_id: str, document_id: str) -> bool:
        with self.sessions() as session:
            candidate_use = session.scalar(select(ReviewCandidate.id).where(
                ReviewCandidate.review_id != review_id,
                func.json_extract(ReviewCandidate.content, "$.document_id") == document_id,
            ).limit(1)) is not None
            return candidate_use or session.scalar(select(ReviewJob.id).where(
                ReviewJob.review_id != review_id,
                func.json_extract(ReviewJob.content, "$.reference.document_id") == document_id,
            ).limit(1)) is not None

    def delete(self, review_id: str) -> None:
        with self.sessions.begin() as session:
            session.execute(delete(ReviewJob).where(ReviewJob.review_id == review_id))
            session.execute(delete(ReviewCandidate).where(ReviewCandidate.review_id == review_id))
            session.execute(delete(LiteratureReview).where(LiteratureReview.id == review_id))

    def prune_topic_selection(self, review_id: str, topic_ids: set[str]) -> None:
        with self.sessions.begin() as session:
            session.execute(delete(ReviewCandidate).where(
                ReviewCandidate.review_id == review_id, ReviewCandidate.selected == 0,
            ))
            discarded = list(session.scalars(select(ReviewJob).where(
                ReviewJob.review_id == review_id,
                ReviewJob.stage.in_(("scope", "discovery", "screening")),
                ReviewJob.job_key != "topics",
            )))
            for row in discarded:
                if row.job_key.startswith("topic-exploration:") and row.job_key.rsplit(":", 1)[-1] in topic_ids:
                    continue
                # Keep usage accounting, not discarded model output or metadata.
                row.content = {
                    "key": row.job_key, "status": "pruned",
                    "tokens_spent": row.content.get("tokens_spent", 0),
                    "usage_estimated": row.content.get("usage_estimated", False),
                    **({"provenance": row.content["provenance"]} if "provenance" in row.content else {}),
                }
                row.stage = "workspace"

    def workspace_receipts(self, review_id: str) -> dict[str, str]:
        with self.sessions() as session:
            contents = session.scalars(select(ReviewJob.content).where(ReviewJob.review_id == review_id))
            return {row["workspace_receipt"]: row["receipt_hash"] for row in contents if "workspace_receipt" in row}

    def candidate(self, review_id: str, identifier: str) -> Candidate:
        with self.sessions() as session:
            row = session.get(ReviewCandidate, identifier)
            if row is None or row.review_id != review_id:
                raise NotFoundError("Review candidate was not found.")
            return Candidate.model_validate(row.content)

    def save_candidate(self, review_id: str, key: str, candidate: Candidate) -> Candidate:
        with self.sessions() as session:
            aliases = [
                func.json_extract(ReviewCandidate.content, f"$.{name}") == value
                for name in ("doi", "arxiv_id", "openalex_id", "document_id", "url")
                if (value := getattr(candidate, name))
            ]
            matches = list(session.scalars(select(ReviewCandidate).where(
                ReviewCandidate.review_id == review_id,
                or_(ReviewCandidate.candidate_key == key, *aliases),
            )))
            compatible = []
            for row in matches:
                previous = Candidate.model_validate(row.content)
                conflict = any(
                    getattr(previous, name) and getattr(candidate, name)
                    and getattr(previous, name) != getattr(candidate, name)
                    for name in ("doi", "arxiv_id", "openalex_id")
                )
                if not conflict:
                    compatible.append(row)
                elif row.id == candidate.id or row.candidate_key == key:
                    raise ValidationError("Conflicting publication identifiers need manual review; existing evidence was preserved.")
            compatible.sort(key=lambda row: (not row.selected, row.id))
            if sum(row.selected for row in compatible) > 1:
                raise ValidationError("Multiple approved candidates share this identity. Return to paper approval and choose one before consolidating them.")
            if compatible:
                existing = compatible[0]
                old = Candidate.model_validate(existing.content)
                for duplicate in compatible[1:]:
                    other = Candidate.model_validate(duplicate.content)
                    old = old.model_copy(update={
                        **{name: getattr(other, name) for name in Candidate.model_fields
                           if not getattr(old, name) and getattr(other, name) and name not in {"id", "selected"}},
                        "discovery": list(dict.fromkeys([*old.discovery, *other.discovery])),
                        "references": list(dict.fromkeys([*old.references, *other.references])),
                    })
                    session.delete(duplicate)
                values = {
                    name: getattr(candidate, name) for name in Candidate.model_fields
                    if name not in {"id", "selected", "discovery"} and getattr(candidate, name) not in (None, [], "")
                }
                candidate = old.model_copy(update={
                    **values, "discovery": list(dict.fromkeys([*old.discovery, *candidate.discovery])),
                    "references": list(dict.fromkeys([*old.references, *candidate.references])),
                })
                existing.content = candidate.model_dump(mode="json")
                existing.rank = _RANK.get(candidate.screening.relevance, 0) if candidate.screening else 0
            else:
                session.add(ReviewCandidate(
                    id=candidate.id, review_id=review_id, candidate_key=key,
                    content=candidate.model_dump(mode="json"), selected=int(candidate.selected),
                    rank=_RANK.get(candidate.screening.relevance, 0) if candidate.screening else 0,
                ))
            session.commit()
            return candidate

    def update_candidate(self, review_id: str, candidate: Candidate) -> None:
        with self.sessions() as session:
            row = session.get(ReviewCandidate, candidate.id)
            if row is None or row.review_id != review_id:
                raise NotFoundError("Review candidate was not found.")
            row.content = candidate.model_dump(mode="json")
            row.selected = int(candidate.selected)
            row.rank = _RANK.get(candidate.screening.relevance, 0) if candidate.screening else 0
            session.commit()

    def select_candidates(self, review_id: str, identifiers: list[str], *, revision: int, state: dict) -> None:
        with self.sessions() as session:
            changed = session.execute(
                update(LiteratureReview)
                .where(LiteratureReview.id == review_id, LiteratureReview.revision == revision)
                .values(state=state, revision=revision + 1, updated_at=utcnow())
            )
            if changed.rowcount != 1:
                raise ConflictError("The shortlist changed. Reload before approving papers.")
            rows = list(session.scalars(select(ReviewCandidate).where(ReviewCandidate.review_id == review_id)))
            if not set(identifiers).issubset({row.id for row in rows}):
                raise NotFoundError("An approved paper is not in this review's shortlist.")
            if any(row.id in identifiers and (row.content.get("screening") or {}).get("relevance")
                   in {"IRRELEVANT", "unrelated"} for row in rows):
                raise ValidationError("Irrelevant papers are automatically excluded from reading approval.")
            for row in rows:
                row.selected = int(row.id in identifiers)
                row.content = {**row.content, "selected": bool(row.selected)}
            session.commit()

    def link_document(self, review_id: str, candidate_id: str, document_id: str, *, revision: int, state: dict) -> None:
        with self.sessions() as session:
            changed = session.execute(
                update(LiteratureReview)
                .where(LiteratureReview.id == review_id, LiteratureReview.revision == revision)
                .values(state=state, revision=revision + 1, updated_at=utcnow())
            )
            if changed.rowcount != 1:
                raise ConflictError("The review changed. Reload before linking a local paper.")
            row = session.get(ReviewCandidate, candidate_id)
            if row is None or row.review_id != review_id:
                raise NotFoundError("Review candidate was not found.")
            row.content = {**row.content, "document_id": document_id, "error": None}
            session.commit()

    def job(self, review_id: str, key: str) -> dict | None:
        with self.sessions() as session:
            row = session.scalar(select(ReviewJob).where(
                ReviewJob.review_id == review_id, ReviewJob.job_key == key,
            ))
            return self._job_content(row.content) if row else None

    def jobs(self, review_id: str, stage: str | None = None, *, key_prefix: str | None = None) -> list[dict]:
        with self.sessions() as session:
            query = select(ReviewJob).where(ReviewJob.review_id == review_id)
            if stage:
                query = query.where(ReviewJob.stage == stage)
            if key_prefix is not None:
                query = query.where(ReviewJob.job_key.startswith(key_prefix))
            return [self._job_content(row.content) for row in session.scalars(query.order_by(ReviewJob.job_key))]

    def activity_page(self, review_id: str, *, limit: int, offset: int) -> tuple[list[dict], int]:
        conditions = (
            ReviewJob.review_id == review_id,
            ReviewJob.stage.not_in(("progress", "work_budget")),
            ReviewJob.job_key != "analysis-plan",
        )
        with self.sessions() as session:
            total = session.scalar(select(func.count()).select_from(ReviewJob).where(*conditions)) or 0
            rows = session.scalars(select(ReviewJob).where(*conditions).order_by(
                ReviewJob.updated_at.desc(), ReviewJob.job_key,
            ).offset(offset).limit(limit))
            return [{**self._job_content(row.content), "key": row.job_key, "stage": row.stage} for row in rows], total

    def running_run(self, review_id: str) -> str | None:
        run_id = func.json_extract(ReviewJob.content, "$.run_id")
        with self.sessions() as session:
            return session.scalar(select(run_id).where(
                ReviewJob.review_id == review_id, run_id.is_not(None),
                func.json_extract(ReviewJob.content, "$.status").in_(("starting", "running")),
            ).order_by(ReviewJob.job_key).limit(1))

    def save_job(self, review_id: str, key: str, stage: str, content: dict) -> None:
        identifier = hashlib.sha256(f"{review_id}:{key}".encode()).hexdigest()
        record = self.get(review_id)
        if record.specification.get("embedding_model"):
            if self.storage is None or not record.state.get("workspace_path"):
                raise ValidationError("Topic-led research requires its local workspace.")
            text = json.dumps(content, ensure_ascii=False, sort_keys=True)
            content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            path = f"{record.state['workspace_path']}/receipts/{identifier}-{content_hash}.json"
            try:
                existing_hash = self.storage.file_hash(self.storage.settings.workspace_dir, path)
            except FileNotFoundError:
                self.storage.write_bytes_atomic(self.storage.settings.workspace_dir, path, text.encode("utf-8"))
            else:
                if existing_hash != content_hash:
                    raise ValidationError(f"Research receipt changed on disk: {path}. Restore it before resuming.")
            content = {
                **{name: deepcopy(content[name]) for name in (
                    "key", "stage", "status", "kind", "candidate_id", "run_id", "work_kind",
                    "tokens_spent", "usage_estimated", "provenance",
                ) if name in content},
                "workspace_receipt": path, "receipt_hash": content_hash,
            }
        with self.sessions() as session:
            row = session.get(ReviewJob, identifier)
            if row is None:
                session.add(ReviewJob(
                    id=identifier, review_id=review_id, job_key=key, stage=stage, content=deepcopy(content),
                ))
            else:
                row.content = deepcopy(content)
                row.updated_at = utcnow()
            session.commit()

    def paper_jobs(self, review_id: str, candidate_id: str | None = None) -> list[dict]:
        with self.sessions() as session:
            query = select(ReviewJob).where(
                ReviewJob.review_id == review_id,
                or_(
                    ReviewJob.job_key.startswith("reading-plan:"),
                    ReviewJob.job_key.startswith("paper-operation:"),
                    ReviewJob.job_key.startswith("source-unavailable:"),
                    func.json_extract(ReviewJob.content, "$.kind").in_(("evidence", "analysis_evidence", "selective_summary")),
                ),
            )
            if candidate_id is not None:
                query = query.where(or_(
                    ReviewJob.job_key.startswith(f"reading-plan:{candidate_id}:"),
                    ReviewJob.job_key == f"paper-operation:{candidate_id}",
                    ReviewJob.job_key == f"source-unavailable:{candidate_id}",
                    func.json_extract(ReviewJob.content, "$.candidate_id") == candidate_id,
                ))
            return [self._job_content(row.content) for row in session.scalars(query.order_by(ReviewJob.job_key))]

    def cached(self, key: str, *, max_age_seconds: int = 86_400) -> dict | None:
        with self.sessions() as session:
            row = session.get(ScholarlyCache, key)
            if row and (utcnow() - as_utc(row.created_at)).total_seconds() <= max_age_seconds:
                return deepcopy(row.content)
            return None

    def cache(self, key: str, content: dict) -> None:
        with self.sessions() as session:
            row = session.get(ScholarlyCache, key)
            if row:
                row.content = deepcopy(content)
                row.created_at = utcnow()
            else:
                session.add(ScholarlyCache(key=key, content=deepcopy(content)))
            session.commit()
