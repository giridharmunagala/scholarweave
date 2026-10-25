from fastapi import APIRouter, Depends

from backend.core.http import services
from backend.references.schemas import (
    Reference, ReferenceExport, ReferenceExportResult, ReferenceImport, ReferenceLibrary, ReferenceWrite,
)

router = APIRouter(prefix="/references", tags=["references"])


@router.get("", response_model=ReferenceLibrary)
def list_references(container=Depends(services)):
    return container.references.list()


@router.post("", response_model=ReferenceLibrary, status_code=201)
def create_reference(payload: ReferenceWrite, container=Depends(services)):
    return container.references.create(payload.reference, expected_revision=payload.expected_revision)


@router.post("/import", response_model=ReferenceLibrary)
def import_references(payload: ReferenceImport, container=Depends(services)):
    return container.references.import_references(
        payload.content, payload.format, expected_revision=payload.expected_revision,
    )


@router.post("/export", response_model=ReferenceExportResult)
def export_references(payload: ReferenceExport, container=Depends(services)):
    return ReferenceExportResult(content=container.references.export(payload.format, payload.reference_ids))


@router.get("/{reference_id}", response_model=Reference)
def get_reference(reference_id: str, container=Depends(services)):
    return container.references.get(reference_id)


@router.put("/{reference_id}", response_model=ReferenceLibrary)
def update_reference(reference_id: str, payload: ReferenceWrite, container=Depends(services)):
    return container.references.update(reference_id, payload.reference, expected_revision=payload.expected_revision)
