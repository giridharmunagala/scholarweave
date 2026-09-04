from __future__ import annotations

import asyncio
import html
import math
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import httpx
from ddgs import DDGS
from ddgs.engines.duckduckgo import Duckduckgo
from ddgs.exceptions import DDGSException, RatelimitException

from backend.core.config import Settings

_ATOM = {"atom": "http://www.w3.org/2005/Atom"}
_WHITESPACE = re.compile(r"\s+")
_HTML_TAG = re.compile(r"<[^>]+>")
_MAX_RESULTS = 10
_DUCKDUCKGO_REQUESTS_PER_MINUTE = 60
_DUCKDUCKGO_USER_AGENT = "Mozilla/5.0"
_RATE_LIMIT_MARKERS = (
    "http 202",
    "http 429",
    "rate limit",
    "ratelimit",
    "status code 202",
    "status code 429",
    "too many requests",
)


class DuckDuckGoClient(Protocol):
    def text(self, query: str, **kwargs: Any) -> list[dict[str, Any]]: ...


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
        duckduckgo_client: DuckDuckGoClient | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=settings.search_request_timeout_seconds,
            headers={"User-Agent": settings.search_user_agent},
            follow_redirects=True,
        )
        self._limiters = {
            "arxiv": AsyncRateLimiter(
                settings.arxiv_search_requests_per_minute,
                clock=clock,
                sleep=sleep,
            ),
            "wikipedia": AsyncRateLimiter(
                settings.wikipedia_search_requests_per_minute,
                clock=clock,
                sleep=sleep,
            ),
        }
        if duckduckgo_client is None:
            # ddgs otherwise chooses a random fake User-Agent; some generated mobile
            # agents consistently receive empty DuckDuckGo HTML responses.
            Duckduckgo.headers = {"User-Agent": _DUCKDUCKGO_USER_AGENT}
            self._duckduckgo = DDGS(
                timeout=max(1, math.ceil(settings.search_request_timeout_seconds))
            )
        else:
            self._duckduckgo = duckduckgo_client
        self._duckduckgo_limiter = AsyncRateLimiter(
            _DUCKDUCKGO_REQUESTS_PER_MINUTE,
            clock=clock,
            sleep=sleep,
        )
        self._duckduckgo_lock = asyncio.Lock()
        self._duckduckgo_thread_lock = threading.Lock()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def search_web(self, query: str, limit: int = _MAX_RESULTS) -> dict[str, Any]:
        query, limit = self._validated_request(query, limit)
        warning: str | None = None
        async with self._duckduckgo_lock:
            await self._duckduckgo_limiter.acquire()
            try:
                raw_results = await asyncio.to_thread(
                    self._search_duckduckgo,
                    query,
                    limit,
                )
            except DDGSException as exc:
                if _is_duckduckgo_rate_limit(exc):
                    raise RuntimeError(
                        f"DuckDuckGo search was rate-limited ({exc}). Automatic retries are disabled "
                        "to conserve the session request budget; use evidence already gathered "
                        "or try again in a later session."
                    ) from exc
                if _is_duckduckgo_no_results(exc):
                    raw_results = []
                    warning = "DuckDuckGo reported no results for this query."
                else:
                    raise RuntimeError(f"DuckDuckGo search request failed: {exc}") from exc

        if not isinstance(raw_results, list):
            raise ValueError("DuckDuckGo returned an invalid results payload.")
        results = []
        for item in raw_results:
            if not isinstance(item, dict) or not item.get("href"):
                continue
            results.append(
                {
                    "title": _clean_text(item.get("title")),
                    "url": str(item["href"]),
                    "snippet": _clean_text(item.get("body")),
                    "engine": "duckduckgo",
                    "published_at": None,
                    "image_url": None,
                }
            )
            if len(results) == limit:
                break
        return {
            "query": query,
            "provider": "duckduckgo",
            "results": results,
            **({"warning": warning} if warning else {}),
        }

    def _search_duckduckgo(self, query: str, limit: int) -> list[dict[str, Any]]:
        with self._duckduckgo_thread_lock:
            return self._duckduckgo.text(
                query,
                max_results=limit,
                backend="duckduckgo",
                region="wt-wt",
            )

    async def search_arxiv(self, query: str, limit: int = _MAX_RESULTS) -> dict[str, Any]:
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
                link.attrib.get("title") or link.attrib.get("rel", ""): link.attrib.get(
                    "href", ""
                )
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

    async def search_wikipedia(
        self,
        query: str,
        limit: int = _MAX_RESULTS,
    ) -> dict[str, Any]:
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
            raise RuntimeError(f"{provider} search request failed: {exc}") from exc
        return response

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


def _is_duckduckgo_no_results(exc: BaseException) -> bool:
    message = _WHITESPACE.sub(" ", str(exc)).strip().rstrip(".").casefold()
    return message == "no results found"


def _is_duckduckgo_rate_limit(exc: BaseException) -> bool:
    pending = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, RatelimitException):
            return True
        message = str(current).casefold()
        if any(marker in message for marker in _RATE_LIMIT_MARKERS):
            return True
        pending.extend(arg for arg in current.args if isinstance(arg, BaseException))
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return False
