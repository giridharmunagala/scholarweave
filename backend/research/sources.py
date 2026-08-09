from __future__ import annotations

import ipaddress
import re
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import anyio
import httpx

from backend.core.config import Settings
from backend.core.time import utcnow
from backend.documents import DocumentService
from backend.documents.models import Document
from backend.workspace.service import WorkspaceDocument, WorkspaceService

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 5
_WHITESPACE = re.compile(r"[ \t\f\v]+")
_BLANK_LINES = re.compile(r"\n{3,}")
_ARXIV_HOSTS = {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}


@dataclass(frozen=True, slots=True)
class WebSource:
    id: str
    url: str
    title: str
    chunks: tuple[str, ...]
    created_at: datetime
    expires_at: datetime

    @property
    def text(self) -> str:
        return "\n\n".join(self.chunks)


class _ReadableHTMLParser(HTMLParser):
    _SKIPPED = {"script", "style", "noscript", "svg", "canvas", "template"}
    _BREAKS = {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self._title_parts: list[str] = []
        self._parts: list[str] = []

    @property
    def title(self) -> str:
        return _normalize_inline(" ".join(self._title_parts))

    @property
    def text(self) -> str:
        raw = "".join(self._parts).replace("\r", "\n")
        lines = [_normalize_inline(line) for line in raw.splitlines()]
        return _BLANK_LINES.sub("\n\n", "\n".join(line for line in lines if line)).strip()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        tag = tag.casefold()
        if tag in self._SKIPPED:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if not self._skip_depth and tag in self._BREAKS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "title":
            self._in_title = False
        if tag in self._SKIPPED and self._skip_depth:
            self._skip_depth -= 1
        if not self._skip_depth and tag in self._BREAKS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        if not self._skip_depth and not self._in_title:
            self._parts.append(data)


class SourceDownloadService:
    def __init__(
        self,
        settings: Settings,
        documents: DocumentService,
        workspace: WorkspaceService,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._documents = documents
        self._workspace = workspace
        self._owns_client = client is None
        self._require_peer_validation = client is None
        self._client = client or httpx.AsyncClient(
            timeout=settings.search_request_timeout_seconds,
            headers={"User-Agent": settings.search_user_agent},
            follow_redirects=False,
            trust_env=False,
        )
        self._web_sources: dict[str, WebSource] = {}
        self._lock = anyio.Lock()
        self._pdf_download_lock = anyio.Lock()

    async def close(self) -> None:
        async with self._lock:
            self._web_sources.clear()
        if self._owns_client:
            await self._client.aclose()

    async def download_pdf(
        self,
        url: str,
        *,
        title: str | None = None,
        arxiv_only: bool = False,
    ) -> Document:
        normalized_url = self._normalize_url(url)
        if arxiv_only and (urlparse(normalized_url).hostname or "").casefold() not in _ARXIV_HOSTS:
            raise ValueError("arXiv downloads must use an arxiv.org PDF URL.")
        async with self._pdf_download_lock:
            return await self._download_pdf(
                normalized_url,
                title=title,
                arxiv_only=arxiv_only,
            )

    async def _download_pdf(
        self,
        normalized_url: str,
        *,
        title: str | None,
        arxiv_only: bool,
    ) -> Document:
        existing = self._find_document_by_source(normalized_url)
        if existing is not None and existing.status == "ready":
            return existing
        content, final_url, headers = await self._download(
            normalized_url,
            self._settings.max_upload_bytes,
        )
        if arxiv_only and (urlparse(final_url).hostname or "").casefold() not in _ARXIV_HOSTS:
            raise ValueError("arXiv PDF download redirected outside arxiv.org.")
        media_type = headers.get("content-type", "").partition(";")[0].strip().casefold()
        if not content.startswith(b"%PDF-"):
            raise ValueError(
                f"The downloaded resource is not a PDF (content type: {media_type or 'unknown'})."
            )
        filename = self._pdf_filename(final_url)
        document = self._documents.create_document_from_bytes(
            content,
            filename=filename,
            title=(title or Path(filename).stem).strip() or "Downloaded paper",
            metadata={
                "source_url": normalized_url,
                "resolved_source_url": final_url,
                "downloaded_at": utcnow().isoformat(),
                "source_kind": "arxiv" if arxiv_only else "remote_pdf",
            },
        )
        options = await self._documents.ingestion_options(document.id)
        use_ocr = bool(
            options["recommended_mode"] == "ocr" and options.get("ocr_available")
        )
        await self._documents.ingest_document(document.id, force_ocr=use_ocr)
        ready = self._documents.get_document(document.id)
        assert ready is not None
        return ready

    async def download_web_page(self, url: str) -> WebSource:
        normalized_url = self._normalize_url(url)
        content, final_url, headers = await self._download(
            normalized_url,
            self._settings.max_web_source_bytes,
        )
        media_type = headers.get("content-type", "").partition(";")[0].strip().casefold()
        if media_type not in {"text/html", "application/xhtml+xml"}:
            raise ValueError(
                f"The downloaded resource is not an HTML page (content type: {media_type or 'unknown'})."
            )
        encoding = _charset(headers.get("content-type", "")) or "utf-8"
        try:
            html = content.decode(encoding, errors="replace")
        except LookupError as exc:
            raise ValueError(f"HTML page uses an unsupported character encoding: {encoding}.") from exc
        parser = _ReadableHTMLParser()
        parser.feed(html)
        text = parser.text
        if not text:
            raise ValueError("The HTML page did not contain readable text.")
        now = utcnow()
        source = WebSource(
            id=str(uuid.uuid4()),
            url=final_url,
            title=parser.title or urlparse(final_url).hostname or "Web page",
            chunks=tuple(self._chunk_text(text)),
            created_at=now,
            expires_at=now + timedelta(minutes=self._settings.web_source_ttl_minutes),
        )
        async with self._lock:
            self._purge_expired()
            if len(self._web_sources) >= self._settings.max_temporary_web_sources:
                oldest = min(self._web_sources.values(), key=lambda item: item.created_at)
                self._web_sources.pop(oldest.id, None)
            self._web_sources[source.id] = source
        return source

    async def list_web_sources(self) -> list[WebSource]:
        async with self._lock:
            self._purge_expired()
            return sorted(
                self._web_sources.values(),
                key=lambda item: item.created_at,
                reverse=True,
            )

    async def get_web_source(self, source_id: str) -> WebSource:
        async with self._lock:
            self._purge_expired()
            source = self._web_sources.get(source_id)
        if source is None:
            raise ValueError("Temporary web page was not found or has expired.")
        return source

    async def delete_web_source(self, source_id: str) -> bool:
        async with self._lock:
            self._purge_expired()
            return self._web_sources.pop(source_id, None) is not None

    async def search_web_source(
        self,
        source_id: str,
        query: str,
        *,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        source = await self.get_web_source(source_id)
        tokens = [token.casefold() for token in query.split() if token.strip()]
        if not tokens:
            raise ValueError("Web page search query cannot be empty.")
        scored: list[tuple[int, int, str]] = []
        for index, chunk in enumerate(source.chunks):
            lowered = chunk.casefold()
            score = sum(token in lowered for token in tokens) * 10
            score += sum(lowered.count(token) for token in tokens)
            if score:
                scored.append((score, index, chunk))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            {
                "source_id": source.id,
                "chunk_index": index,
                "citation": source.url,
                "title": source.title,
                "text": chunk,
                "score": score,
            }
            for score, index, chunk in scored[:top_k]
        ]

    async def save_web_note(
        self,
        source_id: str,
        *,
        name: str,
        content: str,
        tags: list[str] | None = None,
    ) -> WorkspaceDocument:
        source = await self.get_web_source(source_id)
        note_content = (
            f"Source: [{source.title}]({source.url})\n\n"
            f"{content.strip()}"
        ).rstrip()
        return self._workspace.create_note(
            name=name,
            content=note_content,
            tags=["web-source", *(tags or [])],
        )

    def _find_document_by_source(self, url: str) -> Document | None:
        for document in self._documents.list_documents():
            metadata = document.metadata_json if isinstance(document.metadata_json, dict) else {}
            if metadata.get("source_url") == url:
                return document
        return None

    def _chunk_text(self, text: str) -> list[str]:
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
        chunks: list[str] = []
        current: list[str] = []
        for paragraph in paragraphs:
            if current and len("\n\n".join([*current, paragraph])) > self._settings.max_chunk_chars:
                chunks.append("\n\n".join(current))
                current = []
            if len(paragraph) <= self._settings.max_chunk_chars:
                current.append(paragraph)
                continue
            if current:
                chunks.append("\n\n".join(current))
                current = []
            for start in range(0, len(paragraph), self._settings.max_chunk_chars):
                chunks.append(paragraph[start : start + self._settings.max_chunk_chars])
        if current:
            chunks.append("\n\n".join(current))
        return chunks

    async def _download(
        self,
        url: str,
        max_bytes: int,
    ) -> tuple[bytes, str, httpx.Headers]:
        current = url
        for _ in range(_MAX_REDIRECTS + 1):
            await self._validate_public_url(current)
            try:
                async with self._client.stream("GET", current) as response:
                    self._validate_connected_peer(response)
                    if response.status_code in _REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            raise ValueError("Remote server returned a redirect without a location.")
                        current = self._normalize_url(urljoin(current, location))
                        continue
                    response.raise_for_status()
                    declared = response.headers.get("content-length")
                    if declared:
                        try:
                            declared_size = int(declared)
                        except ValueError as exc:
                            raise ValueError("Remote server returned an invalid content length.") from exc
                        if declared_size > max_bytes:
                            raise ValueError("Remote resource exceeds the maximum allowed size.")
                    parts: list[bytes] = []
                    size = 0
                    async for part in response.aiter_bytes():
                        size += len(part)
                        if size > max_bytes:
                            raise ValueError("Remote resource exceeds the maximum allowed size.")
                        parts.append(part)
                    return b"".join(parts), str(response.url), response.headers
            except httpx.HTTPError as exc:
                raise RuntimeError(f"Remote download failed: {exc}") from exc
        raise ValueError("Remote resource redirected too many times.")

    async def _validate_public_url(self, url: str) -> None:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if parsed.scheme not in {"http", "https"} or not hostname:
            raise ValueError("Source URL must use HTTP or HTTPS.")
        if parsed.username or parsed.password:
            raise ValueError("Source URLs cannot contain credentials.")
        try:
            addresses = await anyio.to_thread.run_sync(
                lambda: socket.getaddrinfo(
                    hostname,
                    parsed.port or (443 if parsed.scheme == "https" else 80),
                    type=socket.SOCK_STREAM,
                )
            )
        except socket.gaierror as exc:
            raise ValueError("Source hostname could not be resolved.") from exc
        if not addresses:
            raise ValueError("Source hostname did not resolve to an address.")
        for address in addresses:
            if not _is_public_address(address[4][0]):
                raise ValueError("Source URL cannot target a private or local network address.")

    def _validate_connected_peer(self, response: httpx.Response) -> None:
        if not self._require_peer_validation:
            return
        stream = response.extensions.get("network_stream")
        server_addr = stream.get_extra_info("server_addr") if stream is not None else None
        if not server_addr or not _is_public_address(server_addr[0]):
            raise ValueError("Remote connection resolved to a private or local network address.")

    @staticmethod
    def _normalize_url(url: str) -> str:
        normalized = url.strip()
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Source URL must use HTTP or HTTPS.")
        return normalized

    @staticmethod
    def _pdf_filename(url: str) -> str:
        name = unquote(Path(urlparse(url).path).name) or "paper.pdf"
        if not name.casefold().endswith(".pdf"):
            name = f"{name}.pdf"
        return name

    def _purge_expired(self) -> None:
        now = utcnow()
        expired = [
            source_id
            for source_id, source in self._web_sources.items()
            if source.expires_at <= now
        ]
        for source_id in expired:
            self._web_sources.pop(source_id, None)


def _normalize_inline(value: str) -> str:
    return _WHITESPACE.sub(" ", value).strip()


def _charset(content_type: str) -> str | None:
    for part in content_type.split(";")[1:]:
        key, separator, value = part.strip().partition("=")
        if separator and key.casefold() == "charset":
            return value.strip("\"' ")
    return None


def _is_public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_global
