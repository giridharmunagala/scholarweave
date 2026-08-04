export interface HealthResponse {
  status: string;
  database_path: string;
  data_dir: string;
  frontend_available: boolean;
  ocr_available: boolean;
}

export interface SettingsResponse {
  ollama_base_url: string;
  default_generation_model: string | null;
  default_embedding_model: string | null;
  default_model_references: Record<string, ModelReference>;
  data_dir: string;
  workspace_dir: string;
  artifacts_dir: string;
  documents_dir: string;
  database_path: string;
  request_timeout_seconds: number;
  max_upload_bytes: number;
  max_workspace_file_bytes: number;
  max_artifact_bytes: number;
  max_context_chars: number;
  max_chunk_chars: number;
  max_chunks_per_document: number;
  max_map_items: number;
  max_repeat_iterations: number;
  max_concurrent_nodes: number;
  max_subworkflow_depth: number;
  pdf_min_text_chars: number;
  ocr_language: string;
  ocr_llm_enhancement_enabled: boolean;
  ocr_llm_model: string | null;
  agent_provider: 'ollama' | 'openai';
  openai_api_key_set: boolean;
  openai_base_url: string | null;
  agent_max_turns: number;
  agent_tracing_enabled: boolean;
  python_node_enabled: boolean;
  python_node_timeout_seconds: number;
  python_node_memory_mb: number;
  python_node_allowed_imports: string[];
  ocr_llm_triage_model: string | null;
}

export interface SettingsUpdate {
  ollama_base_url?: string | null;
  default_generation_model?: string | null;
  default_embedding_model?: string | null;
  default_model_references?: Record<string, ModelReference> | null;
  request_timeout_seconds?: number | null;
  max_context_chars?: number | null;
  max_chunk_chars?: number | null;
  max_map_items?: number | null;
  max_repeat_iterations?: number | null;
  max_concurrent_nodes?: number | null;
  max_subworkflow_depth?: number | null;
  ocr_llm_enhancement_enabled?: boolean | null;
  ocr_llm_model?: string | null;
  ocr_llm_triage_model?: string | null;
  agent_provider?: 'ollama' | 'openai' | null;
  openai_api_key?: string | null;
  openai_base_url?: string | null;
  agent_max_turns?: number | null;
  agent_tracing_enabled?: boolean | null;
  python_node_enabled?: boolean | null;
  python_node_timeout_seconds?: number | null;
  python_node_memory_mb?: number | null;
  python_node_allowed_imports?: string[] | null;
}

export interface ProviderCheckResponse {
  provider: string;
  base_url: string;
  model: string | null;
  reachable: boolean;
  tool_calling: boolean;
  detail: string | null;
}

export type ProviderKind = 'ollama' | 'openai' | 'azure_openai' | 'azure_foundry' | 'openai_compatible';
export type ModelCapability = 'chat' | 'embedding' | 'vision' | 'tools';

/** A model name plus its provider profile. Neither value is a secret. */
export interface ModelReference {
  provider_profile_id?: string | null;
  model?: string | null;
}

export interface WorkflowModelDefaults {
  chat?: ModelReference | null;
  embedding?: ModelReference | null;
  vision?: ModelReference | null;
  tools?: ModelReference | null;
}

export interface ProviderModelEntry {
  name: string;
  capabilities: ModelCapability[];
}

export interface ProviderProfileCreate {
  name: string;
  kind: ProviderKind;
  base_url: string;
  api_version?: string | null;
  api_key?: string | null;
  models: ProviderModelEntry[];
}

export interface ProviderProfileUpdate {
  name?: string | null;
  kind?: ProviderKind | null;
  base_url?: string | null;
  api_version?: string | null;
  api_key?: string | null;
  models?: ProviderModelEntry[] | null;
}

export interface ProviderProfileResponse {
  id: string;
  name: string;
  kind: ProviderKind;
  base_url: string;
  api_version: string | null;
  api_key_set: boolean;
  state: 'active' | 'archived';
  models: ProviderModelEntry[];
  created_at: string;
  updated_at: string;
}

export interface ProviderModelsResponse {
  models: ProviderModelEntry[];
  discovery_error: string | null;
}

export interface ProviderVerifyRequest {
  model?: string | null;
}

export interface PythonExportResponse {
  filename: string;
  source: string;
}

export interface ModelInfo {
  name: string;
  digest: string | null;
  size: number | null;
  modified_at: string | null;
}

export interface WorkspaceNote {
  path: string;
  name: string;
  size_bytes: number;
  modified_at: string;
}

export interface WorkspaceNoteContent extends WorkspaceNote {
  content: string;
}

export interface ArtifactResponse {
  id: string;
  document_id: string | null;
  run_id: string | null;
  owner_type: string;
  kind: string;
  relative_path: string;
  media_type: string;
  size_bytes: number;
  sha256: string;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface DocumentChunkResponse {
  id: string;
  chunk_index: number;
  section_title: string | null;
  page_start: number;
  page_end: number;
  citation: string;
  text: string;
  metadata: Record<string, unknown>;
}

export interface DocumentResponse {
  id: string;
  title: string;
  source_filename: string;
  content_type: string;
  status: string;
  page_count: number | null;
  metadata: Record<string, unknown>;
  artifacts: ArtifactResponse[];
  chunks: DocumentChunkResponse[];
  created_at: string;
  updated_at: string;
}

export interface PortDefinitionResponse {
  name: string;
  kind: string;
  item_kind: string | null;
  description: string;
  required: boolean;
  fan_in?: boolean;
}

export interface NodeDefinitionResponse {
  type: string;
  label: string;
  description: string;
  category: string;
  tags: string[];
  inputs: PortDefinitionResponse[];
  outputs: PortDefinitionResponse[];
  config_schema: Record<string, unknown>;
  interface_role?: 'input' | 'output' | null;
}

export type CustomPortKind = 'any' | 'text' | 'number' | 'json' | 'list';
export type CustomConfigKind = 'text' | 'number' | 'integer' | 'boolean' | 'json' | 'select';

export interface CustomNodePort {
  name: string;
  kind: CustomPortKind;
  item_kind?: CustomPortKind | null;
  description: string;
  required: boolean;
}

export interface CustomNodeConfigField {
  name: string;
  label: string;
  description: string;
  kind: CustomConfigKind;
  required: boolean;
  default?: unknown;
  options: unknown[];
}

export interface CustomNodeSpec {
  label: string;
  description: string;
  category: string;
  tags: string[];
  inputs: CustomNodePort[];
  outputs: CustomNodePort[];
  config_fields: CustomNodeConfigField[];
  code: string;
}

export interface CustomNodeCreate extends CustomNodeSpec {
  name: string;
}

export interface CustomNodeUpdate extends CustomNodeSpec {
  name?: string | null;
}

export interface CustomNodeRevisionResponse extends CustomNodeSpec {
  id: string;
  definition_id: string;
  revision: number;
  node_type: string;
  config_schema: Record<string, unknown>;
  created_at: string;
}

export interface CustomNodeResponse {
  id: string;
  name: string;
  archived: boolean;
  archived_at: string | null;
  created_at: string;
  updated_at: string;
  latest_revision: CustomNodeRevisionResponse;
  revisions: CustomNodeRevisionResponse[];
}

export interface CustomNodeSampleRequest {
  spec: CustomNodeSpec;
  inputs: Record<string, unknown>;
  config: Record<string, unknown>;
  workflow_inputs: Record<string, unknown>;
}

export interface CustomNodeSampleResponse {
  output: Record<string, unknown>;
  stdout: string;
}

export interface WorkflowPort {
  key: string;
  kind: string;
  label: string;
  description: string;
  required: boolean;
  default: unknown;
  node_id: string;
  /** Every node declaring this key; more than one means the value feeds several places. */
  node_ids?: string[];
}

export interface WorkflowSignature {
  inputs: WorkflowPort[];
  outputs: WorkflowPort[];
}

export type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };
export type ConditionSource = 'workflow' | 'inputs';
export type ConditionOperator =
  | 'equals'
  | 'not_equals'
  | 'exists'
  | 'not_exists'
  | 'truthy'
  | 'falsy'
  | 'empty'
  | 'not_empty'
  | 'greater_than'
  | 'greater_than_or_equal'
  | 'less_than'
  | 'less_than_or_equal'
  | 'starts_with'
  | 'ends_with'
  | 'contains'
  | 'not_contains'
  | 'in'
  | 'not_in';

export interface ConditionPredicate {
  type: 'predicate';
  source: ConditionSource;
  path: string;
  operator: ConditionOperator;
  value?: JsonValue;
}

export interface ConditionGroup {
  type: 'group';
  operator: 'and' | 'or';
  conditions: ConditionRule[];
}

export type ConditionRule = ConditionGroup | ConditionPredicate;

export interface WorkflowNode {
  id: string;
  type: string;
  name?: string | null;
  config: Record<string, unknown>;
  static_inputs: Record<string, unknown>;
  run_when?: ConditionRule | null;
}

export interface WorkflowEdge {
  source_node_id: string;
  source_port: string;
  target_node_id: string;
  target_port: string;
}

export interface WorkflowDefinition {
  name: string;
  description?: string | null;
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  model_defaults?: WorkflowModelDefaults | null;
}

export interface WorkflowValidationResult {
  valid: boolean;
  errors: string[];
  order: string[];
  signature: WorkflowSignature;
}

export interface WorkflowCreateRequest {
  name: string;
  description?: string | null;
  definition: WorkflowDefinition;
  is_template?: boolean;
}

export interface WorkflowVersionResponse {
  id: string;
  workflow_id: string;
  version: number;
  definition: WorkflowDefinition;
  created_at: string;
}

export interface WorkflowResponse {
  id: string;
  name: string;
  description?: string | null;
  is_template: boolean;
  created_at: string;
  updated_at: string;
  latest_version: WorkflowVersionResponse | null;
}

export interface RunCreateRequest {
  workflow_version_id?: string | null;
  workflow?: WorkflowDefinition | null;
  inputs: Record<string, unknown>;
}

export type RunStatus = 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';
export type NodeRunStatus = RunStatus | 'skipped';

export interface NodeRunResponse {
  id: string;
  run_id: string;
  node_path: string;
  node_id: string;
  node_type: string;
  status: NodeRunStatus;
  input: Record<string, unknown>;
  output: Record<string, unknown> | null;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface RunResponse {
  id: string;
  workflow_version_id: string | null;
  workflow_name: string;
  status: RunStatus;
  input: Record<string, unknown>;
  output: unknown;
  error: string | null;
  cancel_requested: boolean;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  total_nodes: number;
  node_runs: NodeRunResponse[];
}

export interface RunEventResponse {
  id: number;
  run_id: string;
  event_type: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface ArtifactContentResponse {
  artifact: ArtifactResponse;
  content: unknown;
}

export interface WorkflowCanvasNodeData extends Record<string, unknown> {
  definition: NodeDefinitionResponse;
  nodeName: string;
  config: Record<string, unknown>;
  staticInputs: Record<string, unknown>;
  runWhen?: ConditionRule | null;
  effectiveModel?: {
    label: string;
    source: string;
    issue: 'missing-profile' | 'archived-profile' | 'missing-model' | 'incompatible' | null;
  };
}
