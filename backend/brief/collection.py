from __future__ import annotations

import html
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

from backend.brief.catalog import BriefSource
from backend.brief.schemas import BriefItemResponse
from backend.research.sources import _arxiv_key

ATOM = {"a": "http://www.w3.org/2005/Atom"}
ARXIV = "{http://arxiv.org/schemas/atom}"
DC = "{http://purl.org/dc/elements/1.1/}"


def clean_text(value: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", value)).split())


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        date = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
        except (ValueError, TypeError):
            return None
    return date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date.astimezone(timezone.utc)


def canonical_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/"), "", ""))


def parse_feed(content: bytes, source: BriefSource) -> list[BriefItemResponse]:
    if source.format == "github-release":
        release = json.loads(content)
        if not isinstance(release, dict) or not isinstance(release.get("html_url"), str):
            raise ValueError("GitHub returned an invalid release.")
        if release.get("draft") or release.get("prerelease"):
            return []
        return [_item(
            source, release.get("name") or release.get("tag_name") or "Release",
            release["html_url"], release.get("body") or "", parse_date(release.get("published_at")),
        )] if _allowed_link(release["html_url"], source) else []
    root = ET.fromstring(content)
    items: list[BriefItemResponse] = []
    if root.tag == f"{{{ATOM['a']}}}feed":
        for entry in root.findall("a:entry", ATOM):
            title = entry.findtext("a:title", "", ATOM)
            if source.kind == "engineering" and re.search(r"(?:^proto-|(?:rc|alpha|beta)\d|^viable/)", title, re.I):
                continue
            link = next(
                (node.get("href", "") for node in entry.findall("a:link", ATOM)
                 if node.get("rel", "alternate") == "alternate"), ""
            )
            items.append(_item(
                source, title, link,
                entry.findtext("a:summary", "", ATOM),
                parse_date(entry.findtext("a:published", None, ATOM) or entry.findtext("a:updated", None, ATOM)),
            ))
    elif root.tag == "rss":
        for entry in root.findall("channel/item"):
            if source.id == "openai":
                categories = {category.text for category in entry.findall("category")}
                if not categories.intersection({"Research", "Publication", "Safety"}):
                    continue
            description = clean_text(entry.findtext("description", ""))
            item = _item(
                source, entry.findtext("title", ""), entry.findtext("link", ""),
                description, parse_date(entry.findtext("pubDate")),
            )
            if source.kind == "paper":
                item.arxiv_id = _arxiv_key(item.url)
                if not item.arxiv_id:
                    continue
                item.announcement = entry.findtext(f"{ARXIV}announce_type") or "new"
                item.authors = [
                    author.strip() for author in clean_text(entry.findtext(f"{DC}creator", "")).split(",")
                    if author.strip()
                ]
                item.abstract = description.partition("Abstract:")[2].strip() or description
                version = re.search(r"arXiv:(\d{4}\.\d{4,5}(?:v\d+)?|[a-z-]+/\d{7}(?:v\d+)?)", description)
                if version:
                    item.arxiv_id = version.group(1)
            items.append(item)
    else:
        raise ValueError("The source did not return an RSS or Atom feed.")
    return [item for item in items if item.title and _allowed_link(item.url, source)]


def _allowed_link(url: str, source: BriefSource) -> bool:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    expected = (urlsplit(source.url).hostname or "").lower()
    return (
        parsed.scheme == "https"
        and not parsed.username and not parsed.password
        and parsed.port in {None, 443}
        and (
            host == expected or (source.kind == "paper" and host == "arxiv.org")
            or (source.format == "github-release" and host == "github.com"
                and parsed.path.startswith(
                    urlsplit(source.url).path.removeprefix("/repos").removesuffix("/latest") + "/"
                ))
        )
    )


def _item(
    source: BriefSource, title: str, url: str, abstract: str, published: datetime | None
) -> BriefItemResponse:
    return BriefItemResponse(
        id="", title=clean_text(title)[:300], url=urljoin(source.url, url),
        source_id=source.id, source_name=source.name, kind=source.kind,
        published_at=published, abstract=clean_text(abstract)[:12000],
    )


class _HeadlineParser(HTMLParser):
    def __init__(self, source: BriefSource) -> None:
        super().__init__(convert_charrefs=True)
        self.source = source
        self.items: list[BriefItemResponse] = []
        self.link: str | None = None
        self.parts: list[str] = []
        self.date: str | None = None
        self.skipped = 0
        self.heading: str | None = None
        self.heading_parts: list[str] = []
        self.date_parts: list[str] = []
        self.in_time = False
        self.article_date: datetime | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "article":
            self.article_date = None
        if tag == "span" and self.source.id == "qwen" and values.get("title"):
            self.article_date = parse_date(values["title"][:10])
        if tag in {"script", "style", "footer"} or (tag == "nav" and self.source.id != "deepseek"):
            self.skipped += 1
        if tag == "a" and not self.skipped:
            link = urljoin(self.source.url, values.get("href") or "")
            path = urlsplit(link).path.rstrip("/")
            if (
                _allowed_link(link, self.source) and path.startswith(self.source.article_prefix)
                and path != self.source.article_prefix.rstrip("/")
                and not (self.source.id == "anthropic" and path.startswith("/research/team/"))
            ):
                self.link, self.parts, self.date = link, [], None
                self.heading, self.heading_parts, self.date_parts = None, [], []
                label = values.get("aria-label") or ""
                if self.source.id == "qwen" and label.startswith("post link to "):
                    self.parts.append(label.removeprefix("post link to "))
                    self.date = self.article_date.isoformat() if self.article_date else None
        if tag in {"h2", "h3", "h4"} and self.link:
            self.heading = tag
        if tag == "time" and self.link:
            self.date = values.get("datetime")
            self.in_time = True
        if tag in {"div", "span", "p", "h2", "h3", "h4", "br", "time"} and self.link:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self.link and not self.skipped:
            self.parts.append(data)
            if self.heading:
                self.heading_parts.append(data)
            if self.in_time:
                self.date_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == self.heading:
            self.heading = None
        if tag == "time":
            self.in_time = False
        if tag == "a" and self.link:
            full_text = clean_text("".join(self.parts))
            text = clean_text("".join(self.heading_parts)) or full_text
            if len(text) >= 12 and text.lower() not in {"read more", "learn more", "view research"}:
                date = parse_date(self.date)
                if date is None:
                    match = re.search(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d{1,2},? \d{4}\b", full_text)
                    if match:
                        for pattern in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
                            try:
                                date = datetime.strptime(match.group(), pattern).replace(tzinfo=timezone.utc)
                                text = text.replace(match.group(), "").strip()
                                break
                            except ValueError:
                                continue
                if date is None:
                    numeric_date = re.search(r"\b\d{4}/\d{2}/\d{2}\b", full_text)
                    if numeric_date:
                        date = parse_date(numeric_date.group().replace("/", "-"))
                        text = text.replace(numeric_date.group(), "").strip()
                if not (self.source.id == "mistral" and full_text.startswith(("Company ", "Partners ", "Customer "))):
                    self.items.append(_item(self.source, text, self.link, "", date))
            self.link = None
        if (tag in {"script", "style", "footer"} or (tag == "nav" and self.source.id != "deepseek")) and self.skipped:
            self.skipped -= 1


def parse_headlines(content: bytes, source: BriefSource) -> list[BriefItemResponse]:
    parser = _HeadlineParser(source)
    parser.feed(content.decode("utf-8", errors="replace"))
    unique: dict[str, BriefItemResponse] = {}
    for item in parser.items:
        key = canonical_url(item.url)
        prior = unique.get(key)
        if prior is None or (prior.published_at is None and item.published_at is not None):
            unique[key] = item
    if not unique:
        raise ValueError("No article headlines were found; this source's page format may have changed.")
    # Undated index entries are leads, never represented as newly published articles.
    return list(unique.values())[:30]
