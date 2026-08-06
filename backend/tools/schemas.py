from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class FunctionToolSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FunctionToolWriteRequest(FunctionToolSchema):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    description: str = Field(default="", max_length=4_000)
    parameters_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    code: str = Field(min_length=1, max_length=100_000)
    requires_approval: bool = False


class FunctionToolTestRequest(FunctionToolSchema):
    definition: FunctionToolWriteRequest
    arguments: dict[str, Any] = Field(default_factory=dict)


class FunctionToolTestResponse(FunctionToolSchema):
    output: Any
    stdout: str


class FunctionToolRevisionResponse(FunctionToolSchema):
    id: str
    definition_id: str
    revision: int
    description: str
    parameters_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    code: str
    requires_approval: bool
    catalog_id: str
    created_at: datetime


class FunctionToolResponse(FunctionToolSchema):
    id: str
    name: str
    description: str
    archived: bool
    created_at: datetime
    updated_at: datetime
    latest_revision: FunctionToolRevisionResponse
