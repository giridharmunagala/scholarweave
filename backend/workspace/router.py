from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response, status

from backend.core.http import services
from backend.workspace.schemas import (
    WorkspaceFileContentResponse,
    WorkspaceFileResponse,
    WorkspaceFileWriteRequest,
    WorkspaceIndexResponse,
    WorkspaceKind,
    WorkspaceNoteCreateRequest,
    WorkspaceNoteCleanupPreview,
    WorkspaceNoteCleanupRequest,
    WorkspaceNoteCleanupResponse,
    WorkspaceNoteEditRequest,
    WorkspaceSearchResponse,
    ReaderNotePage, ReaderNoteConnectionRequest,
)

router = APIRouter(prefix="/workspace", tags=["workspace"])


@router.get("/reader-notes", response_model=ReaderNotePage)
def reader_notes(
    query: str | None = Query(default=None, min_length=1, max_length=2000),
    paper_id: str | None = None, connected_to: str | None = None, note_id: str | None = None,
    limit: int = Query(default=25, ge=1, le=100), offset: int = Query(default=0, ge=0),
    container=Depends(services),
) -> ReaderNotePage:
    return ReaderNotePage.model_validate(container.workspace.reader_notes(
        query=query, paper_ids=[paper_id] if paper_id else None,
        connected_to=connected_to, note_id=note_id, limit=limit, offset=offset,
    ))


@router.post("/reader-notes/connections", response_model=WorkspaceFileContentResponse)
def connect_reader_notes(payload: ReaderNoteConnectionRequest, container=Depends(services)) -> WorkspaceFileContentResponse:
    return WorkspaceFileContentResponse.model_validate(container.workspace.connect_reader_notes(**payload.model_dump()))


@router.get("/files", response_model=list[WorkspaceFileResponse])
def list_files(container=Depends(services)) -> list[WorkspaceFileResponse]:
    return [WorkspaceFileResponse.model_validate(item) for item in container.workspace.list_files()]


@router.get("/notes", response_model=list[WorkspaceFileResponse])
def list_notes(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    container=Depends(services),
) -> list[WorkspaceFileResponse]:
    """List standalone and canonical paper notes without reading file bodies."""
    return [
        WorkspaceFileResponse.model_validate(item)
        for item in container.workspace.list_collection("notes", limit=limit, offset=offset)
    ]


@router.post("/notes/cleanup/preview", response_model=WorkspaceNoteCleanupPreview)
def preview_note_cleanup(container=Depends(services)) -> WorkspaceNoteCleanupPreview:
    """Inspect current note bytes without deleting anything."""
    return WorkspaceNoteCleanupPreview.model_validate(container.workspace.preview_note_cleanup())


@router.post("/notes/cleanup", response_model=WorkspaceNoteCleanupResponse)
def cleanup_notes(
    payload: WorkspaceNoteCleanupRequest,
    container=Depends(services),
) -> WorkspaceNoteCleanupResponse:
    """Delete only confirmed note revisions still smaller than 1024 bytes."""
    return WorkspaceNoteCleanupResponse.model_validate(
        container.workspace.cleanup_notes([item.model_dump() for item in payload.candidates])
    )


@router.get("/summaries", response_model=list[WorkspaceFileResponse])
def list_summaries(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    container=Depends(services),
) -> list[WorkspaceFileResponse]:
    """List canonical summaries; immutable versions remain under /documents/{id}/summaries."""
    return [
        WorkspaceFileResponse.model_validate(item)
        for item in container.workspace.list_collection("summaries", limit=limit, offset=offset)
    ]


@router.get("/search", response_model=list[WorkspaceSearchResponse])
def search_workspace(
    query: str | None = Query(default=None, min_length=1, max_length=2000),
    kinds: list[WorkspaceKind] = Query(default=[]),
    tags: list[str] = Query(default=[]),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    container=Depends(services),
) -> list[WorkspaceSearchResponse]:
    """BM25 search plus directly connected reader highlights, or browsing without a query."""
    return [
        WorkspaceSearchResponse.model_validate(item)
        for item in container.workspace.search(
            query=query, kinds=kinds, tags=tags, limit=limit, offset=offset,
        )
    ]


@router.get("/index", response_model=WorkspaceIndexResponse)
def index_status(container=Depends(services)) -> WorkspaceIndexResponse:
    return WorkspaceIndexResponse.model_validate(container.workspace.index_status())


@router.post("/index", response_model=WorkspaceIndexResponse)
def refresh_index(container=Depends(services)) -> WorkspaceIndexResponse:
    """Reconcile external edits/deletions and rebuild the persistent index, preserving metadata."""
    return WorkspaceIndexResponse.model_validate(container.workspace.refresh_index())


@router.get("/files/content", response_model=WorkspaceFileContentResponse)
def read_file(
    path: str = Query(min_length=1),
    container=Depends(services),
) -> WorkspaceFileContentResponse:
    return WorkspaceFileContentResponse.model_validate(container.workspace.read_file(path))


@router.put("/files/content", response_model=WorkspaceFileContentResponse)
def write_file(
    payload: WorkspaceFileWriteRequest,
    container=Depends(services),
) -> WorkspaceFileContentResponse:
    return WorkspaceFileContentResponse.model_validate(
        container.workspace.write_user_file(
            payload.path, payload.content, tags=payload.tags,
            expected_sha256=payload.expected_sha256,
        )
    )


@router.patch("/files/content", response_model=WorkspaceFileContentResponse)
def edit_note(
    payload: WorkspaceNoteEditRequest,
    container=Depends(services),
) -> WorkspaceFileContentResponse:
    return WorkspaceFileContentResponse.model_validate(
        container.workspace.edit_note(
            payload.path, operation=payload.operation, content=payload.content,
            selection=payload.selection, expected_sha256=payload.expected_sha256,
        )
    )


@router.post("/files/notes", response_model=WorkspaceFileContentResponse)
def create_note(
    payload: WorkspaceNoteCreateRequest,
    container=Depends(services),
) -> WorkspaceFileContentResponse:
    return WorkspaceFileContentResponse.model_validate(
        container.workspace.create_note(name=payload.name, content=payload.content, tags=payload.tags)
    )


@router.delete("/files/content", status_code=status.HTTP_204_NO_CONTENT)
def delete_file(
    path: str = Query(min_length=1),
    container=Depends(services),
) -> Response:
    container.workspace.delete_user_file(path)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/files/folder", status_code=status.HTTP_204_NO_CONTENT)
def delete_folder(
    path: str = Query(min_length=1),
    container=Depends(services),
) -> Response:
    container.workspace.delete_folder(path)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
