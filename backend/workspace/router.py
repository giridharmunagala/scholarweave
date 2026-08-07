from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response, status

from backend.api.dependencies import services
from backend.workspace.schemas import (
    WorkspaceFileContentResponse,
    WorkspaceFileResponse,
    WorkspaceFileWriteRequest,
    WorkspaceNoteCreateRequest,
)

router = APIRouter(prefix="/workspace/files", tags=["workspace"])


@router.get("", response_model=list[WorkspaceFileResponse])
def list_files(container=Depends(services)) -> list[WorkspaceFileResponse]:
    return [
        WorkspaceFileResponse(
            path=document.path,
            name=document.name,
            media_type=document.media_type,
            size_bytes=document.size_bytes,
            modified_at=document.modified_at,
            tags=list(document.tags),
            paper_id=document.paper_id,
            paper_name=document.paper_name,
            note_id=document.note_id,
            note_name=document.note_name,
            kind=document.kind,
        )
        for document in container.workspace.list_files()
    ]


@router.get("/content", response_model=WorkspaceFileContentResponse)
def read_file(
    path: str = Query(min_length=1),
    container=Depends(services),
) -> WorkspaceFileContentResponse:
    document = container.workspace.read_file(path)
    return WorkspaceFileContentResponse(
        path=document.path,
        name=document.name,
        media_type=document.media_type,
        size_bytes=document.size_bytes,
        modified_at=document.modified_at,
        tags=list(document.tags),
        paper_id=document.paper_id,
        paper_name=document.paper_name,
        note_id=document.note_id,
        note_name=document.note_name,
        kind=document.kind,
        content=document.content,
    )


@router.put("/content", response_model=WorkspaceFileContentResponse)
def write_file(
    payload: WorkspaceFileWriteRequest,
    container=Depends(services),
) -> WorkspaceFileContentResponse:
    document = container.workspace.write_file(payload.path, payload.content, tags=payload.tags)
    return WorkspaceFileContentResponse(
        path=document.path,
        name=document.name,
        media_type=document.media_type,
        size_bytes=document.size_bytes,
        modified_at=document.modified_at,
        tags=list(document.tags),
        paper_id=document.paper_id,
        paper_name=document.paper_name,
        note_id=document.note_id,
        note_name=document.note_name,
        kind=document.kind,
        content=document.content,
    )


@router.post("/notes", response_model=WorkspaceFileContentResponse)
def create_note(
    payload: WorkspaceNoteCreateRequest,
    container=Depends(services),
) -> WorkspaceFileContentResponse:
    document = container.workspace.create_note(
        name=payload.name,
        content=payload.content,
        tags=payload.tags,
    )
    return WorkspaceFileContentResponse(
        path=document.path,
        name=document.name,
        media_type=document.media_type,
        size_bytes=document.size_bytes,
        modified_at=document.modified_at,
        tags=list(document.tags),
        paper_id=document.paper_id,
        paper_name=document.paper_name,
        note_id=document.note_id,
        note_name=document.note_name,
        kind=document.kind,
        content=document.content,
    )


@router.delete("/content", status_code=status.HTTP_204_NO_CONTENT)
def delete_file(
    path: str = Query(min_length=1),
    container=Depends(services),
) -> Response:
    container.workspace.delete_file(path)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/folder", status_code=status.HTTP_204_NO_CONTENT)
def delete_folder(
    path: str = Query(min_length=1),
    container=Depends(services),
) -> Response:
    container.workspace.delete_folder(path)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
