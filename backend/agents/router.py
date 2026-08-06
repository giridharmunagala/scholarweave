from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from backend.agents.blueprint import AgentBlueprint
from backend.agents.export import export_agent
from backend.agents.schemas import (
    AgentResponse,
    AgentRevisionResponse,
    AgentValidationResponse,
    AgentWriteRequest,
    FunctionToolCatalogEntry,
    GuardrailCatalogEntry,
    PrimitiveCatalogEntry,
    PythonExportResponse,
    SdkCatalogResponse,
)
from backend.agents.service import AgentDocument, AgentService
from backend.agents.templates import starter_blueprints
from backend.api.dependencies import services
from backend.runtime.sdk_compat import SUPPORTED_SDK_VERSION

router = APIRouter(tags=["agents"])


def agent_service(container=Depends(services)) -> AgentService:
    return container.agents


@router.get("/sdk/catalog", response_model=SdkCatalogResponse)
def sdk_catalog(container=Depends(services)) -> SdkCatalogResponse:
    primitives = [
        ("agent", "Agent", "An autonomous SDK agent."),
        ("function_tool", "FunctionTool", "A typed callable capability."),
        ("agent_tool", "Agent.as_tool", "An agent exposed as another agent's tool."),
        ("handoff", "handoff", "Transfer control to another agent."),
        ("input_guardrail", "InputGuardrail", "Validate run input."),
        ("output_guardrail", "OutputGuardrail", "Validate final output."),
        ("structured_output", "Agent.output_type", "Validate structured agent output."),
        ("model_settings", "ModelSettings", "Per-agent model behavior."),
        ("run_config", "RunConfig", "Runner-level execution behavior."),
        ("session", "Session", "SDK-managed conversation history."),
    ]
    return SdkCatalogResponse(
        sdk_version=SUPPORTED_SDK_VERSION,
        primitives=[
            PrimitiveCatalogEntry(kind=kind, sdk_constructor=constructor, description=description)
            for kind, constructor, description in primitives
        ],
        function_tools=[
            FunctionToolCatalogEntry(
                catalog_id=definition.catalog_id,
                name=definition.name or definition.catalog_id,
                label=definition.label,
                description=definition.description,
                parameters_schema=definition.parameters_schema,
            )
            for definition in container.tool_catalog.definitions()
        ],
        guardrails=[
            GuardrailCatalogEntry(
                catalog_id=definition.catalog_id,
                kind=definition.kind,
                label=definition.label,
                description=definition.description,
                config_schema=definition.config_schema,
            )
            for definition in container.guardrail_catalog.definitions()
        ],
    )


@router.get("/agents/templates", response_model=list[AgentBlueprint])
def templates() -> list[AgentBlueprint]:
    return starter_blueprints()


@router.get("/agents", response_model=list[AgentResponse])
def list_agents(service: AgentService = Depends(agent_service)) -> list[AgentResponse]:
    return [_agent_response(document) for document in service.list()]


@router.post("/agents/validate", response_model=AgentValidationResponse)
def validate_agent(
    blueprint: AgentBlueprint,
    service: AgentService = Depends(agent_service),
) -> AgentValidationResponse:
    issues = service.validate(blueprint)
    return AgentValidationResponse(valid=not issues, issues=list(issues))


@router.post("/agents/export/python", response_model=PythonExportResponse)
def export_python(
    blueprint: AgentBlueprint,
    container=Depends(services),
) -> PythonExportResponse:
    container.compiler.compile(blueprint)
    return PythonExportResponse(
        filename=f"{blueprint.name.lower().replace(' ', '_')}.py",
        source=export_agent(
            blueprint,
            tool_catalog=container.tool_catalog,
            guardrail_catalog=container.guardrail_catalog,
        ),
    )


@router.post("/agents", response_model=AgentResponse, status_code=status.HTTP_201_CREATED)
def create_agent(
    payload: AgentWriteRequest,
    service: AgentService = Depends(agent_service),
) -> AgentResponse:
    return _agent_response(
        service.create(payload.blueprint, presentation=payload.presentation)
    )


@router.get("/agents/{agent_id}", response_model=AgentResponse)
def get_agent(
    agent_id: str,
    service: AgentService = Depends(agent_service),
) -> AgentResponse:
    return _agent_response(service.get(agent_id))


@router.put("/agents/{agent_id}", response_model=AgentResponse)
def update_agent(
    agent_id: str,
    payload: AgentWriteRequest,
    service: AgentService = Depends(agent_service),
) -> AgentResponse:
    return _agent_response(
        service.update(agent_id, payload.blueprint, presentation=payload.presentation)
    )


@router.get("/agents/{agent_id}/revisions", response_model=list[AgentRevisionResponse])
def list_revisions(
    agent_id: str,
    service: AgentService = Depends(agent_service),
) -> list[AgentRevisionResponse]:
    return [_revision_response(revision) for revision in service.list_revisions(agent_id)]


@router.delete("/agents/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_agent(
    agent_id: str,
    service: AgentService = Depends(agent_service),
) -> Response:
    service.delete(agent_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _agent_response(document: AgentDocument) -> AgentResponse:
    record = document.record
    return AgentResponse(
        id=record.id,
        name=record.name,
        description=record.description,
        is_template=record.is_template,
        created_at=record.created_at,
        updated_at=record.updated_at,
        latest_revision=_revision_response(document.latest_revision),
    )


def _revision_response(revision) -> AgentRevisionResponse:
    return AgentRevisionResponse(
        id=revision.id,
        agent_id=revision.agent_id,
        revision=revision.revision,
        blueprint=AgentBlueprint.model_validate(revision.blueprint_json),
        presentation=revision.presentation_json or {},
        sdk_version=revision.sdk_version,
        created_at=revision.created_at,
    )
