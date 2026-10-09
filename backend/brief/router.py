from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from backend.brief.schemas import (
    BriefCatalogResponse, BriefEditionResponse, BriefItemResponse,
    BriefPreferences, BriefPreviewResponse, BriefRefreshRequest,
)
from backend.core.http import services

router = APIRouter(prefix="/brief", tags=["morning brief"])


@router.get("/catalog", response_model=BriefCatalogResponse)
def catalog(container=Depends(services)) -> BriefCatalogResponse:
    return container.brief.catalog()


@router.put("/preferences", response_model=BriefCatalogResponse)
async def preferences(payload: BriefPreferences, container=Depends(services)) -> BriefCatalogResponse:
    return await container.brief.preferences(payload)


@router.get("", response_model=BriefEditionResponse)
async def edition(response: Response, container=Depends(services)) -> BriefEditionResponse:
    response.headers["Cache-Control"] = "no-store"
    return await container.brief.edition()


@router.post("/refresh", response_model=BriefEditionResponse)
async def refresh(payload: BriefRefreshRequest, response: Response, container=Depends(services)) -> BriefEditionResponse:
    response.headers["Cache-Control"] = "no-store"
    return await container.brief.refresh(force=payload.force)


@router.delete("", response_model=BriefEditionResponse)
async def clear(response: Response, container=Depends(services)) -> BriefEditionResponse:
    response.headers["Cache-Control"] = "no-store"
    return await container.brief.clear()


@router.post("/items/{item_id}/fetch", response_model=BriefPreviewResponse)
async def fetch(item_id: str, response: Response, container=Depends(services)) -> BriefPreviewResponse:
    response.headers["Cache-Control"] = "no-store"
    return await container.brief.fetch(item_id)


@router.get("/items/{item_id}/raw")
async def raw(item_id: str, container=Depends(services)) -> Response:
    content, media_type = await container.brief.raw(item_id)
    return Response(content, media_type=media_type, headers={"Cache-Control": "no-store"})


@router.post("/items/{item_id}/keep", response_model=BriefItemResponse)
async def keep(item_id: str, container=Depends(services)) -> BriefItemResponse:
    return await container.brief.keep(item_id)


@router.delete("/items/{item_id}", response_model=BriefEditionResponse)
async def discard(item_id: str, response: Response, container=Depends(services)) -> BriefEditionResponse:
    response.headers["Cache-Control"] = "no-store"
    return await container.brief.discard(item_id)
