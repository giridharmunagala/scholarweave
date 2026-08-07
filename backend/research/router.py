from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from fastapi.responses import StreamingResponse

from backend.api.dependencies import services
from backend.research.schemas import (
    ArtifactContentResponse,
    ArtifactResponse,
    DocumentChunkResponse,
    DocumentResponse,
    IngestionOptionsResponse,
)

router = APIRouter(tags=["research"])


@router.get("/documents", response_model=list[DocumentResponse])
def list_documents(container=Depends(services)) -> list[DocumentResponse]:
    return [
        _document_response(container, document.id)
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


@router.get("/documents/{document_id}", response_model=DocumentResponse)
def get_document(document_id: str, container=Depends(services)) -> DocumentResponse:
    return _document_response(container, document_id)


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(document_id: str, container=Depends(services)) -> Response:
    container.direct_agents.repository.clear_document_analysis(document_id)
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
    container.direct_agents.repository.clear_document_analysis(document_id)
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
