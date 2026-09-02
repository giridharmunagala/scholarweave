from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import StreamingResponse

from backend.agents.blueprint import AgentBlueprint
from backend.api.dependencies import services
from backend.runs.schemas import (
    InterruptionResolutionRequest,
    RunCreateRequest,
    RunEventResponse,
    RunResponse,
    SteeringMessageRequest,
    SteeringMessageResponse,
    StopAndAnswerResponse,
    run_response,
)
from backend.runs.service import RunService

router = APIRouter(prefix="/runs", tags=["runs"])
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "paused"}


def run_service(container=Depends(services)) -> RunService:
    return container.runs


@router.get("", response_model=list[RunResponse])
def list_runs(
    conversation_id: str | None = Query(default=None),
    service: RunService = Depends(run_service),
) -> list[RunResponse]:
    return [
        run_response(record)
        for record in service.list(conversation_id=conversation_id)
    ]


@router.post("", response_model=RunResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_run(
    payload: RunCreateRequest,
    container=Depends(services),
) -> RunResponse:
    compiled = (
        container.agents.compile_revision(payload.agent_revision_id)
        if payload.agent_revision_id
        else container.compiler.compile(payload.blueprint)
    )
    record = container.runs.create(
        compiled,
        payload.input,
        agent_revision_id=payload.agent_revision_id,
        conversation_id=payload.conversation_id,
        reasoning_effort=payload.reasoning_effort,
    )
    return run_response(container.runs.get(record.id))


@router.get("/{run_id}", response_model=RunResponse)
def get_run(run_id: str, service: RunService = Depends(run_service)) -> RunResponse:
    return run_response(service.get(run_id))


@router.delete("/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_run(
    run_id: str,
    service: RunService = Depends(run_service),
) -> Response:
    service.delete(run_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{run_id}/cancel", response_model=RunResponse)
async def cancel_run(
    run_id: str,
    service: RunService = Depends(run_service),
) -> RunResponse:
    return run_response(await service.cancel(run_id))


@router.post("/{run_id}/stop-and-answer", response_model=StopAndAnswerResponse)
async def stop_and_answer_run(
    run_id: str,
    service: RunService = Depends(run_service),
) -> StopAndAnswerResponse:
    stopped, answer = await service.stop_and_answer(run_id)
    return StopAndAnswerResponse(
        stopped_run=run_response(stopped),
        answer_run=run_response(answer),
    )


@router.post(
    "/{run_id}/steering",
    response_model=SteeringMessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def steer_run(
    run_id: str,
    payload: SteeringMessageRequest,
    container=Depends(services),
) -> SteeringMessageResponse:
    message = await container.runs.steer(run_id, payload.content)
    conversation_id = container.runs.get(run_id).conversation_id
    if conversation_id is not None:
        container.conversations.touch(conversation_id, payload.content)
    return SteeringMessageResponse(
        id=message.id,
        content=message.content,
        status="queued",
    )


@router.post(
    "/{run_id}/interruptions/{interruption_id}",
    response_model=RunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def resolve_interruption(
    run_id: str,
    interruption_id: str,
    payload: InterruptionResolutionRequest,
    container=Depends(services),
) -> RunResponse:
    record = container.runs.get(run_id)
    compiled = container.compiler.compile(
        AgentBlueprint.model_validate(record.blueprint_json)
    )
    updated = await container.runs.resolve_interruption(
        compiled,
        run_id=run_id,
        interruption_id=interruption_id,
        approved=payload.approved,
        rejection_message=payload.rejection_message,
    )
    return run_response(updated)


@router.get("/{run_id}/events")
async def stream_events(
    run_id: str,
    after: int = Query(default=-1, ge=-1),
    container=Depends(services),
) -> StreamingResponse:
    container.runs.get(run_id)

    async def events() -> AsyncIterator[str]:
        cursor = after
        async with container.events.subscribe(run_id) as queue:
            for event in container.runs.events_after(run_id, cursor):
                cursor = max(cursor, event.sequence)
                yield _sse(
                    event.sequence,
                    event.event_type,
                    event.payload_json,
                    event.created_at.isoformat(),
                )
            for event in await container.events.events_after(run_id, cursor):
                sequence = int(event["sequence"])
                if sequence <= cursor:
                    continue
                cursor = sequence
                yield _sse(
                    sequence,
                    event["event_type"],
                    event["payload"],
                    event.get("created_at"),
                )
            while True:
                record = container.runs.get(run_id)
                if record.status in TERMINAL_STATUSES:
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                sequence = int(event["sequence"])
                if sequence <= cursor:
                    continue
                cursor = sequence
                yield _sse(
                    sequence,
                    event["event_type"],
                    event["payload"],
                    event.get("created_at"),
                )

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _sse(
    sequence: int,
    event_type: str,
    payload: dict,
    created_at: str | None = None,
) -> str:
    data = json.dumps(
        {
            "sequence": sequence,
            "event_type": event_type,
            "payload": payload,
            "created_at": created_at,
        },
        ensure_ascii=True,
    )
    return f"id: {sequence}\nevent: {event_type}\ndata: {data}\n\n"
