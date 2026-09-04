from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from backend.agents.blueprint import ModelReferenceSpec, SessionPolicySpec
from backend.core.http import services
from backend.conversations.schemas import (
    ConversationDetailResponse,
    ConversationMessageRequest,
    ConversationMessageResponse,
    ConversationResponse,
    ResearchConversationCreateRequest,
)
from backend.runs.schemas import run_response

router = APIRouter(tags=["research conversations"])


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: str,
    container=Depends(services),
) -> Response:
    await container.runs.cancel_conversation_runs(conversation_id)
    await container.conversations.delete(conversation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/agent/conversations", response_model=list[ConversationResponse])
def list_research_conversations(container=Depends(services)) -> list[ConversationResponse]:
    return [_response(record) for record in container.conversation_turns.list_conversations()]


@router.post(
    "/agent/conversations",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_research_conversation(
    payload: ResearchConversationCreateRequest,
    container=Depends(services),
) -> ConversationResponse:
    return _response(
        container.conversation_turns.create_conversation(
            title=payload.title,
            model_reference=payload.model_reference,
        )
    )


@router.get(
    "/agent/conversations/{conversation_id}",
    response_model=ConversationDetailResponse,
)
async def get_research_conversation(
    conversation_id: str,
    container=Depends(services),
) -> ConversationDetailResponse:
    record = container.conversation_turns.get_conversation(conversation_id)
    items = await container.conversation_turns.conversation_items(conversation_id)
    return ConversationDetailResponse(**_response(record).model_dump(), items=items)


@router.post(
    "/agent/conversations/{conversation_id}/messages",
    response_model=ConversationMessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def send_research_message(
    conversation_id: str,
    payload: ConversationMessageRequest,
    container=Depends(services),
) -> ConversationMessageResponse:
    run = container.conversation_turns.start_message(
        conversation_id,
        payload.content,
        reasoning_effort=payload.reasoning_effort,
        web_enabled=payload.web_enabled,
        deep_work=payload.deep_work,
        fast_answer=payload.fast_answer,
        web_search_limit=payload.web_search_limit,
        context_window_tokens=payload.context_window_tokens,
    )
    return ConversationMessageResponse(
        conversation=_response(container.conversation_turns.get_conversation(conversation_id)),
        run=run_response(container.runs.get(run.id)),
    )


@router.get("/deep-work/conversations", response_model=list[ConversationResponse])
def list_deep_work_conversations(container=Depends(services)) -> list[ConversationResponse]:
    return [
        _response(record)
        for record in container.conversation_turns.list_deep_work_conversations()
    ]


@router.post(
    "/deep-work/conversations",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_deep_work_conversation(
    payload: ResearchConversationCreateRequest,
    container=Depends(services),
) -> ConversationResponse:
    return _response(
        container.conversation_turns.create_deep_work_conversation(
            title=payload.title,
            model_reference=payload.model_reference,
        )
    )


@router.get(
    "/deep-work/conversations/{conversation_id}",
    response_model=ConversationDetailResponse,
)
async def get_deep_work_conversation(
    conversation_id: str,
    container=Depends(services),
) -> ConversationDetailResponse:
    record = container.conversation_turns.get_deep_work_conversation(conversation_id)
    items = await container.conversation_turns.conversation_items(conversation_id)
    return ConversationDetailResponse(**_response(record).model_dump(), items=items)


@router.post(
    "/deep-work/conversations/{conversation_id}/messages",
    response_model=ConversationMessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def send_deep_work_message(
    conversation_id: str,
    payload: ConversationMessageRequest,
    container=Depends(services),
) -> ConversationMessageResponse:
    run = container.conversation_turns.start_deep_work_message(
        conversation_id,
        payload.content,
        reasoning_effort=payload.reasoning_effort,
        web_enabled=payload.web_enabled,
        context_window_tokens=payload.context_window_tokens,
    )
    return ConversationMessageResponse(
        conversation=_response(
            container.conversation_turns.get_deep_work_conversation(conversation_id)
        ),
        run=run_response(container.runs.get(run.id)),
    )


def _response(record) -> ConversationResponse:
    return ConversationResponse(
        id=record.id,
        title=record.title,
        kind=record.kind,
        model_reference=ModelReferenceSpec.model_validate(record.model_reference_json or {}),
        session_policy=SessionPolicySpec.model_validate(record.session_policy_json or {}),
        status=record.status,
        last_message_preview=record.last_message_preview,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )
