import type {
  ArtifactContentResponse,
  CustomNodeCreate,
  CustomNodeResponse,
  CustomNodeSampleRequest,
  CustomNodeSampleResponse,
  CustomNodeUpdate,
  DocumentResponse,
  HealthResponse,
  ModelInfo,
  NodeDefinitionResponse,
  ProviderCheckResponse,
  ProviderModelsResponse,
  ProviderProfileCreate,
  ProviderProfileResponse,
  ProviderProfileUpdate,
  ProviderVerifyRequest,
  PythonExportResponse,
  RunCreateRequest,
  RunResponse,
  SettingsResponse,
  SettingsUpdate,
  WorkflowCreateRequest,
  WorkflowDefinition,
  WorkflowResponse,
  WorkflowValidationResult,
  WorkflowVersionResponse,
  WorkspaceNote,
  WorkspaceNoteContent,
} from '../types/api';

const API_BASE = import.meta.env.VITE_API_BASE || '/api';

function normalizeErrorDetail(detail: unknown): string {
  if (Array.isArray(detail)) {
    return detail.map((item) => normalizeErrorDetail(item)).join('\n');
  }
  if (detail && typeof detail === 'object') {
    return JSON.stringify(detail, null, 2);
  }
  if (typeof detail === 'string') {
    return detail;
  }
  return 'Request failed';
}

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      Accept: 'application/json',
      ...(init?.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
      ...init?.headers,
    },
  });

  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      message = normalizeErrorDetail(body.detail ?? body);
    } catch {
      const text = await response.text();
      if (text) {
        message = text;
      }
    }
    throw new ApiError(response.status, message);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

export const api = {
  getHealth: () => request<HealthResponse>('/health'),
  getSettings: () => request<SettingsResponse>('/settings'),
  updateSettings: (payload: SettingsUpdate) => request<SettingsResponse>('/settings', { method: 'PUT', body: JSON.stringify(payload) }),
  listNodes: () => request<NodeDefinitionResponse[]>('/nodes'),
  listModels: () => request<ModelInfo[]>('/models'),
  listProviderProfiles: (includeArchived = false) =>
    request<ProviderProfileResponse[]>(`/providers${includeArchived ? '?include_archived=true' : ''}`),
  getProviderProfile: (id: string) => request<ProviderProfileResponse>(`/providers/${encodeURIComponent(id)}`),
  createProviderProfile: (payload: ProviderProfileCreate) =>
    request<ProviderProfileResponse>('/providers', { method: 'POST', body: JSON.stringify(payload) }),
  updateProviderProfile: (id: string, payload: ProviderProfileUpdate) =>
    request<ProviderProfileResponse>(`/providers/${encodeURIComponent(id)}`, { method: 'PUT', body: JSON.stringify(payload) }),
  archiveProviderProfile: (id: string) =>
    request<ProviderProfileResponse>(`/providers/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  discoverProviderModels: (id: string) =>
    request<ProviderModelsResponse>(`/providers/${encodeURIComponent(id)}/models`),
  verifyProviderProfile: (id: string, payload: ProviderVerifyRequest = {}) =>
    request<ProviderCheckResponse>(`/providers/${encodeURIComponent(id)}/verify`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  listCustomNodes: (includeArchived = false) =>
    request<CustomNodeResponse[]>(`/custom-nodes${includeArchived ? '?include_archived=true' : ''}`),
  getCustomNode: (id: string) => request<CustomNodeResponse>(`/custom-nodes/${encodeURIComponent(id)}`),
  createCustomNode: (payload: CustomNodeCreate) =>
    request<CustomNodeResponse>('/custom-nodes', { method: 'POST', body: JSON.stringify(payload) }),
  updateCustomNode: (id: string, payload: CustomNodeUpdate) =>
    request<CustomNodeResponse>(`/custom-nodes/${encodeURIComponent(id)}`, { method: 'PUT', body: JSON.stringify(payload) }),
  archiveCustomNode: (id: string) =>
    request<CustomNodeResponse>(`/custom-nodes/${encodeURIComponent(id)}/archive`, { method: 'POST' }),
  sampleCustomNode: (payload: CustomNodeSampleRequest) =>
    request<CustomNodeSampleResponse>('/custom-nodes/sample', { method: 'POST', body: JSON.stringify(payload) }),
  testCustomNode: (payload: CustomNodeSampleRequest) =>
    request<CustomNodeSampleResponse>('/custom-nodes/test', { method: 'POST', body: JSON.stringify(payload) }),
  listNotes: () => request<WorkspaceNote[]>('/notes'),
  getNote: (path: string) => request<WorkspaceNoteContent>(`/notes/content?${new URLSearchParams({ path })}`),
  deleteNote: (path: string) => request<void>(`/notes?${new URLSearchParams({ path })}`, { method: 'DELETE' }),
  listDocuments: () => request<DocumentResponse[]>('/documents'),
  getDocument: (id: string) => request<DocumentResponse>(`/documents/${id}`),
  uploadDocument: async (file: File, title?: string) => {
    const formData = new FormData();
    formData.append('file', file);
    if (title) {
      formData.append('title', title);
    }
    return request<DocumentResponse>('/documents', { method: 'POST', body: formData, headers: {} });
  },
  ingestDocument: (id: string) => request<DocumentResponse>(`/documents/${id}/ingest`, { method: 'POST' }),
  deleteDocument: (id: string) => request<void>(`/documents/${id}`, { method: 'DELETE' }),
  getArtifactContent: (artifactId: string) => request<ArtifactContentResponse>(`/artifacts/${artifactId}/content`),
  listWorkflows: () => request<WorkflowResponse[]>('/workflows'),
  listWorkflowTemplates: () => request<WorkflowDefinition[]>('/workflows/templates'),
  getWorkflow: (id: string) => request<WorkflowResponse>(`/workflows/${id}`),
  listWorkflowVersions: (id: string) => request<WorkflowVersionResponse[]>(`/workflows/${id}/versions`),
  validateWorkflow: (definition: WorkflowDefinition) => request<WorkflowValidationResult>('/workflows/validate', { method: 'POST', body: JSON.stringify(definition) }),
  exportWorkflowPython: (definition: WorkflowDefinition) =>
    request<PythonExportResponse>('/workflows/export/python', { method: 'POST', body: JSON.stringify(definition) }),
  verifyAgentProvider: () => request<ProviderCheckResponse>('/agents/verify', { method: 'POST' }),
  createWorkflow: (payload: WorkflowCreateRequest) => request<WorkflowResponse>('/workflows', { method: 'POST', body: JSON.stringify(payload) }),
  deleteWorkflow: (id: string) => request<void>(`/workflows/${id}`, { method: 'DELETE' }),
  listRuns: () => request<RunResponse[]>('/runs'),
  getRun: (id: string) => request<RunResponse>(`/runs/${id}`),
  createRun: (payload: RunCreateRequest) => request<RunResponse>('/runs', { method: 'POST', body: JSON.stringify(payload) }),
  cancelRun: (id: string) => request<RunResponse>(`/runs/${id}/cancel`, { method: 'POST' }),
  deleteRun: (id: string) => request<void>(`/runs/${id}`, { method: 'DELETE' }),
};

export function runEventsUrl(runId: string, after?: number): string {
  const search = typeof after === 'number' ? `?after=${after}` : '';
  return `${API_BASE}/runs/${runId}/events${search}`;
}
