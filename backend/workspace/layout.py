from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath


class WorkspaceLayout:
    paper_root = "library/papers"
    knowledge_root = "knowledge"

    @staticmethod
    def named_component(title: str, identifier: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", identifier):
            raise ValueError("Invalid workspace identifier.")
        normalized = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
        slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")[:48].rstrip("-")
        return f"{slug or 'untitled'}--{identifier}"

    @classmethod
    def paper_folder(cls, document_id: str, title: str) -> str:
        return f"{cls.paper_root}/{cls.named_component(title, document_id)}"

    @classmethod
    def knowledge_note(cls, note_id: str, title: str) -> str:
        return f"{cls.knowledge_root}/{cls.named_component(title, note_id)}.md"

    @classmethod
    def paper_id(cls, path: str) -> str | None:
        parts = PurePosixPath(path).parts
        if len(parts) < 4 or parts[:2] != ("library", "papers"):
            return None
        _, separator, identifier = parts[2].partition("--")
        if not separator or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", identifier):
            return None
        return identifier

    @classmethod
    def kind(cls, path: str) -> str:
        parts = PurePosixPath(path).parts
        if cls.paper_id(path) is not None:
            if len(parts) == 4 and parts[-1] == "summary.md":
                return "paper_summary"
            if len(parts) == 4 and parts[-1] == "notes.md":
                return "paper_notes"
            return "paper_file"
        if parts and parts[0] == cls.knowledge_root and PurePosixPath(path).suffix.lower() == ".md":
            return "note"
        return "file"