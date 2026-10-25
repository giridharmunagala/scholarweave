from __future__ import annotations

import re
import threading
import uuid
from pydantic import ValidationError as SchemaValidationError

from backend.core.errors import ConflictError, NotFoundError, ValidationError
from backend.documents.service import DocumentService
from backend.persistence.files import SafeStorage
from backend.references.formats import export_references, parse_references
from backend.references.schemas import Reference, ReferenceFields, ReferenceLibrary


class ReferenceService:
    """One-process, revision-guarded bibliography; reference IDs and keys are stable."""

    _lock = threading.RLock()
    _path = "references/library.json"

    def __init__(self, storage: SafeStorage, documents: DocumentService) -> None:
        self.storage = storage
        self.documents = documents

    def list(self) -> ReferenceLibrary:
        with self._lock:
            try:
                raw = self.storage.read_stored_bytes(self.storage.settings.data_dir, self._path)
            except FileNotFoundError:
                return ReferenceLibrary()
            try:
                return ReferenceLibrary.model_validate_json(raw)
            except SchemaValidationError as exc:
                raise ValidationError("Stored reference library is corrupt or invalid. Restore references/library.json from a valid backup; no changes were written.") from exc

    def get(self, reference_id: str) -> Reference:
        record = next((item for item in self.list().references if item.id == reference_id), None)
        if record is None:
            raise NotFoundError("Reference was not found.")
        return record

    def _validate(self, reference: Reference, others: list[Reference]) -> None:
        if reference.document_id and self.documents.get_document(reference.document_id) is None:
            raise NotFoundError("Linked paper was not found.")
        ids = {item.id for item in others}
        if reference.id in reference.related_reference_ids or not set(reference.related_reference_ids) <= ids:
            raise ValidationError("Version links must identify other existing references.")
        for other in others:
            if other.citation_key.casefold() == reference.citation_key.casefold():
                raise ConflictError(f"Citation key already belongs to reference {other.id}: {other.citation_key}")
            same_doi = reference.doi and reference.doi == other.doi
            same_arxiv = reference.arxiv_id and re.sub(r"v\d+$", "", reference.arxiv_id) == re.sub(r"v\d+$", "", other.arxiv_id)
            same_paper = reference.document_id and reference.document_id == other.document_id
            if same_doi or same_arxiv or same_paper:
                raise ConflictError(f"Identity already belongs to reference {other.id} ({other.citation_key}); edit or link that record explicitly. No merge performed.")

    def _save(self, library: ReferenceLibrary) -> ReferenceLibrary:
        library.revision += 1
        self.storage.write_bytes_atomic(
            self.storage.settings.data_dir, self._path, library.model_dump_json(indent=2).encode("utf-8"),
        )
        return library

    def _load_revision(self, expected_revision: int) -> ReferenceLibrary:
        library = self.list()
        if expected_revision != library.revision:
            raise ConflictError("References changed. Reload before saving; no changes were written.")
        return library

    def create(self, reference: ReferenceFields, *, expected_revision: int) -> ReferenceLibrary:
        with self._lock:
            library = self._load_revision(expected_revision)
            record = Reference(id=str(uuid.uuid4()), **reference.model_dump())
            self._validate(record, library.references)
            library.references.append(record)
            return self._save(library)

    def update(self, reference_id: str, reference: ReferenceFields, *, expected_revision: int) -> ReferenceLibrary:
        with self._lock:
            library = self._load_revision(expected_revision)
            existing = next((item for item in library.references if item.id == reference_id), None)
            if existing is None:
                raise NotFoundError("Reference was not found.")
            if existing.citation_key != reference.citation_key:
                raise ValidationError("Citation keys are immutable; metadata edits preserve existing citations.")
            record = Reference(id=reference_id, **reference.model_dump())
            self._validate(record, [item for item in library.references if item.id != reference_id])
            library.references = [record if item.id == reference_id else item for item in library.references]
            return self._save(library)

    def import_references(self, content: str, format: str, *, expected_revision: int) -> ReferenceLibrary:
        records = parse_references(content, format)
        with self._lock:
            library = self._load_revision(expected_revision)
            for reference in records:
                record = Reference(id=str(uuid.uuid4()), **reference.model_dump())
                self._validate(record, library.references)
                library.references.append(record)
            return self._save(library)

    def export(self, format: str, reference_ids: list[str] | None = None) -> str:
        library = self.list()
        if reference_ids is not None:
            lookup = {record.id: record for record in library.references}
            missing = set(reference_ids) - lookup.keys()
            if missing:
                raise NotFoundError("Unknown reference IDs: " + ", ".join(sorted(missing)))
            records = [lookup[key] for key in dict.fromkeys(reference_ids)]
        else:
            records = library.references
        return export_references(records, format)

    def bibliography(self) -> dict[str, str]:
        return {
            record.citation_key: ". ".join(part for part in (
                ", ".join(record.authors), f"({record.year})" if record.year else "",
                record.title, record.journal or record.publisher,
                f"https://doi.org/{record.doi}" if record.doi else record.url,
            ) if part)
            for record in self.list().references
        }
