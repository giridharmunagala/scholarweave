from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.utils import dumps_json
from backend.core.errors import ValidationError
from backend.persistence.files import SafeStorage, StorageError
from backend.workspace.models import WorkspaceEntry
from backend.workspace.repository import WorkspaceRepository
from backend.workspace.layout import WorkspaceLayout

_MAX_TAGS = 32
_MAX_TAG_LENGTH = 64
_MAX_PAPER_NAME_LENGTH = 300
WORKSPACE_KINDS = ("note", "paper_summary", "paper_notes", "paper_file", "file")
WORKSPACE_COLLECTIONS = {
    "notes": ["note", "paper_notes"],
    "summaries": ["paper_summary"],
    "files": [],
}


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
    score: float | None = None
    excerpt: str | None = None


class WorkspaceService:
    def __init__(
        self,
        storage: SafeStorage,
        repository: WorkspaceRepository | None = None,
    ) -> None:
        self._storage = storage
        self._index_lock = threading.RLock()
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
        if query is not None and (not query.strip() or len(query) > 2000):
            raise ValidationError("Workspace query must contain 1 to 2000 characters.")
        if any(kind not in WORKSPACE_KINDS for kind in kinds or []):
            raise ValidationError("Unknown workspace document kind.")
        normalized_tags = self._normalize_tags(tags or [])
        return [
            replace(self._document_from_entry(entry), score=score, excerpt=excerpt)
            for entry, score, excerpt in self._repository.search(
                query=query,
                kinds=kinds or [],
                tags=normalized_tags,
                limit=limit,
                offset=offset,
            )
        ]

    def list_collection(
        self, collection: str, *, limit: int = 50, offset: int = 0,
    ) -> list[WorkspaceDocument]:
        if collection not in WORKSPACE_COLLECTIONS:
            raise ValidationError("Unknown workspace collection.")
        return self.search(kinds=WORKSPACE_COLLECTIONS[collection], limit=limit, offset=offset)

    def index_status(self) -> dict[str, Any]:
        return {"engine": "sqlite-fts5-bm25", "indexed_files": self._repository.count()}

    def refresh_index(self) -> dict[str, Any]:
        with self._index_lock:
            existing = {entry.path: entry for entry in self._repository.list()}
            entries = [
                self._entry_from_file(path, existing=existing.get(path))[0]
                for path in self._storage.list_workspace_files()
            ]
            self._repository.rebuild(entries)
            return {**self.index_status(), "removed_files": len(existing.keys() - {e.path for e in entries})}

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
        normalized_tags = self._normalize_tags(tags) if tags is not None else None
        with self._index_lock:
            stored = self._storage.write_workspace_file(path, content)
            document = self._index_file(
                stored.relative_path, tags=normalized_tags,
                existing=self._repository.get(stored.relative_path),
            )
            return replace(document, sha256=stored.sha256)

    def delete_file(self, path: str) -> None:
        with self._index_lock:
            normalized_path = self._storage.workspace_file_info(path).relative_path
            self._storage.delete_workspace_file(normalized_path)
            self._repository.delete_path(normalized_path)

    def organize_file(
        self,
        *,
        action: str,
        path: str,
        destination: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Apply an explicit, single-file agent operation without touching managed artifacts."""
        if action not in {"move_file", "set_tags", "delete_file"}:
            raise ValidationError("Workspace action must be move_file, set_tags, or delete_file.")
        if action != "move_file" and destination is not None:
            raise ValidationError("destination must be null except for move_file.")
        if action != "set_tags" and tags is not None:
            raise ValidationError("tags must be null except for set_tags.")
        with self._index_lock:
            normalized_path = self._organizable_path(path)
            existing = self._repository.get(normalized_path)
            if existing is None:
                raise ValidationError(
                    "Select an indexed file from list_workspace or search_research_notes; "
                    "refresh the workspace index first for external files."
                )
            if not self._storage.resolve_path(
                self._storage.settings.workspace_dir, normalized_path,
            ).is_file():
                raise ValidationError("The selected workspace file no longer exists.")
            if action == "delete_file":
                self.delete_file(normalized_path)
                return {"path": normalized_path, "deleted": True}
            if action == "set_tags":
                if not isinstance(tags, list):
                    raise ValidationError("set_tags requires a tags array; use [] to clear tags.")
                try:
                    normalized_tags = self._normalize_tags(tags)
                except ValueError as error:
                    raise ValidationError(str(error)) from error
                document = self.set_tags(normalized_path, normalized_tags)
                return {"path": document.path, "tags": list(document.tags)}
            target = self._organizable_path(destination)
            if self._repository.get(target) is not None:
                raise ValidationError("Workspace destination already exists in the index.")
            try:
                self._storage.move_workspace_file(normalized_path, target)
            except StorageError as error:
                raise ValidationError(str(error)) from error
            indexed = False
            try:
                entry, _ = self._entry_from_file(target, existing=existing)
                self._repository.upsert(entry, previous_path=normalized_path)
                indexed = True
            finally:
                if not indexed:
                    self._storage.move_workspace_file(target, normalized_path)
            return {"path": target, "previous_path": normalized_path, "tags": list(entry.tags_json)}

    def _organizable_path(self, path: str | None) -> str:
        if not isinstance(path, str) or not path.strip():
            raise ValidationError("Select one explicit workspace-relative file path.")
        parts = path.replace("\\", "/").split("/")
        if any(not part or part.endswith((".", " ")) for part in parts) or any(
            char in path for char in ':*?<>|\x00'
        ):
            raise ValidationError("Absolute paths, traversal, and wildcard patterns are not allowed.")
        normalized = "/".join(parts)
        try:
            absolute = self._storage.resolve_path(self._storage.settings.workspace_dir, normalized)
        except StorageError as error:
            raise ValidationError(str(error)) from error
        resolved = absolute.relative_to(self._storage.settings.workspace_dir.resolve()).as_posix()
        if resolved.casefold() != normalized.casefold():
            raise ValidationError("Workspace organization does not follow symbolic links.")
        lowered = resolved.casefold().split("/")
        if lowered[0] == ".scholarweave" or lowered[:2] in [
            ["library", "papers"], ["inbox", "attachments"],
        ]:
            raise ValidationError("Managed paper files, attachments, and workspace metadata are protected.")
        if absolute.suffix.lower() not in {".md", ".txt", ".json"}:
            raise ValidationError("Select one standalone .md, .txt, or .json file, not a folder.")
        return resolved

    def delete_folder(self, path: str) -> None:
        normalized_path = Path(path).as_posix().strip("/")
        if not normalized_path or normalized_path == ".":
            raise ValueError("The workspace root cannot be deleted.")
        if normalized_path in {"library", "knowledge", "projects", "inbox"} or (
            normalized_path == WorkspaceLayout.paper_root
            or normalized_path.startswith(f"{WorkspaceLayout.paper_root}/")
        ):
            raise ValueError("Managed research folders cannot be deleted through workspace tools.")
        with self._index_lock:
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
        with self._index_lock:
            normalized_path = self._storage.workspace_file_info(path).relative_path
            return self._index_file(
                normalized_path, tags=self._normalize_tags(tags),
                existing=self._repository.get(normalized_path),
            )

    def create_note(
        self,
        *,
        name: str,
        content: str,
        tags: list[str] | None = None,
    ) -> WorkspaceDocument:
        note_name = self._normalize_note_name(name)
        note_id = str(uuid.uuid4())
        path = WorkspaceLayout.knowledge_note(note_id, note_name)
        body = f"# {note_name}\n"
        if content.strip():
            body += f"\n{content.strip()}\n"
        with self._index_lock:
            document = self.write_file(path, body, tags=["note", *(tags or [])])
            entry = self._repository.get(path)
            assert entry is not None
            entry.note_id = note_id
            entry.note_name = note_name
            entry.display_name = note_name
            entry.kind = "note"
            indexed = self._document_from_entry(self._repository.upsert(entry), content=body)
            return replace(indexed, sha256=document.sha256)

    def paper_folder(self, document_id: str, title: str | None = None) -> str:
        name = self._normalize_paper_name(title) if title is not None else None
        return self._repository.paper(document_id, name).folder

    def delete_paper_folder(self, document_id: str) -> None:
        with self._index_lock:
            folder = self.paper_folder(document_id)
            self._storage.delete_stored_tree(self._storage.settings.workspace_dir, folder)
            self._repository.delete_prefix(folder)
            self._repository.delete_paper(document_id)

    def ensure_paper_folder(self, document_id: str, title: str) -> dict[str, Any]:
        paper = self._repository.paper(document_id, self._normalize_paper_name(title))
        folder = paper.folder
        paper_tag = f"paper:{document_id}"
        paper_name = paper.name
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
            existing = self._repository.get(path)
            role = Path(path).stem
            required_tags = ["paper", paper_tag, role]
            existing_tags = list(existing.tags_json or []) if existing is not None else []
            tags = self._normalize_tags([*existing_tags, *required_tags])
            try:
                self._storage.workspace_file_info(path)
            except FileNotFoundError:
                self.write_file(path, content, tags=tags)
                created.append(path)
                continue
            self._index_file(
                path,
                tags=tags,
                existing=existing,
                paper_names={document_id: paper_name},
            )
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
        folder = self.paper_folder(document_id)
        with self._index_lock:
            self._repository.rename_paper(document_id, normalized_name)
            for entry in self._repository.list_prefix(folder):
                entry.paper_id = document_id
                entry.paper_name = normalized_name
                entry.display_name = normalized_name
                entry.kind = WorkspaceLayout.kind(entry.path)
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
        with self._index_lock:
            entry, content = self._entry_from_file(
                path, tags=tags, existing=existing, paper_names=paper_names,
            )
            stored = self._repository.upsert(entry)
            return self._document_from_entry(stored, content=content)

    def _entry_from_file(
        self, path: str, *, tags: list[str] | None = None,
        existing: WorkspaceEntry | None = None,
        paper_names: dict[str, str] | None = None,
    ) -> tuple[WorkspaceEntry, Any]:
        info = self._storage.workspace_file_info(path)
        media_type, content = self._storage.read_workspace_file(info.relative_path)
        searchable_content = content if isinstance(content, str) else dumps_json(content)
        paper_id = WorkspaceLayout.paper_id(info.relative_path)
        paper_name = (
            (paper_names or {}).get(paper_id)
            if paper_id is not None
            else None
        ) or (existing.paper_name if existing is not None else None)
        if paper_id and paper_name is None:
            try:
                paper_name = self._repository.paper(paper_id).name
            except FileNotFoundError:
                pass
        normalized_tags = tags if tags is not None else list(existing.tags_json if existing else [])
        entry = WorkspaceEntry(
            path=info.relative_path,
            name=Path(info.relative_path).name,
            display_name=(
                existing.display_name
                if existing is not None
                else paper_name
            ),
            kind="note" if existing is not None and existing.kind == "note" else WorkspaceLayout.kind(info.relative_path),
            media_type=media_type,
            size_bytes=info.size_bytes,
            modified_at=info.modified_at,
            tags_json=normalized_tags,
            tags_text=_tags_text(normalized_tags),
            paper_id=paper_id,
            paper_name=paper_name,
            note_id=existing.note_id if existing is not None else None,
            note_name=existing.note_name if existing is not None else None,
            search_content=searchable_content,
        )
        return entry, content

    def _index_existing_files(self) -> None:
        for path in self._storage.list_workspace_files():
            self._index_file(path)

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


def _tags_text(tags: list[str]) -> str:
    normalized = "\n".join(tag.casefold() for tag in tags)
    return f"\n{normalized}\n"
