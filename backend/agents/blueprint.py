from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.providers.reasoning import ReasoningEffort
from backend.agents.sdk import SUPPORTED_SDK_VERSION

Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")]


class BlueprintModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelReferenceSpec(BlueprintModel):
    provider_profile_id: str | None = None
    model: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def require_complete_reference(self) -> "ModelReferenceSpec":
        if self.provider_profile_id and not self.model:
            raise ValueError("A provider profile reference requires a model name.")
        return self


class ReasoningSpec(BlueprintModel):
    effort: ReasoningEffort | None = None


class ModelSettingsSpec(BlueprintModel):
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    frequency_penalty: float | None = Field(default=None, ge=-2, le=2)
    presence_penalty: float | None = Field(default=None, ge=-2, le=2)
    tool_choice: Literal["auto", "required", "none"] | str | None = None
    parallel_tool_calls: bool | None = None
    truncation: Literal["auto", "disabled"] | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    reasoning: ReasoningSpec | None = None
    verbosity: Literal["low", "medium", "high"] | None = None


class JsonOutputSpec(BlueprintModel):
    kind: Literal["json_schema"] = "json_schema"
    name: Identifier
    schema_: dict[str, Any] = Field(alias="schema")
    strict: bool = True


OutputSpec = Annotated[JsonOutputSpec, Field(discriminator="kind")]


class FunctionToolSpec(BlueprintModel):
    id: Identifier
    kind: Literal["function"] = "function"
    catalog_id: str = Field(min_length=1, max_length=255)
    name: Identifier | None = None
    description: str | None = Field(default=None, max_length=2_000)
    config: dict[str, Any] = Field(default_factory=dict)
    needs_approval: bool = False
    input_guardrail_ids: list[Identifier] = Field(default_factory=list, max_length=50)
    output_guardrail_ids: list[Identifier] = Field(default_factory=list, max_length=50)


class WebSearchToolSpec(BlueprintModel):
    id: Identifier
    kind: Literal["web_search"] = "web_search"
    search_context_size: Literal["low", "medium", "high"] = "medium"
    external_web_access: bool | None = None


class FileSearchToolSpec(BlueprintModel):
    id: Identifier
    kind: Literal["file_search"] = "file_search"
    vector_store_ids: list[str] = Field(min_length=1, max_length=20)
    max_num_results: int | None = Field(default=None, ge=1, le=100)
    include_search_results: bool = False


ToolSpec = Annotated[
    FunctionToolSpec | WebSearchToolSpec | FileSearchToolSpec,
    Field(discriminator="kind"),
]


class GuardrailSpec(BlueprintModel):
    id: Identifier
    kind: Literal["input", "output", "tool_input", "tool_output"]
    catalog_id: str = Field(min_length=1, max_length=255)
    config: dict[str, Any] = Field(default_factory=dict)


class AgentSpec(BlueprintModel):
    id: Identifier
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4_000)
    instructions: str = Field(min_length=1, max_length=100_000)
    model: ModelReferenceSpec = Field(default_factory=ModelReferenceSpec)
    model_settings: ModelSettingsSpec = Field(default_factory=ModelSettingsSpec)
    output: OutputSpec | None = None
    tool_ids: list[Identifier] = Field(default_factory=list, max_length=100)
    input_guardrail_ids: list[Identifier] = Field(default_factory=list, max_length=50)
    output_guardrail_ids: list[Identifier] = Field(default_factory=list, max_length=50)
    tool_use_behavior: Literal["run_llm_again", "stop_on_first_tool"] = "run_llm_again"
    reset_tool_choice: bool = True


class HandoffSpec(BlueprintModel):
    id: Identifier
    source_agent_id: Identifier
    target_agent_id: Identifier
    tool_name: Identifier | None = None
    tool_description: str | None = Field(default=None, max_length=2_000)
    nest_handoff_history: bool | None = None


class AgentToolSpec(BlueprintModel):
    id: Identifier
    owner_agent_id: Identifier
    delegate_agent_id: Identifier
    tool_name: Identifier
    tool_description: str = Field(min_length=1, max_length=2_000)
    max_turns: int | None = Field(default=None, ge=1, le=100)
    needs_approval: bool = False
    serialize_calls: bool = False
    max_parallel_calls: int | None = Field(default=None, ge=2, le=64)

    @model_validator(mode="after")
    def validate_concurrency_policy(self) -> "AgentToolSpec":
        if self.serialize_calls and self.max_parallel_calls is not None:
            raise ValueError("An agent tool cannot be both serialized and parallel.")
        return self


class RunSettingsSpec(BlueprintModel):
    max_turns: int = Field(default=10, ge=1, le=100)
    max_tool_concurrency: int | None = Field(default=None, ge=1, le=64)
    tracing_enabled: bool = False
    exclusive_inference: bool = Field(
        default=False,
        exclude_if=lambda value: not value,
    )


class SessionPolicySpec(BlueprintModel):
    model_config = ConfigDict(extra="ignore")

    history_max_items: int | None = Field(default=None, ge=1, le=10_000)
    messages_only: bool = False


class AgentBlueprint(BlueprintModel):
    schema_version: Literal[1] = 1
    sdk_version: Literal[SUPPORTED_SDK_VERSION] = SUPPORTED_SDK_VERSION
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4_000)
    entry_agent_id: Identifier
    agents: list[AgentSpec] = Field(min_length=1, max_length=100)
    tools: list[ToolSpec] = Field(default_factory=list, max_length=200)
    handoffs: list[HandoffSpec] = Field(default_factory=list, max_length=200)
    agent_tools: list[AgentToolSpec] = Field(default_factory=list, max_length=200)
    guardrails: list[GuardrailSpec] = Field(default_factory=list, max_length=100)
    run: RunSettingsSpec = Field(default_factory=RunSettingsSpec)
    session: SessionPolicySpec = Field(default_factory=SessionPolicySpec)
