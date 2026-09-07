from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backend.core.config import Settings
from backend.persistence.database import SCHEMA_GENERATION
from backend.persistence.files import SafeStorage, StorageError
from backend.utils import dumps_json, loads_json
from backend.workspace.layout import WorkspaceLayout
from backend.workspace.models import WorkspaceEntry
from backend.workspace.repository import WorkspaceUpgradeRepository


class WorkspaceUpgrade:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.storage = SafeStorage(settings)
        self.repository = WorkspaceUpgradeRepository(settings.database_path)
        self.backup_dir = settings.data_dir / "workspace-layout-backup"
        if any(self.backup_dir.resolve().is_relative_to(root.resolve())
               for root in (settings.workspace_dir, settings.documents_dir)):
            raise StorageError("The upgrade backup must be outside the research directories.")

    def required(self) -> bool:
        journal = self.backup_dir / "journal.json"
        if journal.is_file() and json.loads(self.storage.read_text(self.backup_dir, "journal.json"))["stage"] not in {"complete", "restored"}:
            return True
        root = self.settings.workspace_dir / "papers"
        if root.is_dir() and any(root.rglob("*")):
            return True
        snapshot = self.repository.snapshot()
        if any(item["kind"] == "note" and item["path"].startswith("notes/")
               for item in snapshot.get("workspace_entries", [])):
            return True
        return any(
            item["kind"] == "source_pdf"
            and loads_json(item["metadata_json"], default={}).get("storage_area") == "documents"
            for item in snapshot.get("artifacts", [])
        )

    def preview(self) -> dict[str, Any]:
        snapshot = self.repository.snapshot()
        generation = next((loads_json(row["value_json"]) for row in snapshot.get("app_settings", [])
                           if row["key"] == "schema_generation"), None)
        if snapshot and generation != SCHEMA_GENERATION:
            raise StorageError("Upgrade the database to the current schema before reorganizing the workspace.")
        existing = {row["path"]: row for row in snapshot.get("workspace_entries", [])}
        registered = {row["document_id"]: row for row in snapshot.get("workspace_papers", [])}
        legacy_names = self._legacy_metadata("papers.json")
        papers = dict(registered)
        for document in snapshot.get("documents", []):
            identifier = document["id"]
            name = next((row["paper_name"] for row in existing.values()
                         if row.get("paper_id") == identifier and row.get("paper_name")), None)
            name = name or legacy_names.get(identifier) or document["title"]
            papers.setdefault(identifier, {
                "document_id": identifier, "name": name,
                "folder": WorkspaceLayout.paper_folder(identifier, name),
            })
        legacy_papers = self.settings.workspace_dir / "papers"
        if legacy_papers.exists():
            for directory in legacy_papers.iterdir():
                if directory.is_dir():
                    name = legacy_names.get(directory.name) or "Paper"
                    papers.setdefault(directory.name, {
                        "document_id": directory.name, "name": name,
                        "folder": WorkspaceLayout.paper_folder(directory.name, name),
                    })
        moves = []
        source_paths = {}
        for path in sorted(self.settings.workspace_dir.rglob("*")):
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise StorageError("Workspace upgrades do not follow symbolic links or junctions.")
            if not path.is_file():
                continue
            relative = path.relative_to(self.settings.workspace_dir).as_posix()
            parts = Path(relative).parts
            if len(parts) >= 3 and parts[0] == "papers":
                destination = f"{papers[parts[1]]['folder']}/{'/'.join(parts[2:])}"
            elif parts[0] == "notes":
                entry = existing.get(relative, {})
                if entry.get("note_id") and entry.get("note_name"):
                    destination = WorkspaceLayout.knowledge_note(entry["note_id"], entry["note_name"])
                else:
                    destination = f"knowledge/imported/{'/'.join(parts[1:])}"
            else:
                continue
            moves.append(self._move("workspace", relative, destination))
        for artifact in snapshot.get("artifacts", []):
            metadata = loads_json(artifact["metadata_json"], default={})
            if artifact["kind"] != "source_pdf" or metadata.get("storage_area") != "documents":
                continue
            destination = f"{papers[artifact['document_id']]['folder']}/source.pdf"
            move = self._move("documents", artifact["relative_path"], destination)
            if move["sha256"] != artifact["sha256"]:
                raise StorageError(f"Source PDF differs from its recorded checksum: {artifact['relative_path']}")
            moves.append(move)
            source_paths[artifact["id"]] = destination
        destinations: set[str] = set()
        for move in moves:
            key = move["destination"].casefold()
            if key in destinations:
                raise StorageError(f"Workspace upgrade has a destination collision: {move['destination']}")
            destinations.add(key)
            target = self.storage.resolve_path(self.settings.workspace_dir, move["destination"])
            if target.exists():
                raise StorageError(f"Workspace upgrade destination already exists: {move['destination']}")
        return {
            "layout_version": 1, "stage": "planned", "moves": moves,
            "papers": list(papers.values()), "source_paths": source_paths,
            "entries": list(existing.values()), "legacy_tags": self._legacy_metadata("tags.json"),
            "backup_dir": str(self.backup_dir), "retire_conversations": True,
            "bytes_to_copy": sum(move["size_bytes"] for move in moves),
        }

    def apply(self) -> dict[str, Any]:
        journal_path = self.backup_dir / "journal.json"
        if journal_path.is_file():
            plan = json.loads(self.storage.read_text(self.backup_dir, "journal.json"))
            if plan["stage"] == "complete":
                return plan
            if plan["stage"] in {"restoring", "restored"}:
                raise StorageError("This upgrade was restored; archive its backup before starting a new upgrade.")
        else:
            plan = self.preview()
            if not plan["moves"]:
                return {**plan, "stage": "complete"}
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            plan["backup_hashes"] = {
                "workspace": self.storage.snapshot_tree(self.settings.workspace_dir, self.backup_dir / "workspace"),
                "documents": self.storage.snapshot_tree(self.settings.documents_dir, self.backup_dir / "documents"),
            }
            if self.settings.database_path.exists():
                self.repository.backup(self.backup_dir / "metadata.sqlite3")
                plan["database_backup_hash"] = self.storage.file_hash(self.backup_dir, "metadata.sqlite3")
            self.storage.write_upgrade_journal(self.backup_dir, plan)
        if plan["stage"] == "planned":
            for move in plan["moves"]:
                self.storage.copy_verified(self._base(move["area"]), move["source"],
                                           self.settings.workspace_dir, move["destination"], move["sha256"])
            entries = self._relocated_entries(plan)
            self.repository.relocate(plan["papers"], entries, plan["source_paths"])
            plan["stage"] = "indexed"
            self.storage.write_upgrade_journal(self.backup_dir, plan)
        for move in plan["moves"]:
            if self.storage.file_hash(self.settings.workspace_dir, move["destination"]) != move["sha256"]:
                raise StorageError(f"Moved research file changed before cleanup: {move['destination']}")
            source = self.storage.resolve_path(self._base(move["area"]), move["source"])
            if source.exists():
                if self.storage.file_hash(self._base(move["area"]), move["source"]) != move["sha256"]:
                    raise StorageError(f"Original research file changed before cleanup: {move['source']}")
                self.storage.delete_stored_file(self._base(move["area"]), move["source"])
        for name in ("tags.json", "papers.json"):
            self.storage.delete_stored_file(self.settings.workspace_dir, f".scholarweave/{name}")
        plan["stage"] = "complete"
        self.storage.write_upgrade_journal(self.backup_dir, plan)
        return plan

    def restore(self) -> None:
        plan = json.loads(self.storage.read_text(self.backup_dir, "journal.json"))
        if plan["stage"] == "restored":
            return
        if plan["stage"] == "restoring":
            raise StorageError("A restore was interrupted. Preserve both directory copies and complete the restore offline.")
        if not (self.backup_dir / "metadata.sqlite3").is_file():
            raise StorageError("The original database backup is missing; refusing a partial restore.")
        if self.storage.file_hash(self.backup_dir, "metadata.sqlite3") != plan["database_backup_hash"]:
            raise StorageError("The original database backup failed checksum validation.")
        for area, hashes in plan["backup_hashes"].items():
            for path, expected in hashes.items():
                if self.storage.file_hash(self.backup_dir / area, path) != expected:
                    raise StorageError(f"Research backup failed checksum validation: {area}/{path}")
        for root in (self.settings.workspace_dir, self.settings.documents_dir):
            if root.with_name(f"{root.name}.before-layout-restore").exists():
                raise StorageError("A previous restored directory already exists; preserve it before retrying.")
        self.repository.backup(self.backup_dir / "before-restore.sqlite3")
        plan["stage"] = "restoring"
        self.storage.write_upgrade_journal(self.backup_dir, plan)
        self.storage.restore_tree(self.backup_dir / "workspace", self.settings.workspace_dir)
        self.storage.restore_tree(self.backup_dir / "documents", self.settings.documents_dir)
        WorkspaceUpgradeRepository(self.backup_dir / "metadata.sqlite3").backup(self.settings.database_path)
        plan["stage"] = "restored"
        self.storage.write_upgrade_journal(self.backup_dir, plan)

    def _relocated_entries(self, plan: dict[str, Any]) -> list[WorkspaceEntry]:
        path_map = {move["source"]: move["destination"] for move in plan["moves"] if move["area"] == "workspace"}
        old_entries = {path_map.get(entry["path"], entry["path"]): entry for entry in plan["entries"]}
        legacy_tags = {path_map.get(path, path): tags for path, tags in plan["legacy_tags"].items()}
        paper_names = {paper["document_id"]: paper["name"] for paper in plan["papers"]}
        entries = []
        for path in self.storage.list_workspace_files():
            if path in path_map:
                continue
            info = self.storage.workspace_file_info(path)
            media_type, content = self.storage.read_workspace_file(path)
            old = old_entries.get(path, {})
            tags = loads_json(old.get("tags_json"), default=legacy_tags.get(path, []))
            paper_id = WorkspaceLayout.paper_id(path)
            entries.append(WorkspaceEntry(
                path=path, name=Path(path).name, display_name=old.get("display_name") or paper_names.get(paper_id),
                kind=WorkspaceLayout.kind(path), media_type=media_type, size_bytes=info.size_bytes,
                modified_at=info.modified_at, tags_json=tags, tags_text="\n" + "\n".join(tag.casefold() for tag in tags) + "\n",
                paper_id=paper_id, paper_name=paper_names.get(paper_id),
                note_id=old.get("note_id"), note_name=old.get("note_name"),
                search_content=content if isinstance(content, str) else dumps_json(content),
            ))
        return entries

    def _move(self, area: str, source: str, destination: str) -> dict[str, Any]:
        path = self.storage.resolve_path(self._base(area), source)
        return {"area": area, "source": source, "destination": destination,
                "sha256": self.storage.file_hash(self._base(area), source), "size_bytes": path.stat().st_size}

    def _base(self, area: str) -> Path:
        if area == "workspace":
            return self.settings.workspace_dir
        if area == "documents":
            return self.settings.documents_dir
        raise StorageError(f"Unknown upgrade storage area: {area}")

    def _legacy_metadata(self, filename: str) -> dict[str, Any]:
        try:
            value = self.storage.read_workspace_file(f".scholarweave/{filename}")[1]
        except FileNotFoundError:
            return {}
        if not isinstance(value, dict):
            raise StorageError(f"Invalid legacy workspace metadata: {filename}")
        return value