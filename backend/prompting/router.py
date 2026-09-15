from __future__ import annotations

from fastapi import APIRouter, Depends

from backend.core.http import services
from backend.prompting.schemas import SkillResponse, SkillWriteRequest
from backend.prompting.skills import SkillDefinition

router = APIRouter(prefix="/skills", tags=["skills"])


def _response(skill: SkillDefinition) -> SkillResponse:
    return SkillResponse(
        name=skill.name, content=skill.instructions, source=skill.source, revision=skill.revision,
    )


@router.get("", response_model=list[SkillResponse])
def list_skills(container=Depends(services)) -> list[SkillResponse]:
    return [_response(skill) for skill in container.skills.list()]


@router.get("/{name}", response_model=SkillResponse)
def read_skill(name: str, container=Depends(services)) -> SkillResponse:
    return _response(container.skills.get(name))


@router.put("/{name}", response_model=SkillResponse)
def save_skill(
    name: str, payload: SkillWriteRequest, container=Depends(services),
) -> SkillResponse:
    return _response(container.skills.save(name, payload.content, payload.expected_revision))
