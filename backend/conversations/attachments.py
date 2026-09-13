from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

import anyio
from fastapi import UploadFile
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from backend.core.errors import DocumentProcessingError, NotFoundError, ValidationError
from backend.documents.service import DocumentService
from backend.persistence.files import SafeStorage
from backend.utils import clean_filename, dumps_json, sha256_bytes
from backend.workspace.layout import WorkspaceLayout
from backend.workspace.service import WorkspaceService


@dataclass(frozen=True, slots=True)
class ConversationAttachment:
    path: str
    name: str
    media_type: str
    size_bytes: int
    document_id: str | None = None


class ConversationAttachmentService:
    def __init__(
        self,
        storage: SafeStorage,
        workspace: WorkspaceService,
        documents: DocumentService,
    ) -> None:
        self._storage = storage
        self._workspace = workspace
        self._documents = documents

    async def upload(self, upload: UploadFile) -> ConversationAttachment:
        filename = clean_filename(upload.filename or "")
        suffix = Path(filename).suffix.lower()
        if suffix not in {".md", ".txt", ".pdf"}:
            raise ValidationError("Attach a Markdown (.md), plain-text (.txt), or PDF (.pdf) file.")
        filename = f"{Path(filename).stem[:100]}{suffix}"
        limit = (
            self._storage.settings.max_upload_bytes
            if suffix == ".pdf"
            else self._storage.settings.max_workspace_file_bytes
        )
        content = await self._storage.read_upload(upload, max_bytes=limit)
        if not content:
            raise ValidationError("The attached file is empty.")
        if suffix == ".pdf":
            return await self._upload_pdf(filename, content)
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError("Markdown and plain-text attachments must use UTF-8 encoding.") from exc
        if "\x00" in text or not text.lstrip("\ufeff").strip():
            raise ValidationError("The attachment must contain readable, non-empty text.")
        path = WorkspaceLayout.chat_upload(filename, sha256_bytes(content)[:24])
        return self.describe(self._save_text(path, text))

    async def _upload_pdf(self, filename: str, content: bytes) -> ConversationAttachment:
        await anyio.to_thread.run_sync(self._validate_pdf, content)
        source_hash = sha256_bytes(content)
        document = self._documents.find_by_source_hash(source_hash)
        if document is None:
            document = self._documents.create_document_from_bytes(
                content, filename=filename, title=Path(filename).stem,
            )
        source = next(
            (artifact for artifact in self._documents.get_document_artifacts(document.id)
             if artifact.kind == "source_pdf"),
            None,
        )
        if source is None or source.metadata_json.get("storage_area") != "workspace":
            raise DocumentProcessingError("The saved PDF has no workspace source. Repair it in the library.")
        try:
            existing_hash = self._storage.file_hash(self._storage.settings.workspace_dir, source.relative_path)
        except FileNotFoundError:
            self._storage.write_workspace_document(source.relative_path, content)
        else:
            if existing_hash != source_hash:
                raise ValidationError(
                    "The existing PDF was changed outside ScholarWeave. "
                    "Restore its original source or remove that library entry before re-uploading."
                )
        text = self._extracted_text(document.id) if document.status == "ready" else None
        if text is None:
            try:
                result = await self._documents.ingest_document(
                    document.id, enhance_with_llm=False,
                )
            except PyPdfError as exc:
                raise DocumentProcessingError(
                    "The PDF could not be read. Upload a valid, unencrypted PDF."
                ) from exc
            if result.get("stopped"):
                raise DocumentProcessingError("PDF preparation was stopped. Retry the upload when ready.")
            text = self._extracted_text(document.id)
        if text is None or not text.strip():
            raise DocumentProcessingError("The PDF has no readable text.")
        if len(text.encode("utf-8")) > self._storage.settings.max_workspace_file_bytes:
            raise ValidationError(
                "The PDF's extracted text exceeds the workspace file size limit. "
                "Attach a smaller section; the original PDF remains in the library."
            )
        path = WorkspaceLayout.chat_pdf_text(self._workspace.paper_folder(document.id))
        return self.describe(self._save_text(path, text))

    def _extracted_text(self, document_id: str) -> str | None:
        extracted = next(
            (artifact for artifact in self._documents.get_document_artifacts(document_id)
             if artifact.kind == "extracted_markdown"),
            None,
        )
        if extracted is None:
            return None
        try:
            text = self._documents.artifact_content(extracted)
        except FileNotFoundError:
            return None
        except UnicodeDecodeError as exc:
            raise DocumentProcessingError("The saved PDF text is invalid. Prepare the PDF in the library again.") from exc
        if not isinstance(text, str):
            raise DocumentProcessingError("The saved PDF text has an invalid format.")
        return text

    def _save_text(self, path: str, text: str) -> str:
        original = Path(path)
        version = 1
        while True:
            try:
                previous = self._workspace.read_file(path)
            except FileNotFoundError:
                break
            except UnicodeDecodeError as exc:
                raise ValidationError(
                    "An earlier attachment was changed to invalid UTF-8. Repair or rename that workspace file."
                ) from exc
            if previous.content == text:
                break
            # Deterministic alternatives preserve edits and remain reusable on later retries.
            version += 1
            path = original.with_name(f"{original.stem}-{version}{original.suffix}").as_posix()
        self._workspace.write_file(path, text)
        return path

    @staticmethod
    def _validate_pdf(content: bytes) -> None:
        try:
            reader = PdfReader(io.BytesIO(content))
            if reader.is_encrypted:
                raise ValidationError("Password-protected PDFs are not supported. Upload an unencrypted copy.")
            if not reader.pages:
                raise ValidationError("The PDF contains no pages.")
        except (PyPdfError, KeyError, TypeError, ValueError, IndexError) as exc:
            raise ValidationError("The file is not a readable PDF.") from exc

    def describe(self, path: str) -> ConversationAttachment:
        if Path(path).suffix.lower() not in {".md", ".txt"}:
            raise ValidationError("Attachments must reference uploaded Markdown, text, or prepared PDF text.")
        try:
            info = self._storage.workspace_file_info(path)
            file = self._workspace.read_file(info.relative_path)
        except FileNotFoundError as exc:
            raise NotFoundError("An attached workspace file is missing. Remove it or upload it again.") from exc
        except UnicodeDecodeError as exc:
            raise ValidationError("An attached workspace file is no longer valid UTF-8 text.") from exc
        if file.paper_id and WorkspaceLayout.is_chat_pdf_text(file.path):
            document = self._documents.get_document(file.paper_id)
            if document is None:
                raise NotFoundError("The attached PDF is no longer in the library.")
            source = next(
                (artifact for artifact in self._documents.get_document_artifacts(document.id)
                 if artifact.kind == "source_pdf"),
                None,
            )
            if source is None or source.metadata_json.get("storage_area") != "workspace":
                raise NotFoundError("The attached PDF's source is missing.")
            try:
                digest = self._storage.file_hash(self._storage.settings.workspace_dir, source.relative_path)
            except FileNotFoundError as exc:
                raise NotFoundError("The attached PDF's source is missing. Upload the PDF again to restore it.") from exc
            if digest != source.sha256:
                raise ValidationError("The attached PDF changed outside ScholarWeave. Restore its source before using it.")
            return ConversationAttachment(
                path=file.path,
                name=document.source_filename,
                media_type="application/pdf",
                size_bytes=source.size_bytes,
                document_id=document.id,
            )
        return ConversationAttachment(
            path=file.path, name=file.name, media_type=file.media_type, size_bytes=info.size_bytes,
        )

    def message_with_attachments(self, message: str, paths: list[str]) -> str:
        if not paths:
            return message
        attachments = [self.describe(path) for path in dict.fromkeys(paths)]
        references = [
            {"name": item.name, "path": item.path, "document_id": item.document_id}
            for item in attachments
        ]
        # Persist references with the user turn so follow-ups and recovered runs retain them.
        return (
            f"{message}\n\nAttached workspace files (saved and indexed):\n"
            f"```json\n{dumps_json(references)}\n```"
        )
