from __future__ import annotations

import asyncio
import html
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from backend.core.config import Settings

_ATOM = {"atom": "http://www.w3.org/2005/Atom"}
_WHITESPACE = re.compile(r"\s+")
_HTML_TAG = re.compile(r"<[^>]+>")
_MAX_RESULTS = 10


class AsyncRateLimiter:
    def __init__(
        self,
        requests_per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if requests_per_minute < 1:
            raise ValueError("requests_per_minute must be at least 1.")
        self._interval = 60.0 / requests_per_minute
        self._clock = clock
        self._sleep = sleep
        self._next_request_at = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = self._clock()
            delay = max(0.0, self._next_request_at - now)
            if delay:
                await self._sleep(delay)
                now = self._clock()
            self._next_request_at = max(now, self._next_request_at) + self._interval


class ResearchSearchService:
    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=settings.search_request_timeout_seconds,
            headers={"User-Agent": settings.search_user_agent},
            follow_redirects=True,
        )
        self._limiters = {
            "web": AsyncRateLimiter(settings.web_search_requests_per_minute),
            "arxiv": AsyncRateLimiter(settings.arxiv_search_requests_per_minute),
            "wikipedia": AsyncRateLimiter(settings.wikipedia_search_requests_per_minute),
        }

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def search_web(self, query: str, limit: int) -> dict[str, Any]:
        query, limit = self._validated_request(query, limit)
        payload = await self._post_json(
            "web",
            f"{self._settings.searxng_base_url.rstrip('/')}/search",
            data={
                "q": query,
                "format": "json",
                "categories": "general",
                "language": "auto",
            },
        )
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            raise ValueError("SearXNG returned an invalid results payload.")
        results = []
        for item in raw_results:
            if not isinstance(item, dict) or not item.get("url"):
                continue
            results.append(
                {
                    "title": _clean_text(item.get("title")),
                    "url": str(item["url"]),
                    "snippet": _clean_text(item.get("content")),
                    "engine": str(item.get("engine") or ""),
                    "published_at": item.get("publishedDate"),
                }
            )
            if len(results) == limit:
                break
        return {"query": query, "provider": "searxng", "results": results}

    async def search_arxiv(self, query: str, limit: int) -> dict[str, Any]:
        query, limit = self._validated_request(query, limit)
        response = await self._get(
            "arxiv",
            self._settings.arxiv_api_url,
            params={
                "search_query": f"all:{query}",
                "start": 0,
                "max_results": limit,
                "sortBy": "relevance",
                "sortOrder": "descending",
            },
        )
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise ValueError("arXiv returned invalid Atom XML.") from exc

        results = []
        for entry in root.findall("atom:entry", _ATOM):
            entry_url = _element_text(entry, "atom:id")
            links = {
                link.attrib.get("title") or link.attrib.get("rel", ""): link.attrib.get("href", "")
                for link in entry.findall("atom:link", _ATOM)
            }
            results.append(
                {
                    "arxiv_id": entry_url.rstrip("/").rsplit("/", 1)[-1],
                    "title": _element_text(entry, "atom:title"),
                    "authors": [
                        _element_text(author, "atom:name")
                        for author in entry.findall("atom:author", _ATOM)
                    ],
                    "summary": _element_text(entry, "atom:summary"),
                    "published_at": _element_text(entry, "atom:published"),
                    "updated_at": _element_text(entry, "atom:updated"),
                    "url": links.get("alternate") or entry_url,
                    "pdf_url": links.get("pdf") or None,
                    "categories": [
                        category.attrib["term"]
                        for category in entry.findall("atom:category", _ATOM)
                        if category.attrib.get("term")
                    ],
                }
            )
        return {"query": query, "provider": "arxiv", "results": results[:limit]}

    async def search_wikipedia(self, query: str, limit: int) -> dict[str, Any]:
        query, limit = self._validated_request(query, limit)
        payload = await self._get_json(
            "wikipedia",
            self._settings.wikipedia_api_url,
            params={
                "action": "query",
                "generator": "search",
                "gsrsearch": query,
                "gsrlimit": limit,
                "prop": "extracts|info",
                "exintro": 1,
                "explaintext": 1,
                "exsentences": 5,
                "inprop": "url",
                "format": "json",
                "formatversion": 2,
            },
        )
        query_payload = payload.get("query")
        pages = query_payload.get("pages", []) if isinstance(query_payload, dict) else []
        if not isinstance(pages, list):
            raise ValueError("Wikipedia returned an invalid pages payload.")
        pages = sorted(
            (page for page in pages if isinstance(page, dict)),
            key=lambda page: int(page.get("index") or 0),
        )
        results = [
            {
                "page_id": page.get("pageid"),
                "title": _clean_text(page.get("title")),
                "url": str(page.get("fullurl") or ""),
                "extract": _clean_text(page.get("extract")),
            }
            for page in pages[:limit]
        ]
        return {"query": query, "provider": "wikipedia", "results": results}

    async def _get_json(
        self,
        provider: str,
        url: str,
        *,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self._get(provider, url, params=params)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"{provider} search returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"{provider} search returned an invalid JSON payload.")
        return payload

    async def _post_json(
        self,
        provider: str,
        url: str,
        *,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self._post(provider, url, data=data)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"{provider} search returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"{provider} search returned an invalid JSON payload.")
        return payload

    async def _get(
        self,
        provider: str,
        url: str,
        *,
        params: dict[str, Any],
    ) -> httpx.Response:
        await self._limiters[provider].acquire()
        try:
            response = await self._client.get(url, params=params)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise self._request_error(provider, exc) from exc
        return response

    async def _post(
        self,
        provider: str,
        url: str,
        *,
        data: dict[str, Any],
    ) -> httpx.Response:
        await self._limiters[provider].acquire()
        try:
            response = await self._client.post(url, data=data)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise self._request_error(provider, exc) from exc
        return response

    @staticmethod
    def _request_error(provider: str, exc: httpx.HTTPError) -> RuntimeError:
        if (
            provider == "web"
            and isinstance(exc, httpx.HTTPStatusError)
            and exc.response.status_code == 403
        ):
            return RuntimeError(
                "SearXNG rejected the JSON response request. Enable 'json' in its search.formats setting."
            )
        return RuntimeError(f"{provider} search request failed: {exc}")

    @staticmethod
    def _validated_request(query: str, limit: int) -> tuple[str, int]:
        normalized = _clean_text(query)
        if not normalized:
            raise ValueError("Search query cannot be empty.")
        if limit < 1 or limit > _MAX_RESULTS:
            raise ValueError(f"Search limit must be between 1 and {_MAX_RESULTS}.")
        return normalized, limit


def _clean_text(value: Any) -> str:
    text = html.unescape(_HTML_TAG.sub(" ", str(value or "")))
    return _WHITESPACE.sub(" ", text).strip()


def _element_text(element: ET.Element, path: str) -> str:
    child = element.find(path, _ATOM)
    return _clean_text(child.text if child is not None else "")
