from __future__ import annotations

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile

from backend.core.http import services
from backend.workspace.schemas import ReaderNotePage
from backend.tracks.schemas import (
    ImportApplyRequest, ImportPreviewRequest, ImportPreviewResponse, RevisionResponse,
    TrackCreateRequest, TrackDetailResponse, TrackSection, TrackSectionResponse,
    TrackSectionWriteRequest,
)

router = APIRouter(prefix="/tracks", tags=["tracks"])


@router.get("", response_model=list[TrackDetailResponse])
def list_tracks(container=Depends(services)):
    return container.tracks.list()


@router.post("", response_model=TrackDetailResponse, status_code=201)
def create_track(payload: TrackCreateRequest, container=Depends(services)):
    return container.tracks.create(payload.title)


@router.get("/{track_id}", response_model=TrackDetailResponse)
def get_track(track_id: str, container=Depends(services)):
    return container.tracks.get(track_id)


@router.get("/{track_id}/sections/{section}", response_model=TrackSectionResponse)
def read_section(track_id: str, section: TrackSection, container=Depends(services)):
    return container.tracks.read_section(track_id, section)


@router.get("/{track_id}/reader-notes", response_model=ReaderNotePage)
def reader_notes(
    track_id: str, query: str | None = Query(default=None, min_length=1, max_length=2000),
    limit: int = Query(default=25, ge=1, le=100), offset: int = Query(default=0, ge=0),
    container=Depends(services),
) -> ReaderNotePage:
    return ReaderNotePage.model_validate(container.tracks.reader_notes(track_id, query=query, limit=limit, offset=offset))


@router.get("/{track_id}/exploration/{name}", response_model=TrackSectionResponse)
def read_exploration(track_id: str, name: str, container=Depends(services)):
    return container.tracks.read_section(track_id, f"exploration/{name}")


@router.put("/{track_id}/exploration/{name}", response_model=TrackSectionResponse)
def write_exploration(
    track_id: str, name: str, payload: TrackSectionWriteRequest, container=Depends(services),
):
    return container.tracks.write_section(
        track_id, f"exploration/{name}", payload.content, payload.expected_sha256,
    )


@router.get("/{track_id}/exploration/{name}/history", response_model=list[RevisionResponse])
def exploration_history(track_id: str, name: str, container=Depends(services)):
    return container.tracks.history(track_id, f"exploration/{name}")


@router.get("/{track_id}/exploration/{name}/history/{revision}", response_model=TrackSectionResponse)
def exploration_revision(track_id: str, name: str, revision: str, container=Depends(services)):
    return container.tracks.read_history(track_id, f"exploration/{name}", revision)


@router.put("/{track_id}/sections/{section}", response_model=TrackSectionResponse)
def write_section(
    track_id: str, section: TrackSection, payload: TrackSectionWriteRequest,
    container=Depends(services),
):
    return container.tracks.write_section(track_id, section, payload.content, payload.expected_sha256)


@router.get("/{track_id}/sections/{section}/history", response_model=list[RevisionResponse])
def list_history(track_id: str, section: TrackSection, container=Depends(services)):
    return container.tracks.history(track_id, section)


@router.get("/{track_id}/sections/{section}/history/{revision}", response_model=TrackSectionResponse)
def read_history(track_id: str, section: TrackSection, revision: str, container=Depends(services)):
    return container.tracks.read_history(track_id, section, revision)


@router.post("/{track_id}/imports/preview", response_model=ImportPreviewResponse)
def preview_import(track_id: str, payload: ImportPreviewRequest, container=Depends(services)):
    return container.tracks.preview_import(track_id, payload.section, payload.content)


@router.post("/{track_id}/imports/preview-zip", response_model=ImportPreviewResponse)
async def preview_zip(
    track_id: str, file: UploadFile = File(...), container=Depends(services),
):
    data = await container.storage.read_upload(file)
    return container.tracks.preview_zip(track_id, data)


@router.post("/{track_id}/imports", response_model=TrackDetailResponse)
def apply_import(track_id: str, payload: ImportApplyRequest, container=Depends(services)):
    return container.tracks.apply_import(track_id, **payload.model_dump())


@router.get("/{track_id}/export")
def export(track_id: str, section: TrackSection | None = None, container=Depends(services)):
    data, media_type, filename = container.tracks.export(track_id, section)
    return Response(data, media_type=media_type, headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
    })


@router.put("/{track_id}/papers/{document_id}", response_model=TrackDetailResponse)
def add_paper(track_id: str, document_id: str, container=Depends(services)):
    return container.tracks.add_paper(track_id, document_id)


@router.delete("/{track_id}/papers/{document_id}", response_model=TrackDetailResponse)
def remove_paper(track_id: str, document_id: str, container=Depends(services)):
    return container.tracks.remove_paper(track_id, document_id)


@router.put("/{track_id}/conversations/{conversation_id}", response_model=TrackDetailResponse)
def link_conversation(track_id: str, conversation_id: str, container=Depends(services)):
    return container.tracks.link_conversation(track_id, conversation_id)


@router.delete("/{track_id}/conversations/{conversation_id}", response_model=TrackDetailResponse)
def unlink_conversation(track_id: str, conversation_id: str, container=Depends(services)):
    container.tracks.get(track_id)
    linked = container.tracks.for_conversation(conversation_id)
    if linked is not None and linked.id == track_id:
        container.tracks.unlink_conversation(conversation_id)
    return container.tracks.get(track_id)
