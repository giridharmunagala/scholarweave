from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from backend.brief.catalog import SOURCES, TOPICS, BriefSource
from backend.brief.collection import canonical_url, parse_feed, parse_headlines
from backend.brief.schemas import (
    BriefCatalogResponse, BriefEditionResponse, BriefItemResponse, BriefPreferences,
    BriefPreviewResponse, BriefSourceResponse, BriefSourceStatus, BriefTopicResponse,
)
from backend.core.errors import NotFoundError, ValidationError
from backend.documents.service import DocumentService
from backend.persistence.files import SafeStorage
from backend.research.sources import SourceDownloadService, _arxiv_key
from backend.utils import utcnow
from backend.workspace.service import WorkspaceService

_PREFERENCES_FILE = "brief-preferences.json"
_PREVIEW_BUDGET = 128 * 1024 * 1024


class BriefService:
    def __init__(
        self, storage: SafeStorage, downloads: SourceDownloadService,
        documents: DocumentService, workspace: WorkspaceService,
    ) -> None:
        self._storage = storage
        self._downloads = downloads
        self._documents = documents
        self._workspace = workspace
        self._lock = asyncio.Lock()
        self._edition = BriefEditionResponse()
        self._previews: dict[str, tuple[bytes, str]] = {}
        self._dismissed: set[str] = set()
        self._preferences = BriefPreferences()
        path = storage.resolve_path(storage.settings.data_dir, _PREFERENCES_FILE)
        if path.exists():
            self._preferences = BriefPreferences.model_validate_json(
                storage.read_text(storage.settings.data_dir, _PREFERENCES_FILE)
            )

    def catalog(self) -> BriefCatalogResponse:
        return BriefCatalogResponse(
            sources=[BriefSourceResponse(id=s.id, name=s.name, url=s.url, kind=s.kind) for s in SOURCES],
            topics=[BriefTopicResponse(id=key, label=value[0]) for key, value in TOPICS.items()],
            preferences=self._preferences.model_copy(deep=True),
        )

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
            self._previews.clear()
            self._dismissed.clear()
            self._edition = BriefEditionResponse()

    def _day(self) -> str:
        return utcnow().astimezone(ZoneInfo(self._storage.settings.user_timezone)).date().isoformat()

    def _snapshot(self) -> BriefEditionResponse:
        snapshot = self._edition.model_copy(deep=True)
        snapshot.temporary_bytes = sum(len(value[0]) for value in self._previews.values())
        for item in snapshot.items:
            self._saved(item)
        return snapshot

    async def edition(self) -> BriefEditionResponse:
        async with self._lock:
            self._expire()
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
            )
            return self._snapshot()

    async def clear(self) -> BriefEditionResponse:
        async with self._lock:
            self._previews.clear()
            self._dismissed.clear()
            self._edition = BriefEditionResponse(day=self._day(), cleared=True)
            return self._snapshot()

    async def close(self) -> None:
        await self.clear()

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
            return self._snapshot()

    def _saved(self, item: BriefItemResponse) -> None:
        if item.kind == "paper":
            document = self._downloads.find_document(f"https://arxiv.org/abs/{item.arxiv_id}")
            if document is None and item.saved_document_id:
                document = self._documents.get_document(item.saved_document_id)
            item.saved_document_id = document.id if document else None
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
