from __future__ import annotations

import shutil
import hashlib
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import UploadFile

from backend.core.config import Settings
from backend.utils import clean_filename, dumps_json, loads_json, sha256_bytes


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

    def write_workspace_document(self, relative_path: str, content: bytes) -> StoredFile:
        if len(content) > self.settings.max_upload_bytes:
            raise StorageError("Document exceeds maximum allowed size")
        self._safe_path(self.settings.workspace_dir, relative_path, {".pdf"})
        return self._write_bytes(self.settings.workspace_dir, relative_path, content)

    def resolve_path(self, base_dir: Path, relative_path: str) -> Path:
        return self._safe_path(base_dir, relative_path)

    def file_hash(self, base_dir: Path, relative_path: str) -> str:
        with self._safe_path(base_dir, relative_path).open("rb") as source:
            return hashlib.file_digest(source, "sha256").hexdigest()

    def copy_verified(self, source_base: Path, source_path: str, target_base: Path, target_path: str, expected_hash: str) -> None:
        source = self._safe_path(source_base, source_path)
        target = self._safe_path(target_base, target_path)
        if self.file_hash(source_base, source_path) != expected_hash:
            raise StorageError(f"Source changed during workspace upgrade: {source_path}")
        if target.exists():
            if self.file_hash(target_base, target_path) != expected_hash:
                raise StorageError(f"Workspace upgrade destination already exists: {target_path}")
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            shutil.copyfile(source, temporary)
            if self.file_hash(target_base, temporary.relative_to(target_base.resolve()).as_posix()) != expected_hash:
                raise StorageError(f"Workspace upgrade copy verification failed: {target_path}")
            with temporary.open("r+b") as copied:
                os.fsync(copied.fileno())
            temporary.rename(target)
        finally:
            temporary.unlink(missing_ok=True)

    def snapshot_tree(self, source_base: Path, target_base: Path) -> dict[str, str]:
        if not source_base.is_dir() or source_base.is_symlink():
            raise StorageError(f"Snapshot source is not a regular directory: {source_base}")
        target_base.mkdir(parents=True, exist_ok=True)
        manifest: dict[str, str] = {}
        for source in sorted(source_base.rglob("*")):
            if source.is_symlink() or (hasattr(source, "is_junction") and source.is_junction()):
                raise StorageError("Workspace upgrades do not follow symbolic links or junctions.")
            if not source.is_file():
                continue
            relative = source.relative_to(source_base).as_posix()
            digest = self.file_hash(source_base, relative)
            self.copy_verified(source_base, relative, target_base, relative, digest)
            manifest[relative] = digest
        return manifest

    def write_upgrade_journal(self, backup_dir: Path, content: Any) -> None:
        temporary = self.write_json(backup_dir, "journal.pending.json", content)
        with temporary.absolute_path.open("r+b") as journal:
            os.fsync(journal.fileno())
        temporary.absolute_path.replace(self._safe_path(backup_dir, "journal.json"))

    def restore_tree(self, snapshot: Path, destination: Path) -> None:
        staged = destination.with_name(f"{destination.name}.layout-restore")
        preserved = destination.with_name(f"{destination.name}.before-layout-restore")
        if preserved.exists():
            raise StorageError(f"Preserved restore directory already exists: {preserved}")
        staged.mkdir(parents=True, exist_ok=True)
        self.snapshot_tree(snapshot, staged)
        if destination.exists():
            destination.rename(preserved)
        staged.rename(destination)

    async def read_upload(self, upload: UploadFile) -> bytes:
        content = bytearray()
        while chunk := await upload.read(min(1024 * 1024, self.settings.max_upload_bytes + 1 - len(content))):
            content.extend(chunk)
            if len(content) > self.settings.max_upload_bytes:
                raise StorageError("Upload exceeds maximum allowed size")
        return bytes(content)

    def _write_bytes(self, base_dir: Path, relative_path: str, content: bytes) -> StoredFile:
        absolute = self._safe_path(base_dir, relative_path)
        absolute.parent.mkdir(parents=True, exist_ok=True)
        absolute.write_bytes(content)
        return StoredFile(
            relative_path=absolute.resolve().relative_to(base_dir.resolve()).as_posix(),
            absolute_path=absolute,
            size_bytes=len(content),
            sha256=sha256_bytes(content),
        )

    def write_text(self, base_dir: Path, relative_path: str, content: str) -> StoredFile:
        data = content.encode("utf-8")
        return self.write_bytes(base_dir, relative_path, data)

    def write_json(self, base_dir: Path, relative_path: str, content: Any) -> StoredFile:
        return self.write_text(base_dir, relative_path, dumps_json(content))

    def read_text(
        self,
        base_dir: Path,
        relative_path: str,
        *,
        allowed_suffixes: set[str] | None = None,
    ) -> str:
        absolute = self._safe_path(
            base_dir,
            relative_path,
            allowed_suffixes=allowed_suffixes,
        )
        content = absolute.read_bytes()
        if len(content) > self.settings.max_artifact_bytes:
            raise StorageError("Artifact exceeds maximum allowed size")
        return content.decode("utf-8")

    async def save_upload(self, upload: UploadFile, relative_dir: str) -> StoredFile:
        filename = clean_filename(upload.filename or "upload.bin")
        content = await self.read_upload(upload)
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
        if relative.as_posix() in {"library", "knowledge", "projects", "inbox"} or relative.parts[:2] == ("library", "papers"):
            raise StorageError("Managed research folders require an explicit domain deletion")
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
            relative_path=absolute.resolve()
            .relative_to(self.settings.workspace_dir.resolve())
            .as_posix(),
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
