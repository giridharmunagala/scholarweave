from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import UploadFile

from backend.core.config import Settings
from backend.core.json import dumps_json, loads_json
from backend.core.text import clean_filename
from backend.persistence.hashing import sha256_bytes


class StorageError(ValueError):
    pass


@dataclass(slots=True)
class StoredFile:
    relative_path: str
    absolute_path: Path
    size_bytes: int
    sha256: str


@dataclass(slots=True)
class WorkspaceMarkdownFile:
    relative_path: str
    size_bytes: int
    modified_at: datetime


@dataclass(frozen=True, slots=True)
class WorkspaceFileInfo:
    relative_path: str
    size_bytes: int
    modified_at: datetime


class SafeStorage:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _safe_path(self, base_dir: Path, relative_path: str, allowed_suffixes: set[str] | None = None) -> Path:
        path = Path(relative_path)
        if path.is_absolute():
            raise StorageError("Absolute paths are not allowed")
        resolved_base = base_dir.resolve()
        resolved_target = (resolved_base / path).resolve()
        try:
            resolved_target.relative_to(resolved_base)
        except ValueError as exc:
            raise StorageError("Path escapes the allowed directory") from exc
        if allowed_suffixes and resolved_target.suffix.lower() not in allowed_suffixes:
            raise StorageError(f"Unsupported file type: {resolved_target.suffix}")
        return resolved_target

    def write_bytes(self, base_dir: Path, relative_path: str, content: bytes) -> StoredFile:
        if len(content) > self.settings.max_artifact_bytes:
            raise StorageError("Artifact exceeds maximum allowed size")
        return self._write_bytes(base_dir, relative_path, content)

    def write_document_bytes(self, relative_path: str, content: bytes) -> StoredFile:
        if len(content) > self.settings.max_upload_bytes:
            raise StorageError("Document exceeds maximum allowed size")
        return self._write_bytes(self.settings.documents_dir, relative_path, content)

    def _write_bytes(self, base_dir: Path, relative_path: str, content: bytes) -> StoredFile:
        absolute = self._safe_path(base_dir, relative_path)
        absolute.parent.mkdir(parents=True, exist_ok=True)
        absolute.write_bytes(content)
        return StoredFile(
            relative_path=str(Path(relative_path)),
            absolute_path=absolute,
            size_bytes=len(content),
            sha256=sha256_bytes(content),
        )

    def write_text(self, base_dir: Path, relative_path: str, content: str) -> StoredFile:
        data = content.encode("utf-8")
        return self.write_bytes(base_dir, relative_path, data)

    def write_json(self, base_dir: Path, relative_path: str, content: Any) -> StoredFile:
        return self.write_text(base_dir, relative_path, dumps_json(content))

    async def save_upload(self, upload: UploadFile, relative_dir: str) -> StoredFile:
        filename = clean_filename(upload.filename or "upload.bin")
        content = await upload.read()
        if len(content) > self.settings.max_upload_bytes:
            raise StorageError("Upload exceeds maximum allowed size")
        relative_path = str(Path(relative_dir) / filename)
        return self.write_document_bytes(relative_path, content)

    def read_workspace_file(self, relative_path: str) -> tuple[str, Any]:
        allowed = {".txt", ".md", ".json"}
        absolute = self._safe_path(self.settings.workspace_dir, relative_path, allowed_suffixes=allowed)
        content = absolute.read_bytes()
        if len(content) > self.settings.max_workspace_file_bytes:
            raise StorageError("Workspace file exceeds maximum allowed size")
        decoded = content.decode("utf-8")
        if absolute.suffix.lower() == ".json":
            return ("application/json", loads_json(decoded, default={}))
        media_type = "text/markdown" if absolute.suffix.lower() == ".md" else "text/plain"
        return (media_type, decoded)

    def list_workspace_markdown(self) -> list[WorkspaceMarkdownFile]:
        resolved_base = self.settings.workspace_dir.resolve()
        files: list[WorkspaceMarkdownFile] = []
        for candidate in resolved_base.rglob("*"):
            if not candidate.is_file() or candidate.suffix.lower() != ".md":
                continue
            resolved_candidate = candidate.resolve()
            try:
                relative_path = resolved_candidate.relative_to(resolved_base)
            except ValueError:
                continue
            stat = resolved_candidate.stat()
            files.append(
                WorkspaceMarkdownFile(
                    relative_path=relative_path.as_posix(),
                    size_bytes=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                )
            )
        return sorted(files, key=lambda item: item.relative_path.casefold())

    def list_workspace_files(self) -> list[str]:
        """Lists the safe text formats exposed to workspace tools."""
        resolved_base = self.settings.workspace_dir.resolve()
        files: list[str] = []
        for candidate in resolved_base.rglob("*"):
            if not candidate.is_file() or candidate.suffix.lower() not in {".txt", ".md", ".json"}:
                continue
            try:
                relative_path = candidate.resolve().relative_to(resolved_base)
            except ValueError:
                continue
            if relative_path.parts and relative_path.parts[0] == ".scholarweave":
                continue
            files.append(relative_path.as_posix())
        return sorted(files, key=str.casefold)

    def workspace_file_info(self, relative_path: str) -> WorkspaceFileInfo:
        absolute = self._safe_path(
            self.settings.workspace_dir,
            relative_path,
            allowed_suffixes={".txt", ".md", ".json"},
        )
        stat = absolute.stat()
        return WorkspaceFileInfo(
            relative_path=absolute.resolve().relative_to(
                self.settings.workspace_dir.resolve()
            ).as_posix(),
            size_bytes=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        )

    def delete_workspace_file(self, relative_path: str) -> None:
        absolute = self._safe_path(
            self.settings.workspace_dir,
            relative_path,
            allowed_suffixes={".txt", ".md", ".json"},
        )
        if not absolute.exists():
            raise FileNotFoundError(relative_path)
        if not absolute.is_file():
            raise StorageError("Workspace path is not a file")
        absolute.unlink()
        self._remove_empty_parents(absolute.parent, self.settings.workspace_dir)

    def delete_workspace_folder(self, relative_path: str) -> None:
        absolute = self._safe_path(self.settings.workspace_dir, relative_path)
        resolved_base = self.settings.workspace_dir.resolve()
        if absolute.resolve() == resolved_base:
            raise StorageError("Refusing to delete the workspace root")
        if not absolute.exists():
            raise FileNotFoundError(relative_path)
        if not absolute.is_dir():
            raise StorageError("Workspace path is not a folder")
        relative = absolute.resolve().relative_to(resolved_base)
        if relative.parts and relative.parts[0] == ".scholarweave":
            raise StorageError("Refusing to delete workspace metadata")
        shutil.rmtree(absolute)
        self._remove_empty_parents(absolute.parent, self.settings.workspace_dir)

    def read_workspace_markdown(self, relative_path: str) -> tuple[WorkspaceMarkdownFile, str]:
        media_type, content = self.read_workspace_file(relative_path)
        if media_type != "text/markdown" or not isinstance(content, str):
            raise StorageError("Only Markdown notes can be viewed")
        absolute = self._safe_path(self.settings.workspace_dir, relative_path, allowed_suffixes={".md"})
        stat = absolute.stat()
        return (
            WorkspaceMarkdownFile(
                relative_path=Path(relative_path).as_posix(),
                size_bytes=stat.st_size,
                modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            ),
            content,
        )

    def delete_workspace_markdown(self, relative_path: str) -> None:
        absolute = self._safe_path(self.settings.workspace_dir, relative_path, allowed_suffixes={".md"})
        if not absolute.exists():
            raise FileNotFoundError(relative_path)
        if not absolute.is_file():
            raise StorageError("Note path is not a file")
        absolute.unlink()
        self._remove_empty_parents(absolute.parent, self.settings.workspace_dir)

    def delete_stored_file(self, base_dir: Path, relative_path: str) -> None:
        absolute = self._safe_path(base_dir, relative_path)
        if absolute.exists():
            if not absolute.is_file():
                raise StorageError("Stored artifact path is not a file")
            absolute.unlink()
        self._remove_empty_parents(absolute.parent, base_dir)

    def delete_stored_tree(self, base_dir: Path, relative_dir: str) -> None:
        absolute = self._safe_path(base_dir, relative_dir)
        if absolute.resolve() == base_dir.resolve():
            raise StorageError("Refusing to delete the storage root")
        if not absolute.exists():
            return
        if not absolute.is_dir():
            raise StorageError("Stored artifact path is not a directory")
        shutil.rmtree(absolute)
        self._remove_empty_parents(absolute.parent, base_dir)

    @staticmethod
    def _remove_empty_parents(directory: Path, base_dir: Path) -> None:
        resolved_base = base_dir.resolve()
        current = directory.resolve()
        while current != resolved_base:
            try:
                current.relative_to(resolved_base)
                current.rmdir()
            except (ValueError, OSError):
                break
            current = current.parent

    def write_workspace_file(self, relative_path: str, content: Any) -> StoredFile:
        allowed = {".txt", ".md", ".json"}
        absolute = self._safe_path(self.settings.workspace_dir, relative_path, allowed_suffixes=allowed)
        absolute.parent.mkdir(parents=True, exist_ok=True)
        if absolute.suffix.lower() == ".json":
            serialized = dumps_json(content)
        elif isinstance(content, str):
            serialized = content
        else:
            serialized = dumps_json(content)
        data = serialized.encode("utf-8")
        if len(data) > self.settings.max_workspace_file_bytes:
            raise StorageError("Workspace write exceeds maximum allowed size")
        absolute.write_bytes(data)
        return StoredFile(
            relative_path=relative_path,
            absolute_path=absolute,
            size_bytes=len(data),
            sha256=sha256_bytes(data),
        )

    def clear_workspace_folder_markdown(self, relative_dir: str) -> None:
        """Removes the Markdown files directly inside one workspace folder.

        Used when a note folder is rewritten, so a shorter second run does not leave
        stale notes behind. Nested folders and non-Markdown files are left alone.
        """
        absolute = self._safe_path(self.settings.workspace_dir, relative_dir)
        if not absolute.is_dir():
            return
        for candidate in sorted(absolute.glob("*.md")):
            if candidate.is_file():
                candidate.unlink()

    def read_artifact(self, relative_path: str) -> bytes:
        absolute = self._safe_path(self.settings.artifacts_dir, relative_path)
        return absolute.read_bytes()
