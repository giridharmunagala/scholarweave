from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.prompting.skills import MAX_SKILL_BYTES, SKILL_NAME_PATTERN


class SkillResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    content: str
    source: Literal["bundled", "local"]
    revision: str


class SkillWriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=MAX_SKILL_BYTES)
    expected_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
