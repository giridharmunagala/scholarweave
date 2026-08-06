from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import and_, delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from backend.core.config import Settings
from backend.core.text import clean_filename
from backend.core.time import utcnow
from backend.documents.errors import DocumentProcessingError
from backend.documents.models import Artifact, Document, DocumentChunk
from backend.persistence.files import SafeStorage, StoredFile


class DocumentRepository:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        storage: SafeStorage,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.storage = storage

    async def create_from_upload(self, upload: Any, title: str | None = None) -> Document:
        document = Document(
            id=str(uuid.uuid4()),
            title=title or Path(upload.filename or "document.pdf").stem,
            source_filename=clean_filename(upload.filename or "document.pdf"),
            content_type=upload.content_type or "application/pdf",
            status="uploaded",
            metadata_json={},
        )
        stored = await self.storage.save_upload(upload, f"{document.id}/source")
        with self.session_factory() as session:
            session.add(document)
            session.flush()
            session.add(
                Artifact(
                    document_id=document.id,
                    owner_type="document",
                    kind="source_pdf",
                    relative_path=stored.relative_path,
                    media_type=document.content_type,
                    size_bytes=stored.size_bytes,
                    sha256=stored.sha256,
                    metadata_json={"storage_area": "documents"},
                )
            )
            session.commit()
            session.refresh(document)
            return document

    def create_artifact(
        self,
        *,
        owner_type: str,
        kind: str,
        relative_path: str,
        media_type: str,
        stored: StoredFile,
        document_id: str | None = None,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        storage_area: str = "artifacts",
    ) -> Artifact:
        now = utcnow()
        metadata_json = {**(metadata or {}), "storage_area": storage_area}
        artifact_id = str(uuid.uuid4())
        with self.session_factory() as session:
            statement = sqlite_insert(Artifact).values(
                id=artifact_id,
                document_id=document_id,
                run_id=run_id,
                owner_type=owner_type,
                kind=kind,
                relative_path=relative_path,
                media_type=media_type,
                size_bytes=stored.size_bytes,
                sha256=stored.sha256,
                metadata_json=metadata_json,
                created_at=now,
                updated_at=now,
            )
            statement = statement.on_conflict_do_update(
                index_elements=[Artifact.relative_path],
                set_={
                    "kind": kind,
                    "media_type": media_type,
                    "size_bytes": stored.size_bytes,
                    "sha256": stored.sha256,
                    "metadata_json": metadata_json,
                    "updated_at": now,
                },
                where=and_(
                    Artifact.owner_type == owner_type,
                    Artifact.document_id == document_id,
                    Artifact.run_id == run_id,
                ),
            )
            result = session.execute(statement)
            if result.rowcount == 0:
                session.rollback()
                raise ValueError(
                    f"Artifact path is already owned by another resource: {relative_path}"
                )
            session.commit()
            artifact = session.scalar(
                select(Artifact).where(Artifact.relative_path == relative_path)
            )
            assert artifact is not None
            return artifact

    def list(self) -> list[Document]:
        with self.session_factory() as session:
            return list(
                session.scalars(select(Document).order_by(Document.created_at.desc()))
            )

    def get(self, document_id: str) -> Document | None:
        with self.session_factory() as session:
            return session.get(Document, document_id)

    def get_details(
        self,
        document_id: str,
    ) -> tuple[Document, list[Artifact], list[DocumentChunk]] | None:
        with self.session_factory() as session:
            document = session.get(Document, document_id)
            if not document:
                return None
            artifacts = list(
                session.scalars(
                    select(Artifact)
                    .where(Artifact.document_id == document_id)
                    .order_by(Artifact.created_at.asc())
                )
            )
            chunks = list(
                session.scalars(
                    select(DocumentChunk)
                    .where(DocumentChunk.document_id == document_id)
                    .order_by(DocumentChunk.chunk_index.asc())
                )
            )
            return document, artifacts, chunks

    def get_artifact(self, artifact_id: str) -> Artifact | None:
        with self.session_factory() as session:
            return session.get(Artifact, artifact_id)

    def mark_processing(self, document_id: str) -> str:
        with self.session_factory() as session:
            document = session.get(Document, document_id)
            if document is None:
                raise ValueError(f"Unknown document {document_id}")
            if document.status == "processing":
                raise DocumentProcessingError("Document ingestion is already in progress.")
            previous_status = document.status
            metadata = document.metadata_json if isinstance(document.metadata_json, dict) else {}
            document.status = "processing"
            document.metadata_json = {
                **metadata,
                "ingestion": {
                    "phase": "starting",
                    "phase_label": "Starting ingestion",
                    "previous_status": previous_status,
                },
            }
            session.commit()
            return previous_status

    def stop_ingestion(
        self,
        document_id: str,
        *,
        phase_label: str = "Ingestion stopped",
    ) -> Document:
        with self.session_factory() as session:
            document = session.get(Document, document_id)
            if document is None:
                raise ValueError(f"Unknown document {document_id}")
            if document.status != "processing":
                raise DocumentProcessingError("Document ingestion is not running.")
            metadata = (
                document.metadata_json
                if isinstance(document.metadata_json, dict)
                else {}
            )
            ingestion = metadata.get("ingestion")
            progress = ingestion if isinstance(ingestion, dict) else {}
            previous_status = progress.get("previous_status")
            if previous_status not in {"uploaded", "ready", "failed"}:
                previous_status = "ready" if document.page_count is not None else "uploaded"
            document.status = previous_status
            document.metadata_json = {
                **metadata,
                "ingestion": {
                    **progress,
                    "phase": "stopped",
                    "phase_label": phase_label,
                },
            }
            session.commit()
            session.refresh(document)
            return document

    def recover_stale_ingestions(self) -> list[str]:
        with self.session_factory() as session:
            document_ids = list(
                session.scalars(
                    select(Document.id).where(Document.status == "processing")
                )
            )
        for document_id in document_ids:
            self.stop_ingestion(
                document_id,
                phase_label="Interrupted ingestion cleared after application restart",
            )
        return document_ids

    def update_ingestion_progress(
        self,
        document_id: str,
        progress: dict[str, Any],
    ) -> None:
        with self.session_factory() as session:
            document = session.get(Document, document_id)
            if document is None:
                raise ValueError(f"Unknown document {document_id}")
            if document.status != "processing":
                return
            metadata = document.metadata_json if isinstance(document.metadata_json, dict) else {}
            ingestion = metadata.get("ingestion")
            current = ingestion if isinstance(ingestion, dict) else {}
            document.metadata_json = {
                **metadata,
                "ingestion": {**current, **progress},
            }
            session.commit()

    def finish_failed_ingestion(
        self,
        document_id: str,
        previous_status: str,
    ) -> None:
        with self.session_factory() as session:
            document = session.get(Document, document_id)
            if document is None:
                raise ValueError(f"Unknown document {document_id}")
            if document.status != "processing":
                return
            metadata = document.metadata_json if isinstance(document.metadata_json, dict) else {}
            document.status = "ready" if previous_status == "ready" else "failed"
            document.metadata_json = {
                **metadata,
                "ingestion": {
                    "phase": "failed",
                    "phase_label": "Ingestion failed",
                },
            }
            session.commit()

    def mark_ready(
        self,
        document_id: str,
        *,
        page_count: int,
        metadata: dict[str, Any],
    ) -> None:
        with self.session_factory() as session:
            document = session.get(Document, document_id)
            if document is None:
                raise ValueError(f"Unknown document {document_id}")
            document.status = "ready"
            document.page_count = page_count
            document.metadata_json = {
                **metadata,
                "ingestion": {
                    "phase": "complete",
                    "phase_label": "Ingestion complete",
                    "completed_pages": page_count,
                    "total_pages": page_count,
                },
            }
            session.commit()

    def delete(self, document_id: str) -> bool:
        details = self.get_details(document_id)
        if not details:
            return False
        _, artifacts, _ = details
        for artifact in artifacts:
            self._delete_artifact_file(artifact)
        with self.session_factory() as session:
            session.execute(
                delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
            )
            session.execute(delete(Artifact).where(Artifact.document_id == document_id))
            document = session.get(Document, document_id)
            if document is not None:
                session.delete(document)
            session.commit()
        return True

    def delete_run_artifact_files(self, run_id: str) -> None:
        with self.session_factory() as session:
            artifacts = list(
                session.scalars(select(Artifact).where(Artifact.run_id == run_id))
            )
        for artifact in artifacts:
            self._delete_artifact_file(artifact)

    def clear_generated_document_outputs(self, document_id: str) -> None:
        with self.session_factory() as session:
            artifacts = list(
                session.scalars(
                    select(Artifact).where(
                        Artifact.document_id == document_id,
                        Artifact.kind != "source_pdf",
                    )
                )
            )
        for artifact in artifacts:
            self._delete_artifact_file(artifact)
        self.storage.delete_stored_tree(
            self.settings.artifacts_dir,
            f"documents/{document_id}",
        )
        with self.session_factory() as session:
            session.execute(
                delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
            )
            session.execute(
                delete(Artifact).where(
                    Artifact.document_id == document_id,
                    Artifact.kind != "source_pdf",
                )
            )
            session.commit()

    def artifact_bytes(self, artifact: Artifact) -> bytes:
        return (self._artifact_base(artifact) / artifact.relative_path).read_bytes()

    def artifact_content(self, artifact: Artifact) -> Any:
        raw = self.artifact_bytes(artifact)
        if artifact.media_type == "application/json":
            return json.loads(raw.decode("utf-8"))
        if artifact.media_type.startswith("text/"):
            return raw.decode("utf-8")
        return raw.decode("utf-8", errors="replace")

    def _delete_artifact_file(self, artifact: Artifact) -> None:
        self.storage.delete_stored_file(
            self._artifact_base(artifact),
            artifact.relative_path,
        )

    def _artifact_base(self, artifact: Artifact) -> Path:
        storage_area = (artifact.metadata_json or {}).get("storage_area", "artifacts")
        if storage_area == "documents":
            return self.settings.documents_dir
        return self.settings.artifacts_dir
