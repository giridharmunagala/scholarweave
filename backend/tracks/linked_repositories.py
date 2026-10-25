from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath

from backend.core.errors import ConflictError, NotFoundError, ValidationError
from backend.persistence.files import SafeStorage, StorageError
from backend.tracks.repository_schemas import ArtifactLinkRequest, ArtifactPromotionRequest, RepositoryLink
from backend.tracks.service import TrackService


class LinkedRepositoryService:
    """Explicit, local-only file access; never invokes git, a shell, or a model."""

    TEXT_LIMIT = 256 * 1024
    FILE_LIMIT = 10 * 1024 * 1024
    ENTRY_LIMIT = 300
    BLOCKED = {
        "node_modules", "vendor", "venv", "__pycache__", "site-packages",
        "secrets", "credentials", "env", "dist", "build", "target",
        "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    }
    SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".keystore"}
    IMAGES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}

    def __init__(self, storage: SafeStorage, tracks: TrackService) -> None:
        self.storage = storage
        self.tracks = tracks
        self.lock = tracks.workspace.mutation_lock

    def _metadata_path(self, track_id: str) -> str:
        self.tracks.get(track_id)
        return f"linked_repositories/{hashlib.sha256(track_id.encode()).hexdigest()}.json"

    def _load(self, track_id: str) -> list[dict]:
        try:
            return json.loads(self.storage.read_stored_bytes(
                self.storage.settings.data_dir, self._metadata_path(track_id),
            ))
        except FileNotFoundError:
            return []
        except (OSError, StorageError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValidationError("Linked repository metadata is unreadable.") from exc

    def _save(self, track_id: str, links: list[dict]) -> None:
        try:
            self.storage.write_bytes_atomic(
                self.storage.settings.data_dir, self._metadata_path(track_id),
                json.dumps(links, ensure_ascii=False).encode("utf-8"),
            )
        except (OSError, StorageError) as exc:
            raise ValidationError("Unable to persist linked repository metadata.") from exc

    @staticmethod
    def _plain(path: Path) -> None:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValidationError("Symbolic links and Windows reparse points are not allowed.")
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            raise ValidationError("Hard-linked repository files are not supported.")

    def _root(self, link: dict) -> Path:
        root = Path(link["root"])
        try:
            for part in [*reversed(root.parents), root]:
                self._plain(part)
            info = root.stat()
            if not root.is_dir() or [info.st_dev, info.st_ino] != link["identity"]:
                raise ValidationError("The authorized repository root changed; unlink and authorize it again.")
            return root
        except OSError as exc:
            raise ValidationError("The authorized repository root is unavailable.") from exc

    @classmethod
    def _allowed(cls, name: str) -> bool:
        lower = name.lower()
        return not (
            lower.startswith((".", "env.")) or lower in cls.BLOCKED
            or re.search(r"(?:^|[._-])(?:secrets?|credentials?|passwords?|tokens?)(?:[._-]|$)", lower)
            or Path(lower).suffix in cls.SECRET_SUFFIXES
        )

    def _path(self, link: dict, relative: str) -> tuple[Path, str]:
        root = self._root(link)
        windows = PureWindowsPath(relative)
        parts = relative.replace("\\", "/").split("/") if relative else []
        if windows.drive or windows.root or any(
            p in {"", ".", ".."} or ":" in p or p.endswith((" ", "."))
            or any(ord(c) < 32 for c in p) or not self._allowed(p)
            or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", p)
            for p in parts
        ):
            raise ValidationError("Path is excluded or escapes the authorized repository.")
        path = root
        try:
            for part in parts:
                path = path / part
                self._plain(path)
            resolved = self.storage.resolve_path(root, "/".join(parts))
            if resolved != path:
                raise ValidationError("Repository paths must not follow links.")
            info = resolved.stat()
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise ValidationError("Only regular files and directories are supported.")
            if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                raise ValidationError("Hard-linked repository files are not supported.")
            return resolved, "/".join(parts)
        except OSError as exc:
            raise ValidationError("Repository path is missing or inaccessible.") from exc

    def _find(self, track_id: str, link_id: str) -> tuple[list[dict], dict]:
        links = self._load(track_id)
        link = next((item for item in links if item["id"] == link_id), None)
        if link is None:
            raise NotFoundError("Linked repository was not found.")
        return links, link

    def _read(self, link: dict, relative: str) -> tuple[Path, str, bytes]:
        path, normalized = self._path(link, relative)
        if not path.is_file():
            raise ValidationError("Select a regular file.")
        try:
            before = path.stat()
            content = self.storage.read_stored_bytes(Path(link["root"]), normalized, max_bytes=self.FILE_LIMIT)
            self._path(link, normalized)
            after = path.stat()
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
            ):
                raise ConflictError("File changed during reading; refresh and retry.")
        except (OSError, StorageError) as exc:
            raise ValidationError("File is inaccessible or exceeds the 10 MiB preview limit.") from exc
        return path, normalized, content

    def _revision(self, link: dict) -> str | None:
        # This is observational metadata, never proof of which code produced an artifact.
        try:
            root = self._root(link)
            git = root / ".git"
            self._plain(git)
            if not git.is_dir():
                return None
            head = git / "HEAD"
            self._plain(head)
            value = self.storage.read_stored_bytes(git, "HEAD", max_bytes=1024).decode().strip()
            if value.startswith("ref: "):
                ref = value[5:]
                if not re.fullmatch(r"refs/[A-Za-z0-9_./-]+", ref) or ".." in ref:
                    return None
                current = git
                for part in ref.split("/"):
                    current /= part
                    self._plain(current)
                value = self.storage.read_stored_bytes(git, ref, max_bytes=1024).decode().strip()
            return value if re.fullmatch(r"[0-9a-f]{40,64}", value) else None
        except (OSError, StorageError, ValidationError, UnicodeError):
            return None

    def list(self, track_id: str) -> list[RepositoryLink]:
        with self.lock:
            return [RepositoryLink.model_validate({k: v for k, v in item.items() if k != "identity"})
                    for item in self._load(track_id)]

    def link(self, track_id: str, root: str, authorize_root: bool) -> RepositoryLink:
        with self.lock:
            if not authorize_root or not Path(root).is_absolute():
                raise ValidationError("Explicitly authorize an absolute local repository root.")
            path = Path(os.path.abspath(root))
            if path == Path(path.anchor):
                raise ValidationError("A filesystem root cannot be authorized.")
            try:
                info = path.stat()
                item = dict(id=str(uuid.uuid4()), root=str(path), identity=[info.st_dev, info.st_ino],
                            linked_at=datetime.now(timezone.utc).isoformat(), artifacts=[], current_revision=None)
                self._root(item)
                if not (path / ".git").exists():
                    raise ValidationError("Choose an existing code repository containing .git.")
            except OSError as exc:
                raise ValidationError("Repository root is unavailable.") from exc
            links = self._load(track_id)
            if len(links) >= 20:
                raise ValidationError("A track supports at most 20 linked repositories.")
            if any(os.path.normcase(existing["root"]) == os.path.normcase(str(path)) for existing in links):
                raise ConflictError("This repository is already linked.")
            item["current_revision"] = self._revision(item)
            self._save(track_id, [*links, item])
            return RepositoryLink.model_validate({k: v for k, v in item.items() if k != "identity"})

    def unlink(self, track_id: str, link_id: str) -> None:
        with self.lock:
            links, _ = self._find(track_id, link_id)
            self._save(track_id, [item for item in links if item["id"] != link_id])

    def browse(self, track_id: str, link_id: str, path: str = "") -> dict:
        with self.lock:
            _, link = self._find(track_id, link_id)
            folder, normalized = self._path(link, path)
            if not folder.is_dir():
                raise ValidationError("Select a directory.")
            entries = []
            truncated = False
            try:
                with os.scandir(folder) as children:
                    for index, child in enumerate(children):
                        if index >= 2000 or len(entries) >= self.ENTRY_LIMIT:
                            truncated = True
                            break
                        relative = f"{normalized}/{child.name}".lstrip("/")
                        try:
                            target, relative = self._path(link, relative)
                        except ValidationError:
                            continue
                        entries.append(dict(path=relative, name=child.name, directory=target.is_dir(),
                                            size_bytes=target.stat().st_size if target.is_file() else 0))
            except OSError as exc:
                raise ValidationError("Directory is inaccessible.") from exc
            return dict(path=normalized, entries=sorted(entries, key=lambda e: (not e["directory"], e["name"])),
                        truncated=truncated)

    def preview(self, track_id: str, link_id: str, path: str) -> dict:
        with self.lock:
            _, link = self._find(track_id, link_id)
            target, normalized, content = self._read(link, path)
            kind, text = "binary", None
            if target.suffix.lower() in self.IMAGES:
                kind = "image"
            elif target.suffix.lower() == ".pdf":
                kind = "pdf"
            elif len(content) <= self.TEXT_LIMIT and b"\x00" not in content:
                try:
                    text = content.decode("utf-8")
                    kind = "text"
                except UnicodeDecodeError:
                    pass
            return dict(path=normalized, sha256=hashlib.sha256(content).hexdigest(), size_bytes=len(content),
                        kind=kind, content=text, editable=kind == "text")

    def media(self, track_id: str, link_id: str, path: str, expected_sha256: str | None = None) -> tuple[bytes, str]:
        with self.lock:
            _, link = self._find(track_id, link_id)
            target, _, content = self._read(link, path)
            if expected_sha256 and hashlib.sha256(content).hexdigest() != expected_sha256:
                raise ConflictError("Media changed since preview. Reopen the artifact to inspect the current file.")
            media_type = self.IMAGES.get(target.suffix.lower())
            if target.suffix.lower() == ".pdf":
                if b"%PDF-" not in content[:1024]:
                    raise ValidationError("This file does not contain a PDF header.")
                media_type = "application/pdf"
            if not media_type:
                raise ValidationError("Media supports raster images and PDFs only.")
            return content, media_type

    def edit(self, track_id: str, link_id: str, path: str, content: str, expected_sha256: str) -> dict:
        with self.lock:
            preview = self.preview(track_id, link_id, path)
            if not preview["editable"] or "\x00" in content or len(content.encode("utf-8")) > self.TEXT_LIMIT:
                raise ValidationError("Manual edits require UTF-8 text up to 256 KiB.")
            if preview["sha256"] != expected_sha256:
                raise ConflictError("File changed on disk. Reload and reconcile your draft before saving.")
            _, link = self._find(track_id, link_id)
            self._path(link, path)
            try:
                _, _, latest = self._read(link, preview["path"])
                if hashlib.sha256(latest).hexdigest() != expected_sha256:
                    raise ConflictError("File changed on disk. Reload and reconcile your draft before saving.")
                self.storage.write_bytes_atomic(Path(link["root"]), preview["path"], content.encode("utf-8"),
                                                max_bytes=self.TEXT_LIMIT)
            except (OSError, StorageError) as exc:
                raise ValidationError("Unable to save repository file.") from exc
            return self.preview(track_id, link_id, path)

    def artifact(self, track_id: str, link_id: str, request: ArtifactLinkRequest) -> RepositoryLink:
        with self.lock:
            links, link = self._find(track_id, link_id)
            if request.experiment_id not in {item.id for item in self.tracks.get(track_id).experiments}:
                raise ValidationError("Choose an existing experiment ID in this track.")
            preview = self.preview(track_id, link_id, request.path)
            if preview["sha256"] != request.expected_sha256:
                raise ConflictError("Artifact changed since preview; reload before linking.")
            if len(link["artifacts"]) >= 200:
                raise ValidationError("A repository supports at most 200 artifact receipts.")
            link["artifacts"].append(dict(
                id=str(uuid.uuid4()), path=preview["path"], experiment_id=request.experiment_id,
                sha256=preview["sha256"], size_bytes=preview["size_bytes"],
                linked_at=datetime.now(timezone.utc).isoformat(), observed_revision=self._revision(link),
                producing_revision=request.producing_revision, note=request.note,
                provenance="User-linked existing file; producing revision is user-supplied and unverified."
                if request.producing_revision else "User-linked existing file; producing revision is unknown.",
                status="unchanged",
            ))
            self._save(track_id, links)
            return RepositoryLink.model_validate({k: v for k, v in link.items() if k != "identity"})

    def refresh(self, track_id: str, link_id: str) -> RepositoryLink:
        with self.lock:
            links, link = self._find(track_id, link_id)
            self._root(link)
            link["current_revision"] = self._revision(link)
            for artifact in link["artifacts"]:
                try:
                    digest = self.preview(track_id, link_id, artifact["path"])["sha256"]
                    artifact["status"] = "unchanged" if digest == artifact["sha256"] else "changed"
                except (ValidationError, ConflictError):
                    artifact["status"] = "unavailable"
            self._save(track_id, links)
            return RepositoryLink.model_validate({k: v for k, v in link.items() if k != "identity"})

    def promote(self, track_id: str, link_id: str, artifact_id: str, request: ArtifactPromotionRequest):
        with self.lock:
            _, link = self._find(track_id, link_id)
            artifact = next((item for item in link["artifacts"] if item["id"] == artifact_id), None)
            if artifact is None:
                raise NotFoundError("Artifact receipt was not found.")
            if artifact["experiment_id"] not in {item.id for item in self.tracks.get(track_id).experiments}:
                raise ValidationError("This receipt's experiment no longer exists in the track.")
            current = self.tracks.read_section(track_id, "results")
            if current.sha256 != request.expected_results_sha256:
                raise ConflictError("Results changed. Reload the Results baseline before promoting this receipt.")
            if self.preview(track_id, link_id, artifact["path"])["sha256"] != artifact["sha256"]:
                raise ConflictError("Artifact changed since linking. Inspect it and create a new receipt before promotion.")
            marker = f"<!-- linked-repository-receipt:{artifact_id} -->"
            if marker in current.content:
                raise ConflictError("This receipt has already been promoted to Results.")
            metadata = {
                "repository": link["root"], "path": artifact["path"], "sha256": artifact["sha256"],
                "linked_at": artifact["linked_at"], "observed_revision_at_link": artifact["observed_revision"],
                "producing_revision_unverified": artifact["producing_revision"],
                "provenance": artifact["provenance"], "receipt_note": artifact["note"],
                "user_observation_unverified": request.observation,
            }
            # JSON indented as a code block preserves literal user text without creating Markdown links or HTML.
            literal = "\n".join("    " + line for line in json.dumps(metadata, ensure_ascii=False, indent=2).splitlines())
            addition = (
                f"\n\n{marker}\n### {artifact['experiment_id']} — linked artifact observation\n\n"
                "Manually promoted existing artifact receipt. No experiment was executed or scientific claim "
                "verified by ScholarWeave. The receipt is not a snapshot of the artifact contents.\n\n"
                f"{literal}\n"
            )
            return self.tracks.write_section(
                track_id, "results", current.content.rstrip() + addition, request.expected_results_sha256,
            )
