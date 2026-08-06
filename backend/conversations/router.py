from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from backend.agents.blueprint import AgentBlueprint, ModelReferenceSpec, SessionPolicySpec
from backend.api.dependencies import services
from backend.builder.service import builder_blueprint
from backend.conversations.schemas import (
    BuilderConversationCreateRequest,
    ConversationCreateRequest,
    ConversationDetailResponse,
    ConversationMessageRequest,
    ConversationMessageResponse,
    ConversationResponse,
)
from backend.runs.schemas import run_response

router = APIRouter(tags=["conversations"])


@router.get("/conversations", response_model=list[ConversationResponse])
def list_conversations(container=Depends(services)) -> list[ConversationResponse]:
    return [_response(record) for record in container.conversations.list()]


@router.post(
    "/conversations",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_conversation(
    payload: ConversationCreateRequest,
    container=Depends(services),
) -> ConversationResponse:
    revision, blueprint = container.agents.get_revision(payload.agent_revision_id)
    entry = next(agent for agent in blueprint.agents if agent.id == blueprint.entry_agent_id)
    record = container.conversations.create(
        title=payload.title,
        kind="agent",
        agent_revision_id=revision.id,
        model_reference=entry.model.model_dump(mode="json"),
        session_policy=blueprint.session,
    )
    return _response(record)


@router.get("/conversations/{conversation_id}", response_model=ConversationDetailResponse)
async def get_conversation(
    conversation_id: str,
    container=Depends(services),
) -> ConversationDetailResponse:
    record = container.conversations.get(conversation_id)
    compiled = _compile_conversation(record, container)
    primary = compiled.resolved_models[compiled.blueprint.entry_agent_id]
    items = await container.conversations.items(record.id, primary)
    return ConversationDetailResponse(**_response(record).model_dump(), items=items)


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=ConversationMessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def send_conversation_message(
    conversation_id: str,
    payload: ConversationMessageRequest,
    container=Depends(services),
) -> ConversationMessageResponse:
    record = container.conversations.get(conversation_id)
    compiled = _compile_conversation(record, container)
    container.conversations.touch(conversation_id, payload.content)
    run = container.runs.create(
        compiled,
        payload.content,
        agent_revision_id=record.agent_revision_id,
        conversation_id=conversation_id,
    )
    return ConversationMessageResponse(
        conversation=_response(container.conversations.get(conversation_id)),
        run=run_response(container.runs.get(run.id)),
    )


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: str,
    container=Depends(services),
) -> Response:
    record = container.conversations.get(conversation_id)
    compiled = _compile_conversation(record, container)
    primary = compiled.resolved_models[compiled.blueprint.entry_agent_id]
    await container.conversations.delete(conversation_id, primary)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/builder/conversations", response_model=list[ConversationResponse])
def list_builder_conversations(container=Depends(services)) -> list[ConversationResponse]:
    return [_response(record) for record in container.builder.list_conversations()]


@router.post(
    "/builder/conversations",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_builder_conversation(
    payload: BuilderConversationCreateRequest,
    container=Depends(services),
) -> ConversationResponse:
    return _response(
        container.builder.create_conversation(
            title=payload.title,
            model_reference=payload.model_reference,
        )
    )


@router.get(
    "/builder/conversations/{conversation_id}",
    response_model=ConversationDetailResponse,
)
async def get_builder_conversation(
    conversation_id: str,
    container=Depends(services),
) -> ConversationDetailResponse:
    record = container.builder.get_conversation(conversation_id)
    if record.kind != "builder":
        raise ValueError("Conversation is not a builder chat.")
    items = await container.builder.conversation_items(conversation_id)
    return ConversationDetailResponse(**_response(record).model_dump(), items=items)


@router.post(
    "/builder/conversations/{conversation_id}/messages",
    response_model=ConversationMessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def send_builder_message(
    conversation_id: str,
    payload: ConversationMessageRequest,
    container=Depends(services),
) -> ConversationMessageResponse:
    record = container.builder.get_conversation(conversation_id)
    if record.kind != "builder":
        raise ValueError("Conversation is not a builder chat.")
    run = container.builder.start_message(conversation_id, payload.content)
    return ConversationMessageResponse(
        conversation=_response(container.builder.get_conversation(conversation_id)),
        run=run_response(container.runs.get(run.id)),
    )


def _compile_conversation(record, container):
    if record.kind == "builder":
        return container.compiler.compile(builder_blueprint(record.model_reference_json))
    if record.agent_revision_id is None:
        raise ValueError("Agent conversation has no agent revision.")
    return container.agents.compile_revision(record.agent_revision_id)


def _response(record) -> ConversationResponse:
    return ConversationResponse(
        id=record.id,
        title=record.title,
        kind=record.kind,
        agent_revision_id=record.agent_revision_id,
        model_reference=ModelReferenceSpec.model_validate(record.model_reference_json or {}),
        session_policy=SessionPolicySpec.model_validate(record.session_policy_json or {}),
        status=record.status,
        last_message_preview=record.last_message_preview,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )
