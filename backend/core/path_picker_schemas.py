from typing import Literal

from pydantic import BaseModel, ConfigDict


class PathPickerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["file", "folder"]


class PathPickerResponse(BaseModel):
    path: str | None
