from __future__ import annotations

from typing import Literal
import csv
import io

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, StreamingResponse, Response

from backend.core.http import services
from backend.research.reviews.schemas import CandidatePage, PaperProgressPage, PaperReview, ReviewClearRequest, ReviewClearResponse, ReviewControlRequest, ReviewCreateRequest, ReviewResponse, ReviewReference
from backend.research.reviews.schemas import AnalysisProgress, ReviewActivityPage, ReviewActivityDetail

router = APIRouter(prefix="/literature-reviews", tags=["literature reviews"])


@router.get("", response_model=list[ReviewResponse])
def list_reviews(
    conversation_id: str | None = None, track_id: str | None = None, container=Depends(services),
) -> list[ReviewResponse]:
    return container.reviews.list(conversation_id=conversation_id, track_id=track_id)


@router.post("", response_model=ReviewResponse, status_code=202)
async def create_review(payload: ReviewCreateRequest, container=Depends(services)) -> ReviewResponse:
    if payload.conversation_id:
        container.conversations.get(payload.conversation_id)
    return await container.reviews.create(payload)


@router.get("/{review_id}", response_model=ReviewResponse)
def get_review(review_id: str, container=Depends(services)) -> ReviewResponse:
    return container.reviews.get(review_id)


@router.get("/{review_id}/candidates", response_model=CandidatePage)
def review_candidates(
    review_id: str, limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0), container=Depends(services),
    sort: Literal["relevance", "year", "title"] = "relevance",
) -> CandidatePage:
    return container.reviews.candidates(review_id, limit=limit, offset=offset, sort=sort)


@router.get("/{review_id}/events")
async def review_events(review_id: str, container=Depends(services)) -> StreamingResponse:
    container.reviews.get(review_id)

    async def events():
        async for review in container.reviews.progress_events(review_id):
            yield f"event: review.progress\ndata: {review.model_dump_json()}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/{review_id}/references", response_model=list[ReviewReference])
def review_references(review_id: str, container=Depends(services)) -> list[ReviewReference]:
    return container.reviews.references(review_id)


@router.get("/{review_id}/references.csv")
def export_references(review_id: str, container=Depends(services)) -> Response:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "title", "authors", "year", "doi", "arxiv_id", "citing_count", "citing_paper_ids", "screening", "document_id", "error"])
    for row in container.reviews.references(review_id):
        cells = [row.id, row.title, "; ".join(row.authors), row.year, row.doi, row.arxiv_id, row.citing_count, "; ".join(row.citing_paper_ids),
                 row.screening.relevance if row.screening else None, row.document_id, row.error]
        writer.writerow(["'" + cell if isinstance(cell, str) and cell.lstrip().startswith(("=", "+", "-", "@")) else cell for cell in cells])
    return Response(output.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="literature-references.csv"'})


@router.get("/{review_id}/papers/{candidate_id}", response_model=PaperReview)
def paper_review(review_id: str, candidate_id: str, container=Depends(services)) -> PaperReview:
    return container.reviews.paper(review_id, candidate_id)


@router.get("/{review_id}/paper-progress", response_model=PaperProgressPage)
def paper_progress(
    review_id: str, limit: int = Query(default=25, ge=1, le=100), offset: int = Query(default=0, ge=0),
    summaries_only: bool = True, container=Depends(services),
) -> PaperProgressPage:
    return container.reviews.paper_progress(review_id, limit=limit, offset=offset, summaries_only=summaries_only)


@router.get("/{review_id}/papers", response_model=list[PaperReview])
def review_papers(review_id: str, container=Depends(services)) -> list[PaperReview]:
    return container.reviews.papers(review_id)


@router.get("/{review_id}/analysis", response_model=AnalysisProgress | None)
def review_analysis(
    review_id: str, limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0), container=Depends(services),
) -> AnalysisProgress | None:
    return container.reviews.analysis_progress(review_id, limit=limit, offset=offset)


@router.get("/{review_id}/activity", response_model=ReviewActivityPage)
def review_activity(
    review_id: str, limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0), container=Depends(services),
) -> ReviewActivityPage:
    return container.reviews.activity(review_id, limit=limit, offset=offset)


@router.get("/{review_id}/activity/detail", response_model=ReviewActivityDetail)
def review_activity_detail(review_id: str, key: str, container=Depends(services)) -> ReviewActivityDetail:
    return container.reviews.activity_detail(review_id, key)


@router.post("/{review_id}/control", response_model=ReviewResponse)
async def control_review(
    review_id: str, payload: ReviewControlRequest, container=Depends(services),
) -> ReviewResponse:
    return await container.reviews.control(review_id, payload)


@router.get("/{review_id}/export")
def export_review(review_id: str, container=Depends(services)) -> JSONResponse:
    record = container.reviews.export(review_id)
    return JSONResponse(
        record, headers={"Content-Disposition": f'attachment; filename="literature-review-{record["review"]["id"]}.json"'},
    )


@router.post("/{review_id}/clear", response_model=ReviewClearResponse)
async def clear_review(review_id: str, payload: ReviewClearRequest, container=Depends(services)) -> ReviewClearResponse:
    return await container.reviews.clear(review_id, payload.expected_revision)
