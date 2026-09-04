from __future__ import annotations

from pydantic import BaseModel
from fastapi import APIRouter, Depends

from backend.core.http import services
from backend.core.settings_service import SettingsResponse, SettingsUpdate

router = APIRouter(tags=["application"])


class HealthResponse(BaseModel):
    status: str
    sdk_version: str
    database_path: str
    data_dir: str
    frontend_available: bool
    ocr_available: bool


@router.get("/health", response_model=HealthResponse)
def health(container=Depends(services)) -> HealthResponse:
    return HealthResponse(
        status="ok",
        sdk_version=container.sdk_version,
        database_path=str(container.settings.database_path),
        data_dir=str(container.settings.data_dir),
        frontend_available=container.settings.frontend_dist_dir.exists(),
        ocr_available=container.documents.ocr_available(),
    )


@router.get("/settings", response_model=SettingsResponse)
def get_settings(container=Depends(services)) -> SettingsResponse:
    return container.settings_service.response()


@router.put("/settings", response_model=SettingsResponse)
def update_settings(
    payload: SettingsUpdate,
    container=Depends(services),
) -> SettingsResponse:
    return container.settings_service.update(payload)
