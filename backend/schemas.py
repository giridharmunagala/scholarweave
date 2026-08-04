from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, JsonValue, model_validator


class HealthResponse(BaseModel):
    status: str
    database_path: str
    data_dir: str
    frontend_available: bool
    ocr_available: bool


class SettingsResponse(BaseModel):
    ollama_base_url: str
    default_generation_model: str | None
    default_embedding_model: str | None
    default_model_references: dict[str, ModelReference] = Field(default_factory=dict)
    data_dir: str
    workspace_dir: str
    artifacts_dir: str
    documents_dir: str
    database_path: str
    request_timeout_seconds: float
    max_upload_bytes: int
    max_workspace_file_bytes: int
    max_artifact_bytes: int
    max_context_chars: int
    max_chunk_chars: int
    max_chunks_per_document: int
    max_map_items: int
    max_repeat_iterations: int
    max_concurrent_nodes: int
    max_subworkflow_depth: int
    pdf_min_text_chars: int
    ocr_language: str
    ocr_llm_enhancement_enabled: bool
    ocr_llm_model: str | None
    ocr_llm_triage_model: str | None
    agent_provider: str
    openai_api_key_set: bool
    openai_base_url: str | None
    agent_max_turns: int
    agent_tracing_enabled: bool
    python_node_enabled: bool
    python_node_timeout_seconds: float
    python_node_memory_mb: int
    python_node_allowed_imports: list[str]


class SettingsUpdate(BaseModel):
    ollama_base_url: str | None = None
    default_generation_model: str | None = None
    default_embedding_model: str | None = None
    default_model_references: dict[str, ModelReference] | None = None
    request_timeout_seconds: float | None = None
    max_context_chars: int | None = None
    max_chunk_chars: int | None = None
    max_map_items: int | None = None
    max_repeat_iterations: int | None = None
    max_concurrent_nodes: int | None = None
    max_subworkflow_depth: int | None = None
    ocr_llm_enhancement_enabled: bool | None = None
    ocr_llm_model: str | None = None
    ocr_llm_triage_model: str | None = None
    agent_provider: Literal["ollama", "openai"] | None = None
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    agent_max_turns: int | None = Field(default=None, ge=1, le=100)
    agent_tracing_enabled: bool | None = None
    python_node_enabled: bool | None = None
    python_node_timeout_seconds: float | None = Field(default=None, gt=0, le=300)
    python_node_memory_mb: int | None = Field(default=None, ge=32, le=8192)
    python_node_allowed_imports: list[str] | None = None


class ProviderCheckResponse(BaseModel):
    """Result of a live round trip against the configured agent provider."""

    provider: str
    base_url: str
    model: str | None
    reachable: bool
    tool_calling: bool
    detail: str | None = None


ProviderKind = Literal["ollama", "openai", "azure_openai", "azure_foundry", "openai_compatible"]
ModelCapability = Literal["chat", "embedding", "vision", "tools"]


class ModelReference(BaseModel):
    """A model name plus the profile that owns it; neither field alone is a secret."""

    provider_profile_id: str | None = None
    model: str | None = None


class WorkflowModelDefaults(BaseModel):
    chat: ModelReference | None = None
    embedding: ModelReference | None = None
    vision: ModelReference | None = None
    tools: ModelReference | None = None


class ProviderModelEntry(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    capabilities: set[ModelCapability] = Field(default_factory=set)


class ProviderProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: ProviderKind
    base_url: str = Field(min_length=1, max_length=1024)
    api_version: str | None = Field(default=None, max_length=128)
    api_key: str | None = Field(default=None, max_length=16_384)
    models: list[ProviderModelEntry] = Field(default_factory=list)


class ProviderProfileUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    kind: ProviderKind | None = None
    base_url: str | None = Field(default=None, min_length=1, max_length=1024)
    api_version: str | None = Field(default=None, max_length=128)
    api_key: str | None = Field(default=None, max_length=16_384)
    models: list[ProviderModelEntry] | None = None


class ProviderProfileResponse(BaseModel):
    id: str
    name: str
    kind: ProviderKind
    base_url: str
    api_version: str | None
    api_key_set: bool
    state: Literal["active", "archived"]
    models: list[ProviderModelEntry] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class ProviderModelsResponse(BaseModel):
    models: list[ProviderModelEntry] = Field(default_factory=list)
    discovery_error: str | None = None


class ProviderVerifyRequest(BaseModel):
    model: str | None = None


class PythonExportResponse(BaseModel):
    filename: str
    source: str


class ModelInfo(BaseModel):
    name: str
    digest: str | None = None
    size: int | None = None
    modified_at: datetime | None = None


class ArtifactResponse(BaseModel):
    id: str
    document_id: str | None
    run_id: str | None
    owner_type: str
    kind: str
    relative_path: str
    media_type: str
    size_bytes: int
    sha256: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class DocumentChunkResponse(BaseModel):
    id: str
    chunk_index: int
    section_title: str | None
    page_start: int
    page_end: int
    citation: str
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentResponse(BaseModel):
    id: str
    title: str
    source_filename: str
    content_type: str
    status: str
    page_count: int | None
    metadata: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[ArtifactResponse] = Field(default_factory=list)
    chunks: list[DocumentChunkResponse] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class WorkspaceNoteResponse(BaseModel):
    path: str
    name: str
    size_bytes: int
    modified_at: datetime


class WorkspaceNoteContentResponse(WorkspaceNoteResponse):
    content: str


class PortDefinitionResponse(BaseModel):
    name: str
    kind: str
    item_kind: str | None = None
    description: str = ""
    required: bool = True
    fan_in: bool = False


class NodeDefinitionResponse(BaseModel):
    type: str
    label: str
    description: str
    category: str
    tags: list[str] = Field(default_factory=list)
    inputs: list[PortDefinitionResponse] = Field(default_factory=list)
    outputs: list[PortDefinitionResponse] = Field(default_factory=list)
    config_schema: dict[str, Any] = Field(default_factory=dict)
    interface_role: Literal["input", "output"] | None = None


CustomPortKind = Literal["any", "text", "number", "json", "list"]
CustomConfigKind = Literal["text", "number", "integer", "boolean", "json", "select"]


class CustomNodePort(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    kind: CustomPortKind = "any"
    item_kind: CustomPortKind | None = None
    description: str = Field(default="", max_length=2_000)
    required: bool = True


class CustomNodeConfigField(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    label: str = Field(default="", max_length=255)
    description: str = Field(default="", max_length=2_000)
    kind: CustomConfigKind = "text"
    required: bool = False
    default: Any = None
    options: list[Any] = Field(default_factory=list, max_length=200)


class CustomNodeSpec(BaseModel):
    label: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=10_000)
    category: str = Field(default="custom", min_length=1, max_length=120)
    tags: list[str] = Field(default_factory=list, max_length=50)
    inputs: list[CustomNodePort] = Field(default_factory=list, max_length=100)
    outputs: list[CustomNodePort] = Field(min_length=1, max_length=100)
    config_fields: list[CustomNodeConfigField] = Field(default_factory=list, max_length=100)
    code: str = Field(min_length=1, max_length=40_000)


class CustomNodeCreate(CustomNodeSpec):
    name: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")


class CustomNodeUpdate(CustomNodeSpec):
    name: str | None = Field(default=None, min_length=1, max_length=120, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")


class CustomNodeRevisionResponse(CustomNodeSpec):
    id: str
    definition_id: str
    revision: int
    node_type: str
    config_schema: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class CustomNodeResponse(BaseModel):
    id: str
    name: str
    archived: bool
    archived_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    latest_revision: CustomNodeRevisionResponse
    revisions: list[CustomNodeRevisionResponse] = Field(default_factory=list)


class CustomNodeSampleRequest(BaseModel):
    spec: CustomNodeSpec | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    workflow_inputs: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def accept_flat_authoring_payload(cls, value: Any) -> Any:
        """Also accept ``definition`` or a flattened spec with ``sample_inputs``."""
        if not isinstance(value, dict) or value.get("spec") is not None:
            return value
        source = value.get("definition") if isinstance(value.get("definition"), dict) else value
        keys = {"label", "description", "category", "tags", "inputs", "outputs", "config_fields", "code"}
        if not {"label", "outputs", "code"} <= set(source):
            return value
        return {
            "spec": {key: source[key] for key in keys if key in source},
            "inputs": value.get("sample_inputs", {}),
            "config": value.get("config", {}),
            "workflow_inputs": value.get("workflow_inputs", {}),
        }

    @model_validator(mode="after")
    def require_spec(self) -> "CustomNodeSampleRequest":
        if self.spec is None:
            raise ValueError("spec is required")
        return self


class CustomNodeSampleResponse(BaseModel):
    output: dict[str, Any]
    stdout: str = ""


class WorkflowPort(BaseModel):
    """One entry in a workflow's public interface, declared by an interface node."""

    key: str
    kind: str = "any"
    label: str = ""
    description: str = ""
    required: bool = True
    default: Any = None
    node_id: str = ""
    node_ids: list[str] = Field(default_factory=list)


class WorkflowSignature(BaseModel):
    inputs: list[WorkflowPort] = Field(default_factory=list)
    outputs: list[WorkflowPort] = Field(default_factory=list)


ConditionSource = Literal["workflow", "inputs"]
ConditionOperator = Literal[
    "equals",
    "not_equals",
    "exists",
    "not_exists",
    "truthy",
    "falsy",
    "empty",
    "not_empty",
    "greater_than",
    "greater_than_or_equal",
    "less_than",
    "less_than_or_equal",
    "starts_with",
    "ends_with",
    "contains",
    "not_contains",
    "in",
    "not_in",
]


class ConditionPredicate(BaseModel):
    """One safe, visual-rule predicate; paths only traverse input data."""

    type: Literal["predicate"] = "predicate"
    source: ConditionSource
    path: str = Field(min_length=1, max_length=512, pattern=r"^[^.]+(?:\.[^.]+)*$")
    operator: ConditionOperator
    value: JsonValue = None

    @model_validator(mode="after")
    def validate_operand(self) -> "ConditionPredicate":
        no_operand = {"exists", "not_exists", "truthy", "falsy", "empty", "not_empty"}
        if self.operator not in no_operand and "value" not in self.model_fields_set:
            raise ValueError(f"operator '{self.operator}' requires a value")
        if self.operator in {"in", "not_in"} and not isinstance(self.value, list):
            raise ValueError(f"operator '{self.operator}' requires value to be a list")
        return self


class ConditionGroup(BaseModel):
    """A recursive AND/OR group used by the visual workflow builder."""

    type: Literal["group"] = "group"
    operator: Literal["and", "or"]
    conditions: list["ConditionRule"] = Field(min_length=1, max_length=100)


ConditionRule = Annotated[ConditionGroup | ConditionPredicate, Field(discriminator="type")]
ConditionGroup.model_rebuild()


class WorkflowNode(BaseModel):
    id: str
    type: str
    name: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    static_inputs: dict[str, Any] = Field(default_factory=dict)
    run_when: ConditionRule | None = None


class WorkflowEdge(BaseModel):
    source_node_id: str
    source_port: str
    target_node_id: str
    target_port: str


class WorkflowDefinition(BaseModel):
    name: str
    description: str | None = None
    nodes: list[WorkflowNode]
    edges: list[WorkflowEdge] = Field(default_factory=list)
    model_defaults: WorkflowModelDefaults | None = None


class WorkflowValidationResult(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    order: list[str] = Field(default_factory=list)
    signature: WorkflowSignature = Field(default_factory=WorkflowSignature)


class WorkflowCreateRequest(BaseModel):
    name: str
    description: str | None = None
    definition: WorkflowDefinition
    is_template: bool = False


class WorkflowVersionResponse(BaseModel):
    id: str
    workflow_id: str
    version: int
    definition: WorkflowDefinition
    created_at: datetime


class WorkflowResponse(BaseModel):
    id: str
    name: str
    description: str | None = None
    is_template: bool
    created_at: datetime
    updated_at: datetime
    latest_version: WorkflowVersionResponse | None = None


class RunCreateRequest(BaseModel):
    workflow_version_id: str | None = None
    workflow: WorkflowDefinition | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def ensure_workflow_source(self) -> "RunCreateRequest":
        if not self.workflow_version_id and not self.workflow:
            raise ValueError("workflow_version_id or workflow is required")
        return self


class NodeRunResponse(BaseModel):
    id: str
    run_id: str
    node_path: str
    node_id: str
    node_type: str
    status: str
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class RunResponse(BaseModel):
    id: str
    workflow_version_id: str | None
    workflow_name: str
    status: str
    input: dict[str, Any] = Field(default_factory=dict)
    output: Any | None = None
    error: str | None = None
    cancel_requested: bool
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    total_nodes: int
    node_runs: list[NodeRunResponse] = Field(default_factory=list)


class RunEventResponse(BaseModel):
    id: int
    run_id: str
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ArtifactWriteRequest(BaseModel):
    relative_path: str
    content: Any
    media_type: Literal["text/plain", "text/markdown", "application/json"]


class ArtifactContentResponse(BaseModel):
    artifact: ArtifactResponse
    content: Any
