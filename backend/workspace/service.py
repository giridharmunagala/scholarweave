from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.core.text import clean_filename
from backend.persistence.files import SafeStorage
from backend.workspace.models import WorkspaceEntry
from backend.workspace.repository import WorkspaceRepository

_TAG_INDEX_PATH = ".scholarweave/tags.json"
_PAPER_INDEX_PATH = ".scholarweave/papers.json"
_MAX_TAGS = 32
_MAX_TAG_LENGTH = 64
_MAX_PAPER_NAME_LENGTH = 300


@dataclass(frozen=True, slots=True)
class WorkspaceDocument:
    path: str
    name: str
    media_type: str
    size_bytes: int
    modified_at: datetime
    tags: tuple[str, ...] = ()
    paper_id: str | None = None
    paper_name: str | None = None
    note_id: str | None = None
    note_name: str | None = None
    kind: str = "file"
    content: Any | None = None
    sha256: str | None = None


class WorkspaceService:
    def __init__(
        self,
        storage: SafeStorage,
        repository: WorkspaceRepository | None = None,
    ) -> None:
        self._storage = storage
        if repository is None:
            from backend.persistence.database import create_session_factory

            repository = WorkspaceRepository(create_session_factory(storage.settings))
        self._repository = repository
        if self._repository.count() == 0:
            self._index_existing_files()

    def list_files(self, *, tags: list[str] | None = None) -> list[WorkspaceDocument]:
        requested = {tag.casefold() for tag in self._normalize_tags(tags or [])}
        documents = [
            self._document_from_entry(entry)
            for entry in self._repository.list()
        ]
        if not requested:
            return documents
        return [
            document
            for document in documents
            if requested <= {tag.casefold() for tag in document.tags}
        ]

    def search(
        self,
        *,
        query: str | None = None,
        kinds: list[str] | None = None,
        tags: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[WorkspaceDocument]:
        if limit < 1 or limit > 100:
            raise ValueError("Workspace search limit must be between 1 and 100.")
        if offset < 0:
            raise ValueError("Workspace search offset cannot be negative.")
        normalized_tags = self._normalize_tags(tags or [])
        return [
            self._document_from_entry(entry)
            for entry in self._repository.search(
                query=query,
                kinds=kinds or [],
                tags=normalized_tags,
                limit=limit,
                offset=offset,
            )
        ]

    def read_file(self, path: str) -> WorkspaceDocument:
        media_type, content = self._storage.read_workspace_file(path)
        return self._metadata(path, media_type=media_type, content=content)

    def write_file(
        self,
        path: str,
        content: Any,
        *,
        tags: list[str] | None = None,
    ) -> WorkspaceDocument:
        stored = self._storage.write_workspace_file(path, content)
        existing = self._repository.get(stored.relative_path)
        normalized_tags = self._normalize_tags(tags) if tags is not None else list(
            existing.tags_json if existing is not None else []
        )
        document = self._index_file(
            stored.relative_path,
            tags=normalized_tags,
            existing=existing,
        )
        return WorkspaceDocument(
            path=document.path,
            name=document.name,
            media_type=document.media_type,
            size_bytes=document.size_bytes,
            modified_at=document.modified_at,
            tags=document.tags,
            paper_id=document.paper_id,
            paper_name=document.paper_name,
            note_id=document.note_id,
            note_name=document.note_name,
            kind=document.kind,
            content=document.content,
            sha256=stored.sha256,
        )

    def delete_file(self, path: str) -> None:
        self._storage.delete_workspace_file(path)
        normalized_path = Path(path).as_posix()
        self._repository.delete_path(normalized_path)

    def delete_folder(self, path: str) -> None:
        normalized_path = Path(path).as_posix().strip("/")
        if not normalized_path or normalized_path == ".":
            raise ValueError("The workspace root cannot be deleted.")
        if normalized_path == "papers":
            raise ValueError("The top-level papers folder cannot be deleted.")
        self._storage.delete_workspace_folder(normalized_path)
        self._repository.delete_prefix(normalized_path)

    def replace_markdown(
        self,
        path: str,
        old_text: str,
        new_text: str,
        *,
        replace_all: bool = False,
    ) -> WorkspaceDocument:
        if not path.lower().endswith(".md"):
            raise ValueError("Targeted replacement is only supported for Markdown files.")
        if not old_text:
            raise ValueError("The exact text to replace cannot be empty.")
        document = self.read_file(path)
        if not isinstance(document.content, str):
            raise ValueError("Markdown content must be text.")
        occurrences = document.content.count(old_text)
        if occurrences == 0:
            raise ValueError("The exact text to replace was not found.")
        if occurrences > 1 and not replace_all:
            raise ValueError(
                "The exact text occurs more than once; provide a larger unique selection "
                "or enable replace_all."
            )
        updated = document.content.replace(old_text, new_text, -1 if replace_all else 1)
        return self.write_file(path, updated)

    def append_markdown(self, path: str, content: str) -> WorkspaceDocument:
        if not path.lower().endswith(".md"):
            raise ValueError("Append is only supported for Markdown files.")
        document = self.read_file(path)
        if not isinstance(document.content, str):
            raise ValueError("Markdown content must be text.")
        separator = "" if not document.content or document.content.endswith("\n") else "\n"
        return self.write_file(path, f"{document.content}{separator}{content}")

    def set_tags(self, path: str, tags: list[str]) -> WorkspaceDocument:
        info = self._storage.workspace_file_info(path)
        normalized_path = info.relative_path
        normalized_tags = self._normalize_tags(tags)
        existing = self._repository.get(normalized_path)
        return self._index_file(normalized_path, tags=normalized_tags, existing=existing)

    def create_note(
        self,
        *,
        name: str,
        content: str,
        tags: list[str] | None = None,
    ) -> WorkspaceDocument:
        note_name = self._normalize_note_name(name)
        note_id = str(uuid.uuid4())
        path = f"notes/{note_id}/note.md"
        body = f"# {note_name}\n"
        if content.strip():
            body += f"\n{content.strip()}\n"
        document = self.write_file(path, body, tags=["note", *(tags or [])])
        entry = self._repository.get(path)
        assert entry is not None
        entry.note_id = note_id
        entry.note_name = note_name
        entry.display_name = note_name
        entry.kind = "note"
        indexed = self._document_from_entry(self._repository.upsert(entry), content=body)
        return WorkspaceDocument(
            path=indexed.path,
            name=indexed.name,
            media_type=indexed.media_type,
            size_bytes=indexed.size_bytes,
            modified_at=indexed.modified_at,
            tags=indexed.tags,
            note_id=indexed.note_id,
            note_name=indexed.note_name,
            kind=indexed.kind,
            content=indexed.content,
            sha256=document.sha256,
        )

    def ensure_paper_folder(self, document_id: str, title: str) -> dict[str, Any]:
        safe_document_id = clean_filename(document_id)
        folder = f"papers/{safe_document_id}"
        paper_tag = f"paper:{document_id}"
        paper_name = next(
            (
                entry.paper_name
                for entry in self._repository.list_prefix(folder)
                if entry.paper_name
            ),
            self._normalize_paper_name(title),
        )
        templates = {
            f"{folder}/summary.md": (
                f"# {title}\n\n"
                "## Contribution\n\n"
                "## Detailed contributions\n\n"
                "## Experiments and results\n\n"
                "## Open research areas\n"
            ),
            f"{folder}/notes.md": f"# Notes: {title}\n",
        }
        created: list[str] = []
        for path, content in templates.items():
            try:
                self._storage.workspace_file_info(path)
            except FileNotFoundError:
                role = Path(path).stem
                self.write_file(path, content, tags=["paper", paper_tag, role])
                created.append(path)
        self.set_paper_name(document_id, paper_name)
        return {
            "document_id": document_id,
            "folder": folder,
            "summary_path": f"{folder}/summary.md",
            "notes_path": f"{folder}/notes.md",
            "paper_name": paper_name,
            "created": created,
        }

    def set_paper_name(self, document_id: str, name: str) -> dict[str, str]:
        normalized_name = self._normalize_paper_name(name)
        folder = f"papers/{clean_filename(document_id)}"
        for entry in self._repository.list_prefix(folder):
            entry.paper_id = document_id
            entry.paper_name = normalized_name
            entry.display_name = normalized_name
            entry.kind = (
                "paper_summary"
                if entry.path.endswith("/summary.md")
                else "paper_notes"
                if entry.path.endswith("/notes.md")
                else "paper_file"
            )
            self._repository.upsert(entry)
        return {
            "document_id": document_id,
            "folder": folder,
            "paper_name": normalized_name,
        }

    def _metadata(
        self,
        path: str,
        *,
        media_type: str | None = None,
        content: Any | None = None,
    ) -> WorkspaceDocument:
        normalized_path = Path(path).as_posix()
        entry = self._repository.get(normalized_path)
        if entry is None:
            entry_document = self._index_file(normalized_path)
            entry = self._repository.get(entry_document.path)
            assert entry is not None
        return self._document_from_entry(
            entry,
            media_type=media_type,
            content=content,
        )

    def _index_file(
        self,
        path: str,
        *,
        tags: list[str] | None = None,
        existing: WorkspaceEntry | None = None,
        paper_names: dict[str, str] | None = None,
    ) -> WorkspaceDocument:
        info = self._storage.workspace_file_info(path)
        media_type, content = self._storage.read_workspace_file(info.relative_path)
        searchable_content = content if isinstance(content, str) else ""
        paper_id = _paper_id_from_path(info.relative_path)
        paper_name = (
            (paper_names or {}).get(paper_id)
            if paper_id is not None
            else None
        ) or (existing.paper_name if existing is not None else None)
        entry = WorkspaceEntry(
            path=info.relative_path,
            name=Path(info.relative_path).name,
            display_name=(
                existing.display_name
                if existing is not None
                else paper_name
            ),
            kind=existing.kind if existing is not None else _kind_from_path(info.relative_path),
            media_type=media_type,
            size_bytes=info.size_bytes,
            modified_at=info.modified_at,
            tags_json=tags if tags is not None else list(existing.tags_json if existing else []),
            tags_text=_tags_text(tags if tags is not None else list(existing.tags_json if existing else [])),
            paper_id=paper_id,
            paper_name=paper_name,
            note_id=existing.note_id if existing is not None else None,
            note_name=existing.note_name if existing is not None else None,
            search_content=searchable_content,
        )
        return self._document_from_entry(
            self._repository.upsert(entry),
            content=content,
        )

    def _index_existing_files(self) -> None:
        tag_map = self._read_legacy_tag_map()
        paper_names = self._read_legacy_paper_names()
        for path in self._storage.list_workspace_files():
            self._index_file(
                path,
                tags=tag_map.get(path, []),
                paper_names=paper_names,
            )

    def _read_legacy_tag_map(self) -> dict[str, list[str]]:
        try:
            media_type, content = self._storage.read_workspace_file(_TAG_INDEX_PATH)
        except FileNotFoundError:
            return {}
        if media_type != "application/json" or not isinstance(content, dict):
            raise ValueError("Workspace tag index is invalid.")
        tag_map: dict[str, list[str]] = {}
        for path, tags in content.items():
            if not isinstance(path, str) or not isinstance(tags, list):
                raise ValueError("Workspace tag index is invalid.")
            tag_map[path] = self._normalize_tags(tags)
        return tag_map

    def _read_legacy_paper_names(self) -> dict[str, str]:
        try:
            media_type, content = self._storage.read_workspace_file(_PAPER_INDEX_PATH)
        except FileNotFoundError:
            return {}
        if media_type != "application/json" or not isinstance(content, dict):
            raise ValueError("Workspace paper metadata index is invalid.")
        paper_names: dict[str, str] = {}
        for document_id, name in content.items():
            if not isinstance(document_id, str) or not isinstance(name, str):
                raise ValueError("Workspace paper metadata index is invalid.")
            paper_names[document_id] = self._normalize_paper_name(name)
        return paper_names

    @staticmethod
    def _document_from_entry(
        entry: WorkspaceEntry,
        *,
        media_type: str | None = None,
        content: Any | None = None,
    ) -> WorkspaceDocument:
        return WorkspaceDocument(
            path=entry.path,
            name=entry.name,
            media_type=media_type or entry.media_type,
            size_bytes=entry.size_bytes,
            modified_at=entry.modified_at,
            tags=tuple(entry.tags_json or []),
            paper_id=entry.paper_id,
            paper_name=entry.paper_name,
            note_id=entry.note_id,
            note_name=entry.note_name,
            kind=entry.kind,
            content=content,
        )

    @staticmethod
    def _normalize_tags(tags: list[Any]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in tags:
            if not isinstance(value, str):
                raise ValueError("Workspace tags must be strings.")
            tag = value.strip()
            if not tag:
                continue
            if len(tag) > _MAX_TAG_LENGTH:
                raise ValueError(f"Workspace tags cannot exceed {_MAX_TAG_LENGTH} characters.")
            key = tag.casefold()
            if key not in seen:
                normalized.append(tag)
                seen.add(key)
        if len(normalized) > _MAX_TAGS:
            raise ValueError(f"A workspace file cannot have more than {_MAX_TAGS} tags.")
        return normalized

    @staticmethod
    def _normalize_paper_name(name: str) -> str:
        normalized = name.strip()
        if not normalized:
            raise ValueError("Paper name cannot be empty.")
        if len(normalized) > _MAX_PAPER_NAME_LENGTH:
            raise ValueError(
                f"Paper name cannot exceed {_MAX_PAPER_NAME_LENGTH} characters."
            )
        return normalized

    @staticmethod
    def _normalize_note_name(name: str) -> str:
        normalized = name.strip()
        if not normalized:
            raise ValueError("Note name cannot be empty.")
        if len(normalized) > _MAX_PAPER_NAME_LENGTH:
            raise ValueError(
                f"Note name cannot exceed {_MAX_PAPER_NAME_LENGTH} characters."
            )
        return normalized


def _media_type(path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix == ".json":
        return "application/json"
    if suffix == ".md":
        return "text/markdown"
    return "text/plain"


def _paper_id_from_path(path: str) -> str | None:
    parts = Path(path).parts
    if len(parts) >= 3 and parts[0] == "papers":
        return parts[1]
    return None


def _kind_from_path(path: str) -> str:
    parts = Path(path).parts
    if len(parts) >= 3 and parts[0] == "papers":
        if parts[-1] == "summary.md":
            return "paper_summary"
        if parts[-1] == "notes.md":
            return "paper_notes"
        return "paper_file"
    return "file"


def _tags_text(tags: list[str]) -> str:
    normalized = "\n".join(tag.casefold() for tag in tags)
    return f"\n{normalized}\n"
