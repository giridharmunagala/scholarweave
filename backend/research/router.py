from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from fastapi.responses import StreamingResponse

from backend.core.http import services
from backend.runs.schemas import run_response
from backend.research.schemas import (
    ArtifactContentResponse,
    ArtifactResponse,
    DocumentChunkResponse,
    DocumentResponse,
    DocumentSummaryResponse,
    IngestionOptionsResponse,
    PaperFolderAssignmentRequest,
    PaperFolderCreateRequest,
    PaperFolderResponse,
    PaperSummaryContentResponse,
    PaperSummaryPromotionResponse,
    PaperSummaryRunRequest,
    PaperSummaryRunResponse,
    PaperSummaryVersionResponse,
    RemotePdfDownloadRequest,
    SavedWebSourceNoteResponse,
    WebSourceCreateRequest,
    WebSourceNoteRequest,
    WebSourceResponse,
)

router = APIRouter(tags=["research"])
summary_router = APIRouter(
    prefix="/documents/{document_id}/summaries",
    tags=["paper summaries"],
)


@summary_router.post(
    "",
    response_model=PaperSummaryRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_summary(
    document_id: str,
    payload: PaperSummaryRunRequest,
    container=Depends(services),
) -> PaperSummaryRunResponse:
    run, revision = container.summaries.start(
        document_id,
        model_reference=payload.model_reference,
        reasoning_effort=payload.reasoning_effort,
    )
    return PaperSummaryRunResponse(
        run=run_response(container.runs.get(run.id)),
        prompt_revision=revision,
    )


@summary_router.get(
    "",
    response_model=list[PaperSummaryVersionResponse],
)
def list_summary_versions(
    document_id: str,
    container=Depends(services),
) -> list[PaperSummaryVersionResponse]:
    return [
        PaperSummaryVersionResponse.model_validate(item)
        for item in container.summaries.versions(document_id)
    ]


@summary_router.get(
    "/{version_id}",
    response_model=PaperSummaryContentResponse,
)
def get_summary_version(
    document_id: str,
    version_id: str,
    container=Depends(services),
) -> PaperSummaryContentResponse:
    version, content = container.summaries.version(document_id, version_id)
    return PaperSummaryContentResponse(
        version=PaperSummaryVersionResponse.model_validate(version),
        content=content,
    )


@summary_router.post(
    "/{version_id}/promote",
    response_model=PaperSummaryPromotionResponse,
)
def promote_summary_version(
    document_id: str,
    version_id: str,
    container=Depends(services),
) -> PaperSummaryPromotionResponse:
    version, path, content = container.summaries.promote(document_id, version_id)
    return PaperSummaryPromotionResponse(
        version=PaperSummaryVersionResponse.model_validate(version),
        summary_path=path,
        content=content,
    )


@router.get("/paper-folders", response_model=list[PaperFolderResponse])
def list_paper_folders(container=Depends(services)) -> list[PaperFolderResponse]:
    return [_paper_folder_response(folder) for folder in container.documents.list_folders()]


@router.post(
    "/paper-folders",
    response_model=PaperFolderResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_paper_folder(
    payload: PaperFolderCreateRequest,
    container=Depends(services),
) -> PaperFolderResponse:
    return _paper_folder_response(container.documents.create_folder(payload.name))


@router.get("/documents", response_model=list[DocumentSummaryResponse])
def list_documents(container=Depends(services)) -> list[DocumentSummaryResponse]:
    return [
        _document_summary(document)
        for document in container.documents.list_documents()
    ]


@router.post(
    "/documents",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    container=Depends(services),
) -> DocumentResponse:
    if file.content_type not in {"application/pdf", "application/octet-stream"}:
        raise HTTPException(status_code=400, detail="Only PDF uploads are supported.")
    document = await container.documents.create_document_from_upload(file, title=title)
    return _document_response(container, document.id)


@router.post(
    "/documents/download",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def download_document(
    payload: RemotePdfDownloadRequest,
    container=Depends(services),
) -> DocumentResponse:
    try:
        document = await container.source_downloads.download_pdf(
            payload.url,
            title=payload.title,
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _document_response(container, document.id)


@router.get("/web-sources", response_model=list[WebSourceResponse])
async def list_web_sources(container=Depends(services)) -> list[WebSourceResponse]:
    return [_web_source_response(source) for source in await container.source_downloads.list_web_sources()]


@router.post(
    "/web-sources",
    response_model=WebSourceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def download_web_source(
    payload: WebSourceCreateRequest,
    container=Depends(services),
) -> WebSourceResponse:
    try:
        source = await container.source_downloads.download_web_page(payload.url)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _web_source_response(source, include_text=True)


@router.get("/web-sources/{source_id}", response_model=WebSourceResponse)
async def get_web_source(
    source_id: str,
    container=Depends(services),
) -> WebSourceResponse:
    try:
        source = await container.source_downloads.get_web_source(source_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _web_source_response(source, include_text=True)


@router.delete("/web-sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_web_source(
    source_id: str,
    container=Depends(services),
) -> Response:
    if not await container.source_downloads.delete_web_source(source_id):
        raise HTTPException(status_code=404, detail="Temporary web page was not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/web-sources/{source_id}/notes",
    response_model=SavedWebSourceNoteResponse,
    status_code=status.HTTP_201_CREATED,
)
async def save_web_source_note(
    source_id: str,
    payload: WebSourceNoteRequest,
    container=Depends(services),
) -> SavedWebSourceNoteResponse:
    try:
        note = await container.source_downloads.save_web_note(
            source_id,
            name=payload.name,
            content=payload.content,
            tags=payload.tags,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return SavedWebSourceNoteResponse(
        path=note.path,
        note_id=note.note_id,
        name=note.note_name,
    )


@router.get("/documents/{document_id}", response_model=DocumentResponse)
def get_document(document_id: str, container=Depends(services)) -> DocumentResponse:
    return _document_response(container, document_id)


@router.put("/documents/{document_id}/folder", response_model=DocumentResponse)
def assign_document_folder(
    document_id: str,
    payload: PaperFolderAssignmentRequest,
    container=Depends(services),
) -> DocumentResponse:
    container.documents.assign_folder(document_id, payload.folder_id)
    return _document_response(container, document_id)


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(document_id: str, container=Depends(services)) -> Response:
    if not container.documents.delete_document(document_id):
        raise HTTPException(status_code=404, detail="Document was not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/documents/{document_id}/ingestion-options",
    response_model=IngestionOptionsResponse,
)
async def document_ingestion_options(
    document_id: str,
    container=Depends(services),
) -> IngestionOptionsResponse:
    try:
        options = await container.documents.ingestion_options(document_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return IngestionOptionsResponse.model_validate(options)


@router.post("/documents/{document_id}/ingest", response_model=DocumentResponse)
async def ingest_document(
    document_id: str,
    mode: Literal["embedded", "ocr"] = "embedded",
    container=Depends(services),
) -> DocumentResponse:
    await container.documents.ingest_document(document_id, force_ocr=mode == "ocr")
    return _document_response(container, document_id)


@router.post("/documents/{document_id}/ingest/stop", response_model=DocumentResponse)
async def stop_document_ingestion(
    document_id: str,
    container=Depends(services),
) -> DocumentResponse:
    container.documents.stop_ingestion(document_id)
    return _document_response(container, document_id)


@router.get("/documents/{document_id}/ingest/events")
async def stream_document_ingestion(
    document_id: str,
    container=Depends(services),
) -> StreamingResponse:
    _document_response(container, document_id)

    async def events() -> AsyncIterator[str]:
        async with container.documents.subscribe_to_ingestion(document_id) as queue:
            while True:
                document = _document_response(container, document_id)
                yield _document_sse(document)
                if document.status != "processing":
                    break
                try:
                    await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/artifacts/{artifact_id}", response_model=ArtifactResponse)
def get_artifact(artifact_id: str, container=Depends(services)) -> ArtifactResponse:
    artifact = container.documents.get_artifact(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact was not found.")
    return _artifact_response(artifact)


@router.get("/artifacts/{artifact_id}/content", response_model=ArtifactContentResponse)
def get_artifact_content(
    artifact_id: str,
    response: Response,
    container=Depends(services),
) -> ArtifactContentResponse:
    artifact = container.documents.get_artifact(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact was not found.")
    response.headers["Cache-Control"] = "no-store"
    return ArtifactContentResponse(
        artifact=_artifact_response(artifact),
        content=container.documents.artifact_content(artifact),
    )


@router.get("/artifacts/{artifact_id}/raw")
def get_artifact_raw(artifact_id: str, container=Depends(services)) -> Response:
    artifact = container.documents.get_artifact(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact was not found.")
    return Response(
        content=container.documents.artifact_bytes(artifact),
        media_type=artifact.media_type,
        headers={
            "Content-Disposition": f'inline; filename="{Path(artifact.relative_path).name}"',
            "Cache-Control": "no-store",
        },
    )


def _document_response(container, document_id: str) -> DocumentResponse:
    details = container.documents.get_document_details(document_id)
    if details is None:
        raise HTTPException(status_code=404, detail="Document was not found.")
    document, artifacts, chunks = details
    return DocumentResponse(
        id=document.id,
        title=document.title,
        source_filename=document.source_filename,
        content_type=document.content_type,
        status=document.status,
        page_count=document.page_count,
        metadata=document.metadata_json or {},
        artifacts=[_artifact_response(artifact) for artifact in artifacts],
        chunks=[
            DocumentChunkResponse(
                id=chunk.id,
                chunk_index=chunk.chunk_index,
                section_title=chunk.section_title,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                citation=chunk.citation,
                text=chunk.text,
                metadata=chunk.metadata_json or {},
            )
            for chunk in chunks
        ],
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


def _document_summary(document) -> DocumentSummaryResponse:
    return DocumentSummaryResponse(
        id=document.id,
        title=document.title,
        source_filename=document.source_filename,
        content_type=document.content_type,
        status=document.status,
        page_count=document.page_count,
        metadata=document.metadata_json or {},
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


def _paper_folder_response(folder) -> PaperFolderResponse:
    return PaperFolderResponse(
        id=folder.id,
        name=folder.name,
        created_at=folder.created_at,
        updated_at=folder.updated_at,
    )


def _document_sse(document: DocumentResponse) -> str:
    data = json.dumps(document.model_dump(mode="json"), ensure_ascii=True)
    return f"event: document.updated\ndata: {data}\n\n"


def _artifact_response(artifact) -> ArtifactResponse:
    return ArtifactResponse(
        id=artifact.id,
        document_id=artifact.document_id,
        owner_type=artifact.owner_type,
        kind=artifact.kind,
        relative_path=artifact.relative_path,
        media_type=artifact.media_type,
        size_bytes=artifact.size_bytes,
        sha256=artifact.sha256,
        metadata=artifact.metadata_json or {},
        created_at=artifact.created_at,
    )


def _web_source_response(source, *, include_text: bool = False) -> WebSourceResponse:
    return WebSourceResponse(
        id=source.id,
        url=source.url,
        title=source.title,
        text=source.text if include_text else None,
        chunk_count=len(source.chunks),
        created_at=source.created_at,
        expires_at=source.expires_at,
    )
