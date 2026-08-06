from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status

from backend.api.dependencies import services
from backend.providers.schemas import (
    ProviderCreate,
    ProviderModelsResponse,
    ProviderResponse,
    ProviderUpdate,
    ProviderVerifyRequest,
    ProviderVerifyResponse,
)
from backend.providers.service import ProviderService

router = APIRouter(prefix="/providers", tags=["providers"])


def provider_service(container=Depends(services)) -> ProviderService:
    return container.providers


@router.get("", response_model=list[ProviderResponse])
def list_providers(
    include_archived: bool = Query(default=False),
    service: ProviderService = Depends(provider_service),
) -> list[ProviderResponse]:
    return service.list(include_archived=include_archived)


@router.post("", response_model=ProviderResponse, status_code=status.HTTP_201_CREATED)
def create_provider(
    payload: ProviderCreate,
    service: ProviderService = Depends(provider_service),
) -> ProviderResponse:
    return service.create(payload)


@router.get("/{profile_id}", response_model=ProviderResponse)
def get_provider(
    profile_id: str,
    service: ProviderService = Depends(provider_service),
) -> ProviderResponse:
    return service.get(profile_id)


@router.put("/{profile_id}", response_model=ProviderResponse)
def update_provider(
    profile_id: str,
    payload: ProviderUpdate,
    service: ProviderService = Depends(provider_service),
) -> ProviderResponse:
    return service.update(profile_id, payload)


@router.delete("/{profile_id}", response_model=ProviderResponse)
def archive_provider(
    profile_id: str,
    service: ProviderService = Depends(provider_service),
) -> ProviderResponse:
    return service.archive(profile_id)


@router.get("/{profile_id}/models", response_model=ProviderModelsResponse)
async def discover_models(
    profile_id: str,
    service: ProviderService = Depends(provider_service),
) -> ProviderModelsResponse:
    return await service.discover(profile_id)


@router.post("/{profile_id}/verify", response_model=ProviderVerifyResponse)
async def verify_provider(
    profile_id: str,
    payload: ProviderVerifyRequest,
    service: ProviderService = Depends(provider_service),
) -> ProviderVerifyResponse:
    return await service.verify(profile_id, model_name=payload.model)
