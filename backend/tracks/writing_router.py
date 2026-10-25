from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response

from backend.bootstrap import ApplicationServices
from backend.core.http import services
from backend.tracks.writing import export_manuscript
from backend.tracks.writing_schemas import (
    ClaimCollection,
    ClaimInput,
    EvidencePageResponse,
    ManuscriptAudit,
    ManuscriptResponse,
    ManuscriptWriteRequest,
    WritingRevisionRequest,
)

router = APIRouter(prefix="/tracks/{track_id}/writing", tags=["writing"])
Container = Annotated[ApplicationServices, Depends(services)]


@router.get("/claims", response_model=ClaimCollection)
def list_claims(track_id: str, container: Container):
    return container.writing.claims(track_id)


@router.post("/claims", response_model=ClaimCollection, status_code=201)
def create_claim(track_id: str, payload: ClaimInput, container: Container):
    return container.writing.save_claim(track_id, payload)


@router.put("/claims/{claim_id}", response_model=ClaimCollection)
def update_claim(track_id: str, claim_id: str, payload: ClaimInput, container: Container):
    return container.writing.save_claim(track_id, payload, claim_id)


@router.delete("/claims/{claim_id}", response_model=ClaimCollection)
def delete_claim(
    track_id: str, claim_id: str, payload: WritingRevisionRequest, container: Container,
):
    return container.writing.delete_claim(track_id, claim_id, payload.expected_sha256)


@router.get("/page", response_model=EvidencePageResponse)
def evidence_page(track_id: str, document_id: str, container: Container, page: int = Query(ge=1)):
    container.tracks.get(track_id)
    return container.writing.page(document_id, page)


@router.get("/manuscript", response_model=ManuscriptResponse)
def read_manuscript(track_id: str, container: Container):
    return container.writing.manuscript(track_id)


@router.put("/manuscript", response_model=ManuscriptResponse)
def write_manuscript(track_id: str, payload: ManuscriptWriteRequest, container: Container):
    return container.writing.save_manuscript(track_id, payload.content, payload.expected_sha256)


@router.post("/audit", response_model=ManuscriptAudit)
def audit_manuscript(track_id: str, payload: ManuscriptWriteRequest, container: Container):
    return container.writing.audit(track_id, payload.content, set(_bibliography(container)))


def _bibliography(container: ApplicationServices) -> dict[str, str]:
    return container.references.bibliography()


@router.get("/export")
def export(
    track_id: str, container: Container, format: Literal["markdown", "latex", "docx"] = "markdown",
):
    content = container.writing.export_text(track_id, _bibliography(container))
    data, media_type, filename = export_manuscript(content, format)
    return Response(data, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{filename}"'})
