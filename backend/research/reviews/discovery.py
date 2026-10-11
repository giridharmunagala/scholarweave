from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx

from backend.core.config import Settings
from backend.core.errors import ValidationError
from backend.research.reviews.repository import ReviewRepository
from backend.research.reviews.schemas import Candidate
from backend.research.search import AsyncRateLimiter, ResearchSearchService, rate_limit_delay
from backend.research.sources import _arxiv_key
from backend.utils import utcnow


def normalized_doi(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", unquote(value.strip()), flags=re.I).casefold()
    return cleaned if re.fullmatch(r"10\.\d{4,9}/\S+", cleaned) else None


def source_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value.strip())
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            return None
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))
    except ValueError:
        return None


def candidate_key(item: Candidate) -> str:
    if item.doi:
        return f"doi:{item.doi}"
    if item.arxiv_id:
        return f"arxiv:{re.sub(r'v[0-9]+$', '', item.arxiv_id)}"
    if item.openalex_id:
        return f"openalex:{item.openalex_id}"
    if item.document_id:
        return f"local:{item.document_id}"
    if item.url:
        return f"url:{item.url}"
    raise ValidationError("A paper candidate needs an identifier or source URL.")


def normalize_candidate(review_id: str, raw: dict, provider: str, trail: str) -> Candidate:
    location = raw.get("best_oa_location") or raw.get("primary_location") or {}
    if not isinstance(location, dict):
        location = {}
    doi = normalized_doi(raw.get("doi"))
    url = source_url(raw.get("url")) or source_url(location.get("landing_page_url")) or source_url(raw.get("id"))
    arxiv = raw.get("arxiv_id")
    arxiv = _arxiv_key(arxiv) if isinstance(arxiv, str) else None
    arxiv = arxiv or (_arxiv_key(url) if url else None)
    if arxiv:
        arxiv = re.sub(r"v[0-9]+$", "", arxiv)
    raw_authors = raw.get("authors") or [
        (entry.get("author") or {}).get("display_name") for entry in raw.get("authorships") or []
        if isinstance(entry, dict)
    ]
    if isinstance(raw_authors, str):
        raw_authors = [raw_authors]
    authors = [author for author in raw_authors if isinstance(author, str)] if isinstance(raw_authors, list) else []
    year = raw.get("publication_year") or raw.get("year")
    if not isinstance(year, int) or isinstance(year, bool):
        date = str(raw.get("published_at") or "")
        year = int(date[:4]) if re.match(r"^\d{4}", date) else None
    abstract = raw.get("abstract") or raw.get("summary") or raw.get("snippet")
    inverted = raw.get("abstract_inverted_index")
    if not abstract and isinstance(inverted, dict):
        positions = [
            (position, word) for word, indices in inverted.items() if isinstance(indices, list)
            for position in indices if isinstance(position, int) and not isinstance(position, bool) and position >= 0
        ]
        abstract = " ".join(word for _, word in sorted(positions))
    identifier = raw.get("id") if provider == "openalex" else raw.get("openalex_id")
    openalex_id = None
    if isinstance(identifier, str) and re.fullmatch(r"(?:https://openalex.org/)?W\d+", identifier):
        openalex_id = identifier.rsplit("/", 1)[-1]
    pdf = source_url(raw.get("pdf_url")) or source_url(location.get("pdf_url"))
    if arxiv and not pdf:
        pdf = f"https://arxiv.org/pdf/{arxiv}"
    if not pdf and isinstance(url, str) and urlsplit(url).path.lower().endswith(".pdf"):
        pdf = url
    url = url or (f"https://doi.org/{doi}" if doi else f"https://arxiv.org/abs/{arxiv}" if arxiv else None)
    references = raw.get("referenced_works") or []
    candidate = Candidate(
        id="", title=str(raw.get("title") or raw.get("display_name") or url or "Untitled"),
        authors=authors, year=year, doi=doi, arxiv_id=arxiv, openalex_id=openalex_id,
        url=str(url) if url else None, pdf_url=pdf, abstract=str(abstract) if abstract else None,
        document_id=raw.get("document_id") if provider == "library" else None, source_kind=raw.get("type"),
        references=[ref for ref in references if isinstance(ref, str)] if isinstance(references, list) else [],
        discovery=[trail],
    )
    candidate.id = hashlib.sha256(f"{review_id}:{candidate_key(candidate)}".encode()).hexdigest()
    return candidate


class ScholarlyDiscovery:
    def __init__(
        self, settings: Settings, search: ResearchSearchService, repository: ReviewRepository,
        *, client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.search = search
        self.repository = repository
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=settings.search_request_timeout_seconds,
            headers={"User-Agent": settings.search_user_agent}, follow_redirects=False,
        )
        self._openalex_limiter = AsyncRateLimiter(30)

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def query(self, provider: str, query: str, page: int, *, cursor: str | None = None, limit: int = 100) -> dict:
        if not 1 <= limit <= 100:
            raise ValidationError("Discovery page size must be between 1 and 100.")
        request = {"provider": provider, "query": " ".join(query.split()), "page": page, "cursor": cursor}
        if limit != 100:
            request["limit"] = limit
        key = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        cached = self.repository.cached(key)
        if cached is not None:
            return {**cached, "cached": True}
        self._check_cooldown(provider)
        try:
            if provider == "arxiv":
                result = await self.search.search_arxiv_page(query, start=page * 100, limit=limit)
            elif provider == "web":
                if page:
                    return {"results": [], "next_start": None}
                result = await self.search.search_web(query, limit=min(10, limit))
            elif provider == "openalex":
                if page and not cursor:
                    raise ValidationError("An OpenAlex continuation needs its saved cursor.")
                result = await self._openalex("works", {"search": query, "cursor": cursor or "*", "per_page": limit})
            else:
                raise ValidationError("Unknown scholarly discovery provider.")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {429, 503}:
                self._set_cooldown(provider, exc.response.headers)
            raise
        except RuntimeError as exc:
            cause = exc.__cause__
            if isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code in {429, 503}:
                self._set_cooldown(provider, cause.response.headers)
            elif provider == "web":
                self._set_cooldown(provider, httpx.Headers())
            raise
        self.repository.cache(key, result)
        return result

    async def references(self, identifiers: list[str], *, limit: int = 500) -> list[dict]:
        if limit < 1:
            raise ValidationError("Reference discovery needs a positive remaining paper allowance.")
        keys = sorted({
            value.rsplit("/", 1)[-1] for value in identifiers
            if re.fullmatch(r"(?:https://openalex.org/)?W\d+", value)
        })
        results, missing = [], []
        for identifier in keys:
            cached = self.repository.cached(self._work_key(identifier))
            if cached is None:
                missing.append(identifier)
            elif cached.get("work"):
                results.append(cached["work"])
        start = 0
        while start < len(missing):
            if len(results) >= limit:
                break
            group = missing[start:start + min(100, limit - len(results))]
            start += len(group)
            response = await self._openalex("works", {"filter": "openalex:" + "|".join(group), "per_page": len(group)})
            results.extend(response["results"])
            found = {row.get("id", "").rsplit("/", 1)[-1] for row in response["results"]}
            for identifier in set(group) - found:
                self.repository.cache(self._work_key(identifier), {"work": None})
        return results[:limit]

    @staticmethod
    def _work_key(identifier: str) -> str:
        return hashlib.sha256(f"openalex-work:{identifier}".encode()).hexdigest()

    async def _openalex(self, path: str, params: dict) -> dict:
        self._check_cooldown("openalex")
        headers = (
            {"Authorization": f"Bearer {self.settings.openalex_api_key}"}
            if self.settings.openalex_api_key else {}
        )
        async with self._openalex_limiter.request():
            self._check_cooldown("openalex")
            response = await self.client.get(
                f"{self.settings.openalex_api_url.rstrip('/')}/{path}", params=params, headers=headers,
            )
            if response.status_code in {429, 503}:
                self._set_cooldown("openalex", response.headers)
                retry = response.headers.get("Retry-After") or response.headers.get("X-RateLimit-Reset")
                raise ValidationError(
                    "OpenAlex is rate-limited. Discovery is paused for this source; no automatic retry was sent."
                    + (f" Provider retry/reset delay: {retry} seconds." if retry else "")
                )
            response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise ValidationError("OpenAlex returned an invalid work list.")
        for row in payload["results"]:
            if not isinstance(row, dict):
                raise ValidationError("OpenAlex returned an invalid work record.")
            identifier = row.get("id")
            if isinstance(identifier, str) and re.fullmatch(r"https://openalex.org/W\d+", identifier):
                self.repository.cache(self._work_key(identifier.rsplit("/", 1)[-1]), {"work": row})
        return payload

    def _check_cooldown(self, provider: str) -> None:
        key = hashlib.sha256(f"cooldown:{provider}".encode()).hexdigest()
        saved = self.repository.cached(key, max_age_seconds=31_536_000)
        if saved and saved["until"] > utcnow().timestamp():
            raise ValidationError(
                f"{provider} is cooling down until {saved['display_until']}; no request was sent. Resume discovery later."
            )

    def _set_cooldown(self, provider: str, headers: httpx.Headers) -> None:
        now = utcnow()
        until = now + timedelta(seconds=rate_limit_delay(headers))
        key = hashlib.sha256(f"cooldown:{provider}".encode()).hexdigest()
        self.repository.cache(key, {"until": until.timestamp(), "display_until": until.isoformat()})
