from __future__ import annotations

import difflib
import io
import json
import os
import re
import stat
import uuid
import zipfile
from datetime import timezone
from typing import Any
from urllib.parse import quote

from backend.core.errors import ConflictError, NotFoundError, ValidationError
from backend.documents import DocumentService
from backend.persistence.files import SafeStorage, StorageError
from backend.tracks.markdown import SECTIONS, STATUSES, parse_experiments, result_warnings, templates
from backend.tracks.models import ResearchTrack
from backend.tracks.repository import TrackRepository
from backend.tracks.schemas import (
    ImportPreviewResponse, ImportSectionPreview, RevisionResponse, SectionMetadata,
    TrackDetailResponse, TrackSectionResponse,
)
from backend.utils import utcnow
from backend.workspace.layout import WorkspaceLayout
from backend.workspace.service import WorkspaceService


class TrackService:
    def __init__(
        self, workspace: WorkspaceService, storage: SafeStorage,
        documents: DocumentService, repository: TrackRepository,
    ) -> None:
        self.workspace = workspace
        self.storage = storage
        self.documents = documents
        self.repository = repository
        self._limit = min(storage.settings.max_workspace_file_bytes, storage.settings.max_artifact_bytes)
        self._recover_pending_mutations()
        with self.workspace.mutation_lock:
            for record in self.repository.list():
                self._ensure_exploration(record)

    def _ensure_exploration(self, record: ResearchTrack) -> None:
        path = f"{record.folder_path}/exploration"
        absolute = self.storage.resolve_path(self.storage.settings.workspace_dir, path)
        if absolute.relative_to(self.storage.settings.workspace_dir.resolve()).as_posix() != path:
            raise ValidationError("Track exploration must not follow symbolic links.")
        self.storage.ensure_workspace_folder(path)

    def _exploration_sections(self, record: ResearchTrack) -> list[str]:
        self._ensure_exploration(record)
        folder = self.storage.resolve_path(self.storage.settings.workspace_dir, f"{record.folder_path}/exploration")
        return [self._section(f"exploration/{path.stem}") for path in sorted(folder.iterdir())
                if path.is_file() and path.suffix == ".md"]

    def _journal_path(self, track_id: str) -> str:
        return f"track_history/{self._track(track_id).id}/transaction.json"

    def _write_journal(self, track_id: str, journal: dict[str, Any]) -> None:
        self.storage.write_bytes_atomic(
            self.storage.settings.data_dir, self._journal_path(track_id),
            json.dumps(journal, ensure_ascii=False).encode("utf-8"),
        )

    def _recover_pending_mutations(self) -> None:
        with self.workspace.mutation_lock:
            for record in self.repository.list():
                path = self._journal_path(record.id)
                try:
                    journal = json.loads(self.storage.read_text(self.storage.settings.data_dir, path))
                except FileNotFoundError:
                    continue
                if journal["state"] != "committed":
                    for section, content in journal["contents"].items():
                        section_path = self._path(record, section)
                        if content is None:
                            if self.storage.resolve_path(self.storage.settings.workspace_dir, section_path).exists():
                                self.storage.delete_workspace_file(section_path)
                        else:
                            self.storage.write_workspace_file(section_path, content)
                        self.workspace.index_path(section_path)
                    self.repository.set_papers(record.id, journal["paper_ids"])
                self.storage.delete_stored_file(self.storage.settings.data_dir, path)

    def _track(self, track_id: str) -> ResearchTrack:
        record = self.repository.get(track_id)
        if record is None:
            raise NotFoundError("Research track was not found.")
        return record

    def _section(self, section: str) -> str:
        if section not in SECTIONS and not re.fullmatch(r"exploration/[a-z0-9][a-z0-9_-]{0,79}", section):
            raise ValidationError("Unknown research track section.")
        if section.startswith("exploration/") and re.fullmatch(
            r"con|prn|aux|nul|com[1-9]|lpt[1-9]", section.removeprefix("exploration/"),
        ):
            raise ValidationError("Reserved device names cannot be exploration filenames.")
        return section

    def _path(self, record: ResearchTrack, section: str) -> str:
        path = f"{record.folder_path}/{self._section(section)}.md"
        absolute = self.storage.resolve_path(self.storage.settings.workspace_dir, path)
        canonical = absolute.relative_to(self.storage.settings.workspace_dir.resolve()).as_posix()
        if canonical != path:
            raise ValidationError("Track sections must not follow symbolic links.")
        return path

    def _validate_content(self, section: str, content: str) -> None:
        self._section(section)
        if not isinstance(content, str) or "\x00" in content:
            raise ValidationError("Track sections must be UTF-8 Markdown text without null bytes.")
        if len(content.encode("utf-8")) > self._limit:
            raise ValidationError("Track section exceeds the maximum file size.")
        if section == "experiments":
            parse_experiments(content)

    def list(self) -> list[TrackDetailResponse]:
        return [self.get(record.id) for record in self.repository.list()]

    def create(self, title: str) -> TrackDetailResponse:
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 300:
            raise ValidationError("Research track title must contain 1 to 300 characters.")
        title = title.strip()
        if any(ord(char) < 32 for char in title):
            raise ValidationError("Research track title must be a single line.")
        identifier = str(uuid.uuid4())
        folder = WorkspaceLayout.track_folder(identifier, title)
        with self.workspace.mutation_lock:
            try:
                for section, content in templates(title).items():
                    self._validate_content(section, content)
                    self.storage.write_workspace_file(f"{folder}/{section}.md", content)
                self.storage.ensure_workspace_folder(f"{folder}/exploration")
                self.repository.create(identifier, title, folder)
            except Exception:
                self.storage.delete_stored_tree(self.storage.settings.workspace_dir, folder)
                raise
            for section in SECTIONS:
                self.workspace.index_path(f"{folder}/{section}.md")
            return self.get(identifier)

    def get(self, track_id: str) -> TrackDetailResponse:
        with self.workspace.mutation_lock:
            record = self._track(track_id)
            sections = []
            exploration = []
            bodies: dict[str, str] = {}
            warnings: list[str] = []
            for section in (*SECTIONS, *self._exploration_sections(record)):
                try:
                    snapshot = self.read_section(track_id, section)
                except (NotFoundError, ValidationError, StorageError, UnicodeError) as error:
                    warnings.append(f"{section}.md could not be read: {error}")
                    continue
                bodies[section] = snapshot.content
                metadata = SectionMetadata(**snapshot.model_dump(exclude={"content"}))
                (exploration if section.startswith("exploration/") else sections).append(metadata)
                self.workspace.index_path(snapshot.path)
            experiments = []
            if "experiments" in bodies:
                try:
                    experiments, messages = parse_experiments(bodies["experiments"])
                    warnings.extend(messages)
                except ValidationError as error:
                    warnings.append(f"experiments.md: {error}")
            warnings.extend(result_warnings(bodies.get("results", ""), experiments))
            papers = self.repository.paper_ids(track_id)
            for paper in papers:
                if self.documents.get_document(paper) is None:
                    warnings.append(f"Linked paper {paper} no longer exists.")
            modified = [snapshot.modified_at for snapshot in [*sections, *exploration]]
            updated_at = record.updated_at.replace(tzinfo=timezone.utc) if record.updated_at.tzinfo is None else record.updated_at
            return TrackDetailResponse(
                id=record.id, title=record.title, folder_path=record.folder_path,
                created_at=(
                    record.created_at.replace(tzinfo=timezone.utc)
                    if record.created_at.tzinfo is None else record.created_at
                ), updated_at=max([updated_at, *modified]),
                paper_ids=papers, conversation_ids=self.repository.conversation_ids(track_id),
                sections=sections, exploration=exploration, experiments=experiments,
                status_counts={status: sum(row.status == status for row in experiments) for status in STATUSES},
                warnings=warnings,
            )

    def read_section(self, track_id: str, section: str) -> TrackSectionResponse:
        with self.workspace.mutation_lock:
            path = self._path(self._track(track_id), section)
            try:
                document = self.workspace.read_file(path)
            except FileNotFoundError as error:
                raise NotFoundError(f"{section}.md is missing.") from error
            info = self.storage.workspace_file_info(path)
            return TrackSectionResponse(
                section=section, path=path, content=document.content,
                sha256=document.sha256, modified_at=info.modified_at,
            )

    def _guard(self, current: TrackSectionResponse, expected_sha256: str | None) -> None:
        if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ValidationError("An explicit expected_sha256 revision is required.")
        if current.sha256 != expected_sha256:
            raise ConflictError("The track section changed; reload and compare before saving.")

    def _private_path(self, track_id: str, kind: str, identifier: str) -> str:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", identifier):
            raise ValidationError("Invalid track revision or preview identifier.")
        return f"track_history/{self._track(track_id).id}/{kind}/{identifier}.json"

    def _save_history(self, track_id: str, current: TrackSectionResponse) -> None:
        revision = str(uuid.uuid4())
        path = self._private_path(track_id, current.section, revision)
        saved = {
            "revision": revision, "sha256": current.sha256, "created_at": utcnow().isoformat(),
            "section": current.section, "path": current.path, "content": current.content,
            "modified_at": current.modified_at.isoformat(),
        }
        self.storage.write_bytes_atomic(
            self.storage.settings.data_dir, path, json.dumps(saved, ensure_ascii=False).encode("utf-8"),
        )

    def write_section(
        self, track_id: str, section: str, content: str, expected_sha256: str | None,
    ) -> TrackSectionResponse:
        with self.workspace.mutation_lock:
            self._validate_content(section, content)
            self._track(track_id)
            try:
                current = self.read_section(track_id, section)
            except NotFoundError as error:
                if not section.startswith("exploration/") or expected_sha256 is not None:
                    raise ConflictError("The track section was deleted; restore it before saving.") from error
                current = None
            if current is not None:
                self._guard(current, expected_sha256)
            if current is not None and current.content == content:
                return current
            self._apply_sections(track_id, {section: content}, {section: current})
            return self.read_section(track_id, section)

    def _apply_sections(
        self, track_id: str, contents: dict[str, str],
        snapshots: dict[str, TrackSectionResponse | None], paper_ids: list[str] | None = None,
    ) -> None:
        old_papers = self.repository.paper_ids(track_id)
        for section, content in contents.items():
            self._validate_content(section, content)
            self._check_snapshot(track_id, section, snapshots[section])
        for snapshot in snapshots.values():
            if snapshot is not None:
                self._save_history(track_id, snapshot)
        journal = {
            "state": "prepared", "contents": {section: snapshot.content if snapshot else None for section, snapshot in snapshots.items()},
            "paper_ids": old_papers,
        }
        self._write_journal(track_id, journal)
        changed: list[str] = []
        try:
            for section, content in contents.items():
                # Recheck immediately before each atomic file replacement, including external edits.
                self._check_snapshot(track_id, section, snapshots[section])
                self.storage.write_workspace_file(self._path(self._track(track_id), section), content)
                changed.append(section)
            if paper_ids is not None:
                self.repository.set_papers(track_id, paper_ids)
            self.repository.touch(track_id)
            for section in changed:
                self.workspace.index_path(self._path(self._track(track_id), section))
            self._write_journal(track_id, {**journal, "state": "committed"})
        except Exception as error:
            try:
                for section in changed:
                    path = self._path(self._track(track_id), section)
                    if snapshots[section] is None:
                        self.storage.delete_workspace_file(path)
                    else:
                        self.storage.write_workspace_file(path, snapshots[section].content)
                    self.workspace.index_path(path)
                if paper_ids is not None:
                    self.repository.set_papers(track_id, old_papers)
                self.storage.delete_stored_file(self.storage.settings.data_dir, self._journal_path(track_id))
            except Exception as rollback_error:
                raise ConflictError(
                    "Track update failed and rollback needs recovery from section history; no success was recorded."
                ) from rollback_error
            raise error
        try:
            self.storage.delete_stored_file(self.storage.settings.data_dir, self._journal_path(track_id))
        except OSError:
            # A committed journal can be safely collected at the next startup.
            pass

    def _check_snapshot(self, track_id: str, section: str, snapshot: TrackSectionResponse | None) -> None:
        if snapshot is not None:
            self._guard(self.read_section(track_id, section), snapshot.sha256)
        elif self.storage.resolve_path(
            self.storage.settings.workspace_dir, self._path(self._track(track_id), section),
        ).exists():
            raise ConflictError("The exploration file already exists; read its revision before saving.")

    def history(self, track_id: str, section: str) -> list[RevisionResponse]:
        self._section(section)
        folder = self.storage.resolve_path(
            self.storage.settings.data_dir, f"track_history/{self._track(track_id).id}/{section}",
        )
        revisions = []
        if folder.exists():
            for path in folder.glob("*.json"):
                saved = self._read_private(track_id, section, path.stem)
                revisions.append(RevisionResponse(
                    revision=saved["revision"], sha256=saved["sha256"], created_at=saved["created_at"],
                ))
        return sorted(revisions, key=lambda revision: revision.created_at, reverse=True)

    def _read_private(self, track_id: str, kind: str, identifier: str) -> dict[str, Any]:
        path = self._private_path(track_id, kind, identifier)
        try:
            return json.loads(self.storage.read_text(self.storage.settings.data_dir, path))
        except FileNotFoundError as error:
            raise NotFoundError("Track revision or import preview was not found.") from error

    def read_history(self, track_id: str, section: str, revision: str) -> TrackSectionResponse:
        self._section(section)
        saved = self._read_private(track_id, section, revision)
        return TrackSectionResponse(**{key: saved[key] for key in (
            "section", "path", "content", "sha256", "modified_at",
        )})

    def paper_links(self, document_id: str, *, from_path: str) -> str:
        document = self.documents.get_document(document_id)
        if document is None:
            raise NotFoundError("Paper was not found.")
        canonical = self.workspace.ensure_paper_folder(document_id, document.title)
        parent = self.storage.resolve_path(self.storage.settings.workspace_dir, from_path).parent
        folder = self.storage.resolve_path(self.storage.settings.workspace_dir, canonical["folder"])
        relative = os.path.relpath(folder, parent).replace("\\", "/")
        links = f"[summary]({quote(relative + '/summary.md')}) · [notes]({quote(relative + '/notes.md')})"
        source = next((artifact for artifact in self.documents.get_document_artifacts(document_id)
                       if artifact.kind == "source_pdf"), None)
        if source is not None:
            area = source.metadata_json.get("storage_area", "documents")
            base = self.storage.settings.workspace_dir if area == "workspace" else self.storage.settings.documents_dir
            pdf_path = os.path.relpath(self.storage.resolve_path(base, source.relative_path), parent).replace("\\", "/")
            links += f" · [PDF]({quote(pdf_path)})"
        return links

    def append_literature_review(
        self, track_id: str, content: str, *, marker: str, paper_ids: list[str],
    ) -> TrackSectionResponse:
        with self.workspace.mutation_lock:
            record = self._track(track_id)
            if not marker or marker not in content:
                raise ValidationError("A saved review needs its publication marker.")
            for document_id in paper_ids:
                if self.documents.get_document(document_id) is None:
                    raise NotFoundError("A reviewed paper no longer exists.")
            current = self.read_section(track_id, "literature")
            previous_papers = self.repository.paper_ids(track_id)
            papers = sorted(set(previous_papers) | set(paper_ids))
            if marker in current.content:
                body = current.content
                if papers == sorted(previous_papers):
                    return current
            else:
                prefix = "# Literature" if current.content == templates(record.title)["literature"] else current.content.rstrip()
                body = f"{prefix}\n\n{content.rstrip()}\n"
            self._apply_sections(
                track_id, {"literature": body}, {"literature": current}, paper_ids=papers,
            )
            return self.read_section(track_id, "literature")

    def remove_literature_review(self, track_id: str, content: str, *, remove_paper_ids: list[str]) -> bool:
        with self.workspace.mutation_lock:
            current = self.read_section(track_id, "literature")
            block = content.rstrip() + "\n"
            if block not in current.content:
                return False
            if current.content.count(block) != 1:
                raise ConflictError("The published review occurs more than once; cleanup cannot safely identify it.")
            body = current.content.replace(block, "", 1)
            papers = [
                identifier for identifier in self.repository.paper_ids(track_id)
                if identifier not in remove_paper_ids or identifier in body
            ]
            self._apply_sections(track_id, {"literature": body}, {"literature": current}, paper_ids=papers)
            return True

    def add_paper(self, track_id: str, document_id: str) -> TrackDetailResponse:
        with self.workspace.mutation_lock:
            self._track(track_id)
            document = self.documents.get_document(document_id)
            if document is None:
                raise NotFoundError("Paper was not found.")
            papers = self.repository.paper_ids(track_id)
            if document_id not in papers:
                literature = self.read_section(track_id, "literature")
                if re.search(rf"(?m)^## .+ \({re.escape(document_id)}\)$", literature.content):
                    self.repository.set_papers(track_id, sorted([*papers, document_id]))
                    self.repository.touch(track_id)
                    return self.get(track_id)
                safe_title = document.title.replace("|", r"\|").replace("\n", " ")
                links = self.paper_links(document_id, from_path=literature.path)
                lines = literature.content.splitlines()
                header = next((index for index, line in enumerate(lines) if line.strip().startswith("| Paper |")), None)
                if header is not None:
                    end = header + 2
                    while end < len(lines) and lines[end].strip().startswith("|"):
                        end += 1
                    lines.insert(end, f"| {safe_title} | | | | {links} |")
                lines.extend(["", f"## {safe_title} ({document_id})", "", f"Canonical sources: {links}", "",
                              "### Contribution and methods", "", "### Evidence and limitations", "",
                              "### Relation to the research problem", ""])
                self._apply_sections(track_id, {"literature": "\n".join(lines)}, {"literature": literature},
                                     paper_ids=sorted([*papers, document_id]))
            return self.get(track_id)

    def reader_notes(self, track_id: str, *, query: str | None = None, limit: int = 25, offset: int = 0) -> dict:
        self._track(track_id)
        return self.workspace.reader_notes(
            query=query, paper_ids=self.repository.paper_ids(track_id), limit=limit, offset=offset,
        )

    def remove_paper(self, track_id: str, document_id: str) -> TrackDetailResponse:
        with self.workspace.mutation_lock:
            self._track(track_id)
            self.repository.set_papers(track_id, [
                paper for paper in self.repository.paper_ids(track_id) if paper != document_id
            ])
            self.repository.touch(track_id)
            return self.get(track_id)

    def link_conversation(self, track_id: str, conversation_id: str) -> TrackDetailResponse:
        with self.workspace.mutation_lock:
            self._track(track_id)
            if not self.repository.conversation_exists(conversation_id):
                raise NotFoundError("Conversation was not found.")
            self.repository.link_conversation(track_id, conversation_id)
            self.repository.touch(track_id)
            return self.get(track_id)

    def unlink_conversation(self, conversation_id: str) -> None:
        with self.workspace.mutation_lock:
            self.repository.unlink_conversation(conversation_id)

    def for_conversation(self, conversation_id: str) -> TrackDetailResponse | None:
        track_id = self.conversation_track_id(conversation_id)
        return self.get(track_id) if track_id else None

    def conversation_track_id(self, conversation_id: str) -> str | None:
        return self.repository.for_conversation(conversation_id)

    def preview_import(
        self, track_id: str, section: str, content: str,
    ) -> ImportPreviewResponse:
        return self._preview(track_id, {section: content})

    def _preview(
        self, track_id: str, contents: dict[str, str], paper_ids: list[str] | None = None,
    ) -> ImportPreviewResponse:
        with self.workspace.mutation_lock:
            self._track(track_id)
            previews = []
            warnings = []
            for section, content in contents.items():
                self._validate_content(section, content)
                try:
                    current = self.read_section(track_id, section)
                except NotFoundError:
                    if not section.startswith("exploration/"):
                        raise
                    current = None
                previews.append(ImportSectionPreview(
                    section=section, current_content=current.content if current else "", incoming_content=content,
                    expected_sha256=current.sha256 if current else None,
                    diff="\n".join(difflib.unified_diff(
                        current.content.splitlines() if current else [], content.splitlines(),
                        fromfile=f"current/{section}.md", tofile=f"incoming/{section}.md", lineterm="",
                    )),
                ))
            experiments, messages = parse_experiments(contents.get(
                "experiments", self.read_section(track_id, "experiments").content,
            ))
            warnings.extend(messages)
            warnings.extend(result_warnings(contents.get(
                "results", self.read_section(track_id, "results").content,
            ), experiments))
            incoming_papers = paper_ids if paper_ids is not None else self.repository.paper_ids(track_id)
            for paper in incoming_papers:
                if self.documents.get_document(paper) is None:
                    warnings.append(f"Imported paper {paper} is not available locally; acquire it before applying.")
            identifier = str(uuid.uuid4())
            result = ImportPreviewResponse(
                preview_id=identifier, sections=previews, paper_ids=incoming_papers, warnings=warnings,
            )
            self.storage.write_json(
                self.storage.settings.data_dir, self._private_path(track_id, "previews", identifier),
                {"track_id": track_id, "contents": contents,
                 "revisions": {item.section: item.expected_sha256 for item in previews},
                 "paper_ids": paper_ids, "current_paper_ids": self.repository.paper_ids(track_id)},
            )
            return result

    def preview_zip(self, track_id: str, data: bytes) -> ImportPreviewResponse:
        self._track(track_id)
        if len(data) > self.storage.settings.max_upload_bytes:
            raise ValidationError("Track ZIP exceeds the maximum size.")
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                names = [entry.filename for entry in entries]
                required = {f"{section}.md" for section in SECTIONS} | {"manifest.json"}
                if not required <= set(names) or len(entries) > 105 or any(
                    name not in required and not re.fullmatch(r"exploration/[a-z0-9][a-z0-9_-]{0,79}\.md", name)
                    for name in names
                ):
                    raise ValidationError("ZIP requires four canonical sections and manifest.json; only exploration Markdown files may be added.")
                if len(set(name.casefold() for name in names)) != len(names):
                    raise ValidationError("Duplicate ZIP entries are not allowed.")
                files = {}
                total = 0
                for entry in entries:
                    mode = entry.external_attr >> 16
                    if entry.flag_bits & 1 or stat.S_ISLNK(mode) or entry.is_dir():
                        raise ValidationError("Encrypted files, symlinks, and directories are not allowed in track ZIPs.")
                    total += entry.file_size
                    if entry.file_size > self._limit or total > self.storage.settings.max_upload_bytes or (
                        entry.file_size > max(1, entry.compress_size) * 200
                    ):
                        raise ValidationError("ZIP expansion exceeds safe limits.")
                    raw = archive.read(entry)
                    if len(raw) != entry.file_size:
                        raise ValidationError("ZIP entry size is inconsistent.")
                    files[entry.filename] = raw.decode("utf-8-sig")
                manifest = json.loads(files.pop("manifest.json"))
                old_keys = {"format_version", "track_id", "title", "paper_ids", "sections"}
                if not isinstance(manifest, dict) or set(manifest) not in (old_keys, old_keys | {"exploration"}):
                    raise ValidationError("Invalid track manifest.")
                if manifest["format_version"] not in (1, 2) or manifest["track_id"] != track_id:
                    raise ValidationError("Track manifest identity or version does not match this track.")
                if manifest["sections"] != [f"{section}.md" for section in SECTIONS]:
                    raise ValidationError("Track manifest sections do not match canonical files.")
                exploration = sorted(name for name in files if name.startswith("exploration/"))
                if manifest["format_version"] == 1:
                    if exploration or "exploration" in manifest:
                        raise ValidationError("Legacy track ZIPs cannot contain exploration files.")
                elif manifest.get("exploration") != exploration:
                    raise ValidationError("Track manifest exploration files do not match the archive.")
                papers = manifest["paper_ids"]
                if not isinstance(papers, list) or len(papers) > 1000 or any(
                    not isinstance(paper, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", paper) for paper in papers
                ) or len(set(papers)) != len(papers):
                    raise ValidationError("Invalid paper IDs in track manifest.")
                return self._preview(track_id, {name[:-3]: content for name, content in files.items()}, papers)
        except ValidationError:
            raise
        except (zipfile.BadZipFile, UnicodeError, json.JSONDecodeError, RuntimeError, NotImplementedError) as error:
            raise ValidationError("Track ZIP must be a valid, unencrypted UTF-8 archive.") from error

    def _merge(self, section: str, current: str, incoming: str) -> str:
        if current == incoming or not incoming.strip():
            return current
        marker = "\n\n<!-- Imported fragment; review against existing evidence. -->\n\n"
        if section != "experiments":
            return current.rstrip() + marker + incoming
        existing, _ = parse_experiments(current)
        imported, _ = parse_experiments(incoming)
        collisions = {row.id for row in existing} & {row.id for row in imported}
        if collisions:
            raise ValidationError("Experiment ID collisions require a manually reviewed merge: " + ", ".join(sorted(collisions)))
        current_lines, incoming_lines = current.splitlines(), incoming.splitlines()

        def table_range(lines: list[str]) -> tuple[int, int] | None:
            start = next((i for i, line in enumerate(lines) if re.sub(r"\s", "", line).strip("|") ==
                          "ID|Experiment|Status|Successcriterion|Evidence"), None)
            if start is None:
                return None
            end = start + 2
            while end < len(lines) and "|" in lines[end] and lines[end].strip():
                end += 1
            return start, end

        incoming_range, current_range = table_range(incoming_lines), table_range(current_lines)
        if incoming_range is not None and current_range is not None:
            start, end = incoming_range
            current_lines[current_range[1]:current_range[1]] = incoming_lines[start + 2:end]
            del incoming_lines[start:end]
        return "\n".join(current_lines).rstrip() + marker + "\n".join(incoming_lines)

    def apply_import(
        self, track_id: str, preview_id: str, mode: str, expected_revisions: dict[str, str | None],
        confirmed_replace: bool = False, merged_content: dict[str, str] | None = None,
    ) -> TrackDetailResponse:
        with self.workspace.mutation_lock:
            saved = self._read_private(track_id, "previews", preview_id)
            if saved.get("applied"):
                raise NotFoundError("This track import preview has already been applied.")
            if mode not in {"merge", "replace"}:
                raise ValidationError("Import mode must be merge or replace.")
            if mode == "replace" and not confirmed_replace:
                raise ValidationError("Replacing sections requires explicit confirmation.")
            if mode == "replace" and merged_content:
                raise ValidationError("Manual merged content is only allowed in merge mode.")
            revisions = saved["revisions"]
            if expected_revisions != revisions:
                raise ConflictError("Import revisions do not match the preview; create a fresh preview.")
            if merged_content and not set(merged_content) <= set(revisions):
                raise ValidationError("Manual merge contains a section not present in the preview.")
            snapshots = {}
            for section, revision in revisions.items():
                if revision is None and section.startswith("exploration/"):
                    snapshots[section] = None
                else:
                    snapshots[section] = self.read_section(track_id, section)
            for section, current in snapshots.items():
                if current is not None:
                    self._guard(current, revisions[section])
                self._check_snapshot(track_id, section, current)
            contents = {}
            for section, incoming in saved["contents"].items():
                if mode == "replace":
                    contents[section] = incoming
                elif merged_content is not None and section in merged_content:
                    contents[section] = merged_content[section]
                else:
                    contents[section] = self._merge(section, snapshots[section].content if snapshots[section] else "", incoming)
                self._validate_content(section, contents[section])
            papers = saved["paper_ids"]
            if papers is not None:
                if self.repository.paper_ids(track_id) != saved["current_paper_ids"]:
                    raise ConflictError("Track paper associations changed; create a fresh import preview.")
                if mode == "merge":
                    papers = sorted(set(papers) | set(self.repository.paper_ids(track_id)))
                for paper in papers:
                    if self.documents.get_document(paper) is None:
                        raise ValidationError(f"Imported paper {paper} is not available locally.")
            self._apply_sections(track_id, contents, snapshots, paper_ids=papers)
            preview_path = self._private_path(track_id, "previews", preview_id)
            self.storage.write_bytes_atomic(
                self.storage.settings.data_dir, preview_path, b'{"applied": true}',
            )
            return self.get(track_id)

    def export(self, track_id: str, section: str | None = None) -> tuple[bytes, str, str]:
        with self.workspace.mutation_lock:
            record = self._track(track_id)
            if section is not None:
                content = self.read_section(track_id, section).content.encode("utf-8")
                return content, "text/markdown; charset=utf-8", f"{section.rsplit('/', 1)[-1]}.md"
            output = io.BytesIO()
            # Stored entries guarantee that our own exports pass the expansion-ratio guard.
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
                exploration = self._exploration_sections(record)
                if len(exploration) > 100:
                    raise ValidationError("Export supports at most 100 exploration files per track.")
                for name in (*SECTIONS, *exploration):
                    archive.writestr(f"{name}.md", self.read_section(track_id, name).content)
                archive.writestr("manifest.json", json.dumps({
                    "format_version": 2, "track_id": track_id, "title": record.title,
                    "paper_ids": self.repository.paper_ids(track_id),
                    "sections": [f"{name}.md" for name in SECTIONS],
                    "exploration": [f"{name}.md" for name in exploration],
                }, ensure_ascii=False))
            if len(output.getvalue()) > self.storage.settings.max_upload_bytes:
                raise ValidationError("Track ZIP exceeds the maximum round-trip import size.")
            return output.getvalue(), "application/zip", f"research-track-{track_id}.zip"
