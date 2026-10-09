from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath


class WorkspaceLayout:
    paper_root = "library/papers"
    knowledge_root = "knowledge"
    track_root = "projects/tracks"

    @classmethod
    def track_folder(cls, track_id: str, title: str) -> str:
        return f"{cls.track_root}/{cls.named_component(title, track_id)}"

    @classmethod
    def is_track_path(cls, path: str) -> bool:
        normalized = path.replace("\\", "/").casefold().strip("/")
        return normalized == cls.track_root or normalized.startswith(f"{cls.track_root}/")

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

    @staticmethod
    def chat_upload(filename: str, identifier: str) -> str:
        return f"inbox/attachments/{identifier}/{filename}"

    @staticmethod
    def chat_pdf_text(paper_folder: str) -> str:
        return f"{paper_folder}/attachments/source.md"

    @classmethod
    def is_chat_pdf_text(cls, path: str) -> bool:
        parts = PurePosixPath(path).parts
        return (
            cls.paper_id(path) is not None
            and len(parts) == 5
            and parts[-2] == "attachments"
            and PurePosixPath(path).suffix.lower() == ".md"
        )

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
        if cls.is_track_path(path) and (
            PurePosixPath(path).name in {
                "problem.md", "literature.md", "experiments.md", "results.md",
            } or ("/exploration/" in path and PurePosixPath(path).suffix.lower() == ".md")
        ):
            return "track_section"
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