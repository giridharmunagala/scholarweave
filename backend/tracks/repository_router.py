from fastapi import APIRouter, Depends, Query, Response

from backend.core.http import services
from backend.tracks.repository_schemas import (
    ArtifactLinkRequest, ArtifactPromotionRequest, RepositoryEditRequest, RepositoryLink, RepositoryLinkRequest,
    RepositoryListing, RepositoryPreview,
)
from backend.tracks.schemas import TrackSectionResponse

router = APIRouter(prefix="/tracks/{track_id}/repositories", tags=["linked-repositories"])


@router.get("", response_model=list[RepositoryLink])
def list_repositories(track_id: str, container=Depends(services)):
    return container.linked_repositories.list(track_id)


@router.post("", response_model=RepositoryLink, status_code=201)
def link_repository(track_id: str, payload: RepositoryLinkRequest, container=Depends(services)):
    return container.linked_repositories.link(track_id, payload.root, payload.authorize_root)


@router.delete("/{link_id}", status_code=204)
def unlink_repository(track_id: str, link_id: str, container=Depends(services)):
    container.linked_repositories.unlink(track_id, link_id)


@router.get("/{link_id}/browse", response_model=RepositoryListing)
def browse(track_id: str, link_id: str, path: str = "", container=Depends(services)):
    return container.linked_repositories.browse(track_id, link_id, path)


@router.get("/{link_id}/preview", response_model=RepositoryPreview)
def preview(track_id: str, link_id: str, path: str, container=Depends(services)):
    return container.linked_repositories.preview(track_id, link_id, path)


@router.get("/{link_id}/media")
def media(track_id: str, link_id: str, path: str,
          expected_sha256: str | None = Query(default=None, pattern=r"^[0-9a-f]{64}$"),
          container=Depends(services)):
    content, media_type = container.linked_repositories.media(track_id, link_id, path, expected_sha256)
    return Response(content, media_type=media_type, headers={
        "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'; sandbox",
        "Cache-Control": "no-store",
        "Content-Disposition": "attachment",
    })


@router.put("/{link_id}/file", response_model=RepositoryPreview)
def edit(track_id: str, link_id: str, payload: RepositoryEditRequest, container=Depends(services)):
    return container.linked_repositories.edit(track_id, link_id, **payload.model_dump())


@router.post("/{link_id}/artifacts", response_model=RepositoryLink)
def link_artifact(track_id: str, link_id: str, payload: ArtifactLinkRequest, container=Depends(services)):
    return container.linked_repositories.artifact(track_id, link_id, payload)


@router.post("/{link_id}/refresh", response_model=RepositoryLink)
def refresh(track_id: str, link_id: str, container=Depends(services)):
    return container.linked_repositories.refresh(track_id, link_id)


@router.post("/{link_id}/artifacts/{artifact_id}/promote", response_model=TrackSectionResponse)
def promote(track_id: str, link_id: str, artifact_id: str, payload: ArtifactPromotionRequest,
            container=Depends(services)):
    return container.linked_repositories.promote(track_id, link_id, artifact_id, payload)
