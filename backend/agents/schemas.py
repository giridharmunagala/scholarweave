from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.agents.blueprint import AgentBlueprint


class AgentSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentWriteRequest(AgentSchema):
    blueprint: AgentBlueprint
    presentation: dict[str, Any] = Field(default_factory=dict)


class AgentValidationResponse(AgentSchema):
    valid: bool
    issues: list[str] = Field(default_factory=list)


class AgentRevisionResponse(AgentSchema):
    id: str
    agent_id: str
    revision: int
    blueprint: AgentBlueprint
    presentation: dict[str, Any]
    sdk_version: str
    created_at: datetime


class AgentResponse(AgentSchema):
    id: str
    name: str
    description: str | None
    is_template: bool
    created_at: datetime
    updated_at: datetime
    latest_revision: AgentRevisionResponse


class PythonExportResponse(AgentSchema):
    filename: str
    source: str


class PrimitiveCatalogEntry(AgentSchema):
    kind: str
    sdk_constructor: str
    description: str


class FunctionToolCatalogEntry(AgentSchema):
    catalog_id: str
    name: str
    label: str
    description: str
    parameters_schema: dict[str, Any] | None


class GuardrailCatalogEntry(AgentSchema):
    catalog_id: str
    kind: str
    label: str
    description: str
    config_schema: dict[str, Any]


class SdkCatalogResponse(AgentSchema):
    sdk_version: str
    primitives: list[PrimitiveCatalogEntry]
    function_tools: list[FunctionToolCatalogEntry]
    guardrails: list[GuardrailCatalogEntry]
