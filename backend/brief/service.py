from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from openai import OpenAIError

from backend.agents.blueprint import ModelReferenceSpec
from backend.brief.catalog import SOURCES, TOPICS, BriefSource
from backend.brief.collection import canonical_url, parse_feed, parse_headlines
from backend.brief.schemas import (
    BriefCatalogResponse, BriefEditionResponse, BriefItemResponse, BriefPreferences,
    BriefPreviewResponse, BriefSourceResponse, BriefSourceStatus, BriefTopicResponse,
    BriefHighlightSummary, BriefSummaryState,
)
from backend.brief.summaries import BriefSummaryService
from backend.core.errors import NotFoundError, ValidationError
from backend.documents.service import DocumentService
from backend.documents.models import Document
from backend.persistence.files import SafeStorage
from backend.research.sources import SourceDownloadService, _arxiv_key
from backend.utils import utcnow
from backend.workspace.service import WorkspaceService

_PREFERENCES_FILE = "brief-preferences.json"
_PREVIEW_BUDGET = 128 * 1024 * 1024
_CACHE_FOLDER = "temp/morning-brief"
_CACHE_FILE = f"{_CACHE_FOLDER}/edition.json"
_logger = logging.getLogger(__name__)


class _BriefCache(BaseModel):
    edition: BriefEditionResponse
    dismissed: list[str] = Field(default_factory=list)


class BriefService:
    def __init__(
        self, storage: SafeStorage, downloads: SourceDownloadService,
        documents: DocumentService, workspace: WorkspaceService,
        summaries: BriefSummaryService | None = None,
    ) -> None:
        self._storage = storage
        self._downloads = downloads
        self._documents = documents
        self._workspace = workspace
        self._summaries = summaries
        self._summary_task: asyncio.Task[None] | None = None
        self._summary_tasks: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()
        self._edition = BriefEditionResponse()
        self._previews: dict[str, tuple[bytes, str]] = {}
        self._dismissed: set[str] = set()
        self._persisted_previews: set[str] = set()
        self._preferences = BriefPreferences()
        path = storage.resolve_path(storage.settings.data_dir, _PREFERENCES_FILE)
        if path.exists():
            self._preferences = BriefPreferences.model_validate_json(
                storage.read_text(storage.settings.data_dir, _PREFERENCES_FILE)
            )
        self._restore()

    @staticmethod
    def _preview_path(item_id: str) -> str:
        return f"{_CACHE_FOLDER}/{hashlib.sha256(item_id.encode()).hexdigest()}.bin"

    def _preview_limit(self, item: BriefItemResponse) -> int:
        return (
            self._storage.settings.max_upload_bytes if item.kind == "paper"
            else self._storage.settings.max_web_source_bytes
        )

    def _restore(self) -> None:
        if not self._storage.resolve_path(self._storage.settings.data_dir, _CACHE_FILE).exists():
            self._storage.delete_stored_tree(self._storage.settings.data_dir, _CACHE_FOLDER)
            if self._summaries is not None:
                self._summaries.clear_temporary()
            return
        try:
            cache = _BriefCache.model_validate_json(
                self._storage.read_text(self._storage.settings.data_dir, _CACHE_FILE)
            )
        except (ValueError, OSError) as exc:
            _logger.warning("Could not restore the temporary Morning Brief cache; clearing it: %s", exc)
            self._storage.delete_stored_tree(self._storage.settings.data_dir, _CACHE_FOLDER)
            if self._summaries is not None:
                self._summaries.clear_temporary()
            return
        self._edition = cache.edition
        self._dismissed = set(cache.dismissed)
        self._expire()
        if self._edition.highlights.status == "running":
            self._edition.highlights.status = "interrupted"
            self._edition.highlights.error = "Highlights generation stopped with the server. Generate again to retry."
        for item in self._edition.items:
            if not item.downloaded:
                continue
            try:
                content = self._storage.read_stored_bytes(
                    self._storage.settings.data_dir, self._preview_path(item.id),
                    max_bytes=self._preview_limit(item),
                )
                if len(content) != item.size_bytes or sum(len(value[0]) for value in self._previews.values()) + len(content) > _PREVIEW_BUDGET:
                    raise ValueError("Temporary preview size does not match the cached edition or exceeds its budget.")
            except (ValueError, OSError) as exc:
                _logger.warning("Could not restore Morning Brief preview %s: %s", item.id, exc)
                item.downloaded, item.size_bytes = False, 0
                continue
            self._previews[item.id] = (content, "application/pdf" if item.kind == "paper" else "text/plain")
            self._persisted_previews.add(item.id)
        if self._edition.day:
            self._persist()

    def _persist(self) -> None:
        items = {item.id: item for item in self._edition.items}
        for item_id, (content, _) in self._previews.items():
            if item_id not in self._persisted_previews:
                self._storage.write_bytes_atomic(
                    self._storage.settings.data_dir, self._preview_path(item_id), content,
                    max_bytes=self._preview_limit(items[item_id]),
                )
        self._storage.write_bytes_atomic(
            self._storage.settings.data_dir, _CACHE_FILE,
            _BriefCache(edition=self._edition, dismissed=sorted(self._dismissed)).model_dump_json().encode("utf-8"),
        )
        self._persisted_previews = set(self._previews)
        active_files = {self._preview_path(item_id).rsplit("/", 1)[-1] for item_id in self._previews}
        folder = self._storage.resolve_path(self._storage.settings.data_dir, _CACHE_FOLDER)
        for path in folder.glob("*.bin"):
            if path.name not in active_files:
                self._storage.delete_stored_file(self._storage.settings.data_dir, f"{_CACHE_FOLDER}/{path.name}")

    def catalog(self) -> BriefCatalogResponse:
        return BriefCatalogResponse(
            sources=[BriefSourceResponse(id=s.id, name=s.name, url=s.url, kind=s.kind) for s in SOURCES],
            topics=[BriefTopicResponse(id=key, label=value[0]) for key, value in TOPICS.items()],
            preferences=self._preferences.model_copy(deep=True),
            summary_model_reference=self._summary_reference(),
        )

    def _summary_reference(self) -> ModelReferenceSpec:
        return (
            self._preferences.summary_model_reference
            or (self._summaries.default_reference() if self._summaries else ModelReferenceSpec())
        )

    def _cancel_summary(self) -> None:
        if self._summary_task is not None:
            self._summary_task.cancel()
            self._summary_task = None
        if self._edition.highlights.status == "running":
            self._edition.highlights.status = "interrupted"
            self._edition.highlights.error = "Highlights generation was stopped. Generate again to retry."

    def _start_summary(self, reference: ModelReferenceSpec, *, force: bool = False) -> None:
        if self._summaries is None:
            raise ValidationError("Morning Brief highlights are not configured.")
        prior = self._edition.highlights.summary
        self._cancel_summary()
        if (
            not force and prior is not None and prior.model_reference == reference
            and prior.source_hash == self._summaries.source_hash(self._edition)
        ):
            self._edition.highlights = BriefSummaryState(status="ready", summary=prior)
            self._persist()
            return
        self._summaries.clear_temporary()
        self._edition.highlights = BriefSummaryState(status="running", summary=prior)
        self._persist()
        self._summary_task = asyncio.create_task(
            self._generate_summary(self._edition.model_copy(deep=True), reference),
            name="morning-brief-highlights",
        )
        self._summary_tasks.add(self._summary_task)
        self._summary_task.add_done_callback(self._summary_tasks.discard)

    async def _generate_summary(self, edition: BriefEditionResponse, reference: ModelReferenceSpec) -> None:
        assert self._summaries is not None
        task = asyncio.current_task()

        async def progress(completed: int, total: int) -> None:
            async with self._lock:
                self._expire()
                if self._summary_task is task:
                    self._edition.highlights.completed_parts = completed
                    self._edition.highlights.total_parts = total
                    self._persist()

        try:
            summary = await self._summaries.generate(edition, reference, progress)
            async with self._lock:
                self._expire()
                if self._summary_task is not task:
                    return
                self._summaries.save(summary)
                state = self._edition.highlights
                self._edition.highlights = BriefSummaryState(
                    status="ready", summary=summary,
                    completed_parts=state.completed_parts, total_parts=state.total_parts,
                )
                self._persist()
                self._summary_task = None
        except (RuntimeError, ValueError, OSError, OpenAIError) as exc:
            _logger.warning("Morning Brief highlights failed: %s", exc)
            async with self._lock:
                if self._summary_task is task:
                    self._edition.highlights.status = "failed"
                    self._edition.highlights.error = str(exc)
                    self._persist()
                    self._summary_task = None

    async def generate_highlights(self, reference: ModelReferenceSpec | None = None) -> BriefEditionResponse:
        async with self._lock:
            self._expire()
            if not self._edition.items:
                raise ValidationError("Collect headlines before generating highlights.")
            if self._summary_task is not None:
                raise ValidationError("Highlights are already being generated.")
            self._start_summary(reference or self._summary_reference(), force=True)
            return self._snapshot()

    def summary_history(self, *, limit: int, offset: int) -> list[BriefHighlightSummary]:
        if self._summaries is None:
            raise ValidationError("Morning Brief highlights are not configured.")
        return self._summaries.history(limit=limit, offset=offset)

    async def preferences(self, preferences: BriefPreferences) -> BriefCatalogResponse:
        async with self._lock:
            self._storage.write_bytes_atomic(
                self._storage.settings.data_dir, _PREFERENCES_FILE,
                preferences.model_dump_json().encode("utf-8"),
            )
            self._preferences = preferences.model_copy(deep=True)
            return self.catalog()

    def _expire(self) -> None:
        if self._edition.day and self._edition.day != self._day():
            self._cancel_summary()
            if self._summaries is not None:
                self._summaries.clear_temporary()
            self._storage.delete_stored_tree(self._storage.settings.data_dir, _CACHE_FOLDER)
            self._previews.clear()
            self._persisted_previews.clear()
            self._dismissed.clear()
            self._edition = BriefEditionResponse()

    def _day(self) -> str:
        return utcnow().astimezone(ZoneInfo(self._storage.settings.user_timezone)).date().isoformat()

    def _snapshot(self) -> BriefEditionResponse:
        snapshot = self._edition.model_copy(deep=True)
        snapshot.temporary_bytes = sum(len(value[0]) for value in self._previews.values())
        documents = self._documents.list_documents() if any(item.kind == "paper" for item in snapshot.items) else []
        saved_paths = self._workspace.paths_for_tags([
            self._tag(item) for item in snapshot.items if item.kind != "paper"
        ])
        for item in snapshot.items:
            self._saved(item, documents=documents, saved_paths=saved_paths)
        return snapshot

    async def edition(self) -> BriefEditionResponse:
        async with self._lock:
            self._expire()
            if (
                self._edition.items and self._edition.highlights.status == "idle"
                and self._summaries is not None and self._summary_reference().model
            ):
                self._start_summary(self._summary_reference())
            return self._snapshot()

    async def refresh(self, *, force: bool = False) -> BriefEditionResponse:
        async with self._lock:
            self._expire()
            if self._edition.day and not force:
                return self._snapshot()
            preferences = self._preferences
            semaphore = asyncio.Semaphore(4)

            async def collect(source: BriefSource) -> tuple[list[BriefItemResponse], BriefSourceStatus]:
                async with semaphore:
                    now = utcnow()
                    try:
                        content, _, _ = await self._downloads.fetch_resource(
                            source.url, max_bytes=self._storage.settings.max_web_source_bytes
                        )
                        if source.id == "deepseek":
                            archive = re.search(rb'href="(/news/news\d+)"', content)
                            if archive:
                                content, _, _ = await self._downloads.fetch_resource(
                                    "https://api-docs.deepseek.com" + archive.group(1).decode("ascii"),
                                    max_bytes=self._storage.settings.max_web_source_bytes,
                                )
                        items = parse_headlines(content, source) if source.format == "html" else parse_feed(content, source)
                        cutoff = now - timedelta(days=preferences.lookback_days)
                        items = [item for item in items if item.published_at is None or cutoff <= item.published_at <= now + timedelta(days=1)]
                        return items, BriefSourceStatus(
                            source_id=source.id, name=source.name, fetched_at=now, count=len(items),
                            warning="Some index headlines have no publication date; these are not verified new." if any(item.published_at is None for item in items) else None,
                        )
                    except (RuntimeError, ValueError, ET.ParseError) as exc:
                        return [], BriefSourceStatus(
                            source_id=source.id, name=source.name, fetched_at=now, count=0, error=str(exc),
                        )

            results = await asyncio.gather(*(
                collect(source) for source in SOURCES if source.id in preferences.source_ids
            ))
            existing = {self._key(item): item for item in self._edition.items}
            unique: dict[str, BriefItemResponse] = {}
            for items, source_status in results:
                for item in items:
                    key = self._key(item)
                    if key in unique or key in self._dismissed:
                        continue
                    text = f"{item.title} {item.abstract}".casefold()
                    if any(self._matches(term, text) for term in preferences.exclude_terms):
                        continue
                    item.topics = [
                        TOPICS[topic_id][0] for topic_id in preferences.topic_ids
                        if any(self._matches(phrase, text) for phrase in TOPICS[topic_id][1])
                    ]
                    extra = [phrase.strip() for phrase in preferences.extra_interests.split(",") if phrase.strip()]
                    if any(self._matches(phrase, text) for phrase in extra):
                        item.topics.append("Your interests")
                    if item.kind == "paper" and not item.topics:
                        continue
                    prior = existing.get(key)
                    if prior and item.arxiv_id != prior.arxiv_id:
                        prior = None
                    item.id = prior.id if prior else uuid.uuid4().hex
                    if prior:
                        item.downloaded, item.size_bytes = prior.downloaded, prior.size_bytes
                        item.saved_document_id, item.saved_path = prior.saved_document_id, prior.saved_path
                        item.preview_note = prior.preview_note
                    unique[key] = item
                # A failed refresh retains only that source's previous headlines/previews.
                if source_status.error:
                    for key, item in existing.items():
                        if item.source_id == source_status.source_id:
                            unique[key] = item
            epoch = datetime.min.replace(tzinfo=utcnow().tzinfo)
            selected = sorted(unique.values(), key=lambda item: (
                bool(item.topics), item.published_at or epoch, len(item.topics),
            ), reverse=True)
            # Keep a bounded working set, with room for all four sections.
            retained: list[BriefItemResponse] = []
            for kind, limit in (("paper", 80), ("lab", 60), ("engineering", 20), ("reading", 20)):
                candidates = [item for item in selected if item.kind == kind]
                retained.extend(candidates[:limit])
                if len(candidates) > limit:
                    for _, source_status in results:
                        if any(item.source_id == source_status.source_id for item in candidates[limit:]):
                            source_status.warning = (
                                f"Showing a bounded selection of {limit} {kind} headlines. "
                                + (source_status.warning or "")
                            ).strip()
            active_ids = {item.id for item in retained}
            self._previews = {key: value for key, value in self._previews.items() if key in active_ids}
            self._edition = BriefEditionResponse(
                day=self._day(), fetched_at=utcnow(),
                items=retained, sources=[status for _, status in results],
                highlights=self._edition.highlights.model_copy(deep=True),
            )
            self._cancel_summary()
            if retained and self._summaries is not None and self._summary_reference().model:
                self._start_summary(self._summary_reference())
            self._persist()
            return self._snapshot()

    async def clear(self) -> BriefEditionResponse:
        async with self._lock:
            self._expire()
            self._cancel_summary()
            summary = self._edition.highlights.summary
            if self._summaries is not None:
                self._summaries.clear_temporary()
            self._previews.clear()
            self._dismissed.clear()
            self._edition = BriefEditionResponse(
                day=self._day(), cleared=True,
                highlights=BriefSummaryState(status="ready", summary=summary) if summary else BriefSummaryState(),
            )
            self._persist()
            return self._snapshot()

    async def close(self) -> None:
        async with self._lock:
            tasks = list(self._summary_tasks)
            self._cancel_summary()
            if self._edition.day:
                self._persist()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

    def _item(self, item_id: str) -> BriefItemResponse:
        self._expire()
        item = next((item for item in self._edition.items if item.id == item_id), None)
        if item is None:
            raise NotFoundError("This headline has been cleared or expired. Refresh the brief.")
        return item

    async def fetch(self, item_id: str) -> BriefPreviewResponse:
        async with self._lock:
            item = self._item(item_id)
            try:
                if item_id not in self._previews:
                    if item.kind == "paper":
                        assert item.arxiv_id
                        url = f"https://arxiv.org/pdf/{item.arxiv_id}"
                        existing = self._downloads.find_document(url)
                        if existing is not None:
                            item.saved_document_id = existing.id
                            artifact = next((
                                artifact for artifact in self._documents.get_document_artifacts(existing.id)
                                if artifact.kind == "source_pdf"
                            ), None)
                            if artifact is None:
                                raise ValidationError("The saved paper has no source PDF. Open its library entry to investigate.")
                            content = self._documents.artifact_bytes(artifact)
                            item.preview_note = "Using the PDF already in your library; it may be an earlier revision."
                        else:
                            content = await self._downloads.fetch_pdf_bytes(url)
                        media_type = "application/pdf"
                    else:
                        source = await self._downloads.download_web_page(item.url, cache=False, prefer_main=True)
                        content = source.text.encode("utf-8")
                        media_type = "text/plain"
                    if sum(len(value[0]) for value in self._previews.values()) + len(content) > _PREVIEW_BUDGET:
                        raise ValidationError("Temporary previews are full. Discard previews or clear the brief before downloading more.")
                    self._previews[item_id] = (content, media_type)
                    item.downloaded, item.size_bytes = True, len(content)
            except (RuntimeError, ValueError) as exc:
                raise ValidationError(f"Could not download this item: {exc}") from exc
            content, media_type = self._previews[item_id]
            self._saved(item)
            self._persist()
            return BriefPreviewResponse(
                item=item.model_copy(deep=True), media_type=media_type,
                text=content.decode("utf-8") if media_type == "text/plain" else None,
            )

    async def raw(self, item_id: str) -> tuple[bytes, str]:
        async with self._lock:
            self._item(item_id)
            if item_id not in self._previews:
                raise NotFoundError("Download this item before opening its preview.")
            return self._previews[item_id]

    async def discard(self, item_id: str) -> BriefEditionResponse:
        async with self._lock:
            item = self._item(item_id)
            self._dismissed.add(self._key(item))
            self._previews.pop(item_id, None)
            self._edition.items = [item for item in self._edition.items if item.id != item_id]
            self._persist()
            return self._snapshot()

    def _saved(
        self, item: BriefItemResponse, *, documents: Sequence[Document] | None = None,
        saved_paths: dict[str, str] | None = None,
    ) -> None:
        if item.kind == "paper":
            document = self._downloads.find_document(
                f"https://arxiv.org/abs/{item.arxiv_id}", documents=documents,
            )
            if document is None and item.saved_document_id:
                document = (
                    self._documents.get_document(item.saved_document_id) if documents is None
                    else next((document for document in documents if document.id == item.saved_document_id), None)
                )
            item.saved_document_id = document.id if document else None
        elif saved_paths is not None:
            item.saved_path = saved_paths.get(self._tag(item))
        else:
            results = self._workspace.search(tags=[self._tag(item)], limit=1)
            item.saved_path = results[0].path if results else None

    async def keep(self, item_id: str) -> BriefItemResponse:
        async with self._lock:
            item = self._item(item_id)
            self._saved(item)
            if item.saved_document_id or item.saved_path:
                return item.model_copy(deep=True)
            if item_id not in self._previews:
                raise ValidationError("Download and inspect the item before keeping it.")
            content, _ = self._previews[item_id]
            if item.kind == "paper":
                document = self._documents.find_by_source_hash(hashlib.sha256(content).hexdigest())
                if document is None:
                    document = self._documents.create_document_from_bytes(
                        content, filename=f"{item.arxiv_id}.pdf", title=item.title,
                        metadata={
                            "source_url": f"https://arxiv.org/pdf/{item.arxiv_id}",
                            "source_kind": "arxiv", "arxiv_id": item.arxiv_id,
                            "authors": item.authors, "abstract": item.abstract,
                            "downloaded_at": utcnow().isoformat(),
                        },
                    )
                item.saved_document_id = document.id
            else:
                note = self._workspace.create_note(
                    name=item.title,
                    content=f"Source: {item.url}\n\nPublisher: {item.source_name}\n\n"
                    f"Downloaded: {utcnow().isoformat()}\n\n"
                    f"---\n\n{content.decode('utf-8')}",
                    tags=["morning-brief", "web-source", self._tag(item)],
                )
                item.saved_path = note.path
            self._persist()
            return item.model_copy(deep=True)

    @staticmethod
    def _tag(item: BriefItemResponse) -> str:
        return f"brief-source:{hashlib.sha256(canonical_url(item.url).encode()).hexdigest()[:48]}"

    @staticmethod
    def _key(item: BriefItemResponse) -> str:
        return _arxiv_key(item.url) if item.kind == "paper" else canonical_url(item.url)

    @staticmethod
    def _matches(phrase: str, text: str) -> bool:
        return re.search(r"(?<!\w)" + re.escape(phrase.casefold()) + r"(?!\w)", text) is not None
