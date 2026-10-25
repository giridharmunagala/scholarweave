from __future__ import annotations

import hashlib
import re
import uuid
import unicodedata
from dataclasses import dataclass

from backend.core.errors import ValidationError

_ID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_QUOTE = re.compile(
    rf"^(?:<!-- scholarweave-highlight:(?P<id>{_ID}) -->\r?\n)?"
    r"(?P<body>>[^\n]*(?:\r?\n(?!#|<!-- scholarweave-highlight:)[^\n]*)*?)"
    r"\r?\n>\r?\n> ?(?:\u2014|-) p\. (?P<page>[1-9][0-9]*)[ \t]*(?:\r?\n|$)", re.MULTILINE,
)
_CONNECTION = re.compile(rf"^<!-- scholarweave-highlight-link:({_ID}):({_ID}) -->$", re.MULTILINE)


@dataclass(frozen=True)
class Highlight:
    id: str
    text: str
    page: int


def highlights(path: str, content: str) -> list[Highlight]:
    """Read explicit reader captures and the older page-attributed quote format."""
    result = []
    occurrences: dict[str, int] = {}
    for match in _QUOTE.finditer(content):
        lines = match["body"].splitlines()
        body = "\n".join(line.removeprefix(">").removeprefix(" ") for line in lines).strip()
        if not body:
            continue
        fingerprint = hashlib.sha256(f"{body}\npage:{match['page']}".encode()).hexdigest()
        occurrence = occurrences.get(fingerprint, 0)
        occurrences[fingerprint] = occurrence + 1
        identifier = match["id"] or str(uuid.uuid5(uuid.NAMESPACE_URL, f"{path}:{fingerprint}:{occurrence}"))
        if any(item.id == identifier for item in result):
            raise ValidationError(f"Duplicate reader highlight identifier in {path}. Restore unique highlight markers.")
        result.append(Highlight(id=identifier, text=body, page=int(match["page"])))
    return result


def connections(content: str) -> list[tuple[str, str]]:
    return _CONNECTION.findall(content.replace("\r\n", "\n"))


def search_words(text: str) -> set[str]:
    normalized = "".join(char for char in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(char))
    return set(re.findall(r"[^\W_]+", normalized))
