from __future__ import annotations

import asyncio
import html
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from html.parser import HTMLParser
from typing import Any

import httpx

from backend.core.config import Settings

_ATOM = {"atom": "http://www.w3.org/2005/Atom"}
_WHITESPACE = re.compile(r"\s+")
_HTML_TAG = re.compile(r"<[^>]+>")
_MAX_RESULTS = 10
_WEB_SEARCH_REQUESTS_PER_MINUTE = 30
_DUCKDUCKGO_SEARCH_URL = "https://duckduckgo.com/"
_DUCKDUCKGO_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:142.0) "
    "Gecko/20100101 Firefox/142.0"
)


class _DuckDuckGoPreloadParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.url: str | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag != "link":
            return
        attributes = dict(attrs)
        if attributes.get("id") == "deep_preload_link":
            self.url = attributes.get("href")


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
            "web": AsyncRateLimiter(_WEB_SEARCH_REQUESTS_PER_MINUTE),
            "arxiv": AsyncRateLimiter(settings.arxiv_search_requests_per_minute),
            "wikipedia": AsyncRateLimiter(settings.wikipedia_search_requests_per_minute),
        }

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def search_web(self, query: str) -> dict[str, Any]:
        query, _ = self._validated_request(query, _MAX_RESULTS)
        await self._limiters["web"].acquire()
        headers = {
            "User-Agent": _DUCKDUCKGO_USER_AGENT,
            "Accept": "*/*",
            "Referer": _DUCKDUCKGO_SEARCH_URL,
            "Sec-Fetch-Dest": "script",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Site": "same-site",
        }
        landing = await self._send_get(
            "DuckDuckGo",
            _DUCKDUCKGO_SEARCH_URL,
            params={"q": query, "t": "h_", "ia": "web"},
            headers=headers,
        )
        parser = _DuckDuckGoPreloadParser()
        parser.feed(landing.text)
        data_url = _validated_duckduckgo_data_url(parser.url)
        response = await self._send_get(
            "DuckDuckGo",
            data_url.replace("/d.js?", "/d.js?o=json&", 1),
            params=None,
            headers=headers,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError("DuckDuckGo search returned invalid JSON.") from exc
        raw_results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(raw_results, list):
            raise ValueError("DuckDuckGo search returned an invalid results payload.")

        results = [
            {
                "title": _clean_text(item.get("t")),
                "url": str(item["u"]),
                "snippet": _clean_text(item.get("a")),
                "engine": "duckduckgo",
                "published_at": None,
                "image_url": None,
            }
            for item in raw_results
            if isinstance(item, dict) and item.get("u")
        ][:_MAX_RESULTS]
        return {"query": query, "provider": "duckduckgo", "results": results}

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

    async def _get(
        self,
        provider: str,
        url: str,
        *,
        params: dict[str, Any],
    ) -> httpx.Response:
        await self._limiters[provider].acquire()
        return await self._send_get(provider, url, params=params)

    async def _send_get(
        self,
        provider: str,
        url: str,
        *,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        try:
            response = await self._client.get(url, params=params, headers=headers)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise self._request_error(provider, exc) from exc
        return response

    @staticmethod
    def _request_error(provider: str, exc: httpx.HTTPError) -> RuntimeError:
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


def _validated_duckduckgo_data_url(value: str | None) -> str:
    if not value:
        raise RuntimeError(
            "DuckDuckGo did not return search data; the request may have been rate-limited."
        )
    url = httpx.URL(html.unescape(value))
    if url.scheme != "https" or url.host != "links.duckduckgo.com" or url.path != "/d.js":
        raise RuntimeError("DuckDuckGo returned an unexpected search data URL.")
    return str(url)
