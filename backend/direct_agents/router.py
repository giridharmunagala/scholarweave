from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from backend.agents.blueprint import ModelReferenceSpec, SessionPolicySpec
from backend.api.dependencies import services
from backend.direct_agents.schemas import (
    DirectAgentResponse,
    DirectConversationCreateRequest,
    DirectConversationDetailResponse,
    DirectConversationMessageRequest,
    DirectConversationMessageResponse,
    DirectConversationResponse,
)
from backend.direct_agents.service import DIRECT_AGENTS
from backend.runs.schemas import run_response

router = APIRouter(tags=["direct research agents"])


@router.get("/research-agents", response_model=list[DirectAgentResponse])
def list_research_agents() -> list[DirectAgentResponse]:
    return [
        DirectAgentResponse(
            key=agent.key,
            name=agent.name,
            description=agent.description,
            requires_document=agent.requires_document,
        )
        for agent in DIRECT_AGENTS
    ]


@router.get(
    "/research-agent-conversations",
    response_model=list[DirectConversationResponse],
)
def list_direct_conversations(container=Depends(services)) -> list[DirectConversationResponse]:
    scopes = {
        scope.conversation_id: scope
        for scope in container.direct_agents.repository.list_conversation_scopes()
    }
    return [
        _response(record, scopes[record.id])
        for record in container.conversations.list(kind="direct_agent")
        if record.id in scopes
    ]


@router.post(
    "/research-agent-conversations",
    response_model=DirectConversationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_direct_conversation(
    payload: DirectConversationCreateRequest,
    container=Depends(services),
) -> DirectConversationResponse:
    record = container.direct_agents.create_conversation(
        agent_key=payload.agent_key,
        document_ids=payload.document_ids,
        title=payload.title,
        model_reference=payload.model_reference,
    )
    scope = container.direct_agents.repository.get_conversation_scope(record.id)
    return _response(record, scope)


@router.get(
    "/research-agent-conversations/{conversation_id}",
    response_model=DirectConversationDetailResponse,
)
async def get_direct_conversation(
    conversation_id: str,
    container=Depends(services),
) -> DirectConversationDetailResponse:
    record = container.conversations.get(conversation_id)
    scope = container.direct_agents.repository.get_conversation_scope(conversation_id)
    items = await container.conversations.items(conversation_id)
    return DirectConversationDetailResponse(
        **_response(record, scope).model_dump(),
        items=items,
    )


@router.post(
    "/research-agent-conversations/{conversation_id}/messages",
    response_model=DirectConversationMessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def send_direct_message(
    conversation_id: str,
    payload: DirectConversationMessageRequest,
    container=Depends(services),
) -> DirectConversationMessageResponse:
    record = container.conversations.get(conversation_id)
    scope = container.direct_agents.repository.get_conversation_scope(conversation_id)
    compiled = container.direct_agents.compile_conversation(conversation_id)
    container.conversations.touch(conversation_id, payload.content)
    run = container.runs.create(
        compiled,
        payload.content,
        agent_revision_id=None,
        conversation_id=conversation_id,
        reasoning_effort=payload.reasoning_effort,
        runtime_metadata={
            "direct_agent_key": scope.agent_key,
            "direct_agent_document_ids": list(scope.document_ids_json or []),
        },
    )
    return DirectConversationMessageResponse(
        conversation=_response(container.conversations.get(conversation_id), scope),
        run=run_response(container.runs.get(run.id)),
    )


@router.delete(
    "/research-agent-conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_direct_conversation(
    conversation_id: str,
    container=Depends(services),
) -> Response:
    await container.runs.cancel_conversation_runs(conversation_id)
    await container.conversations.delete(conversation_id)
    container.direct_agents.repository.delete_conversation_scope(conversation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _response(record, scope) -> DirectConversationResponse:
    return DirectConversationResponse(
        id=record.id,
        title=record.title,
        kind="direct_agent",
        agent_key=scope.agent_key,
        document_ids=list(scope.document_ids_json or []),
        model_reference=ModelReferenceSpec.model_validate(record.model_reference_json or {}),
        session_policy=SessionPolicySpec.model_validate(record.session_policy_json or {}),
        status=record.status,
        last_message_preview=record.last_message_preview,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )
