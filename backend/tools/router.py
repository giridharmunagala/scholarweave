from __future__ import annotations

from fastapi import APIRouter, Depends, status

from backend.api.dependencies import services
from backend.tools.schemas import (
    FunctionToolResponse,
    FunctionToolRevisionResponse,
    FunctionToolTestRequest,
    FunctionToolTestResponse,
    FunctionToolWriteRequest,
)
from backend.tools.service import FunctionToolDocument, FunctionToolService

router = APIRouter(prefix="/tools", tags=["tools"])


def tool_service(container=Depends(services)) -> FunctionToolService:
    return container.function_tools


@router.get("", response_model=list[FunctionToolResponse])
def list_tools(
    service: FunctionToolService = Depends(tool_service),
) -> list[FunctionToolResponse]:
    return [_response(document) for document in service.list()]


@router.post("", response_model=FunctionToolResponse, status_code=status.HTTP_201_CREATED)
def create_tool(
    payload: FunctionToolWriteRequest,
    service: FunctionToolService = Depends(tool_service),
) -> FunctionToolResponse:
    return _response(service.create(**payload.model_dump()))


@router.post("/test", response_model=FunctionToolTestResponse)
async def test_tool(
    payload: FunctionToolTestRequest,
    service: FunctionToolService = Depends(tool_service),
) -> FunctionToolTestResponse:
    result = await service.test(
        **payload.definition.model_dump(exclude={"name", "description", "requires_approval"}),
        arguments=payload.arguments,
    )
    return FunctionToolTestResponse(**result)


@router.get("/{definition_id}", response_model=FunctionToolResponse)
def get_tool(
    definition_id: str,
    service: FunctionToolService = Depends(tool_service),
) -> FunctionToolResponse:
    return _response(service.get(definition_id))


@router.put("/{definition_id}", response_model=FunctionToolResponse)
def update_tool(
    definition_id: str,
    payload: FunctionToolWriteRequest,
    service: FunctionToolService = Depends(tool_service),
) -> FunctionToolResponse:
    return _response(service.update(definition_id, **payload.model_dump()))


@router.post("/{definition_id}/archive", response_model=FunctionToolResponse)
def archive_tool(
    definition_id: str,
    service: FunctionToolService = Depends(tool_service),
) -> FunctionToolResponse:
    return _response(service.archive(definition_id))


def _response(document: FunctionToolDocument) -> FunctionToolResponse:
    record = document.record
    revision = document.latest_revision
    return FunctionToolResponse(
        id=record.id,
        name=record.name,
        description=record.description,
        archived=record.archived,
        created_at=record.created_at,
        updated_at=record.updated_at,
        latest_revision=FunctionToolRevisionResponse(
            id=revision.id,
            definition_id=revision.definition_id,
            revision=revision.revision,
            description=revision.description,
            parameters_schema=revision.parameters_schema_json,
            output_schema=revision.output_schema_json,
            code=revision.code,
            requires_approval=revision.requires_approval,
            catalog_id=f"custom:{revision.id}",
            created_at=revision.created_at,
        ),
    )
