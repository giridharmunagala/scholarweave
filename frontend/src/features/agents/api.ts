import { json, request } from '../../api/client';
import type {
  AgentBlueprint,
  AgentResponse,
  AgentValidation,
  SdkCatalog,
} from './types';

export const agentsApi = {
  list: () => request<AgentResponse[]>('/agents'),
  templates: () => request<AgentBlueprint[]>('/agents/templates'),
  catalog: () => request<SdkCatalog>('/sdk/catalog'),
  get: (id: string) => request<AgentResponse>(`/agents/${encodeURIComponent(id)}`),
  validate: (blueprint: AgentBlueprint) =>
    request<AgentValidation>('/agents/validate', json('POST', blueprint)),
  create: (blueprint: AgentBlueprint, presentation: Record<string, unknown>) =>
    request<AgentResponse>(
      '/agents',
      json('POST', { blueprint, presentation }),
    ),
  update: (
    id: string,
    blueprint: AgentBlueprint,
    presentation: Record<string, unknown>,
  ) =>
    request<AgentResponse>(
      `/agents/${encodeURIComponent(id)}`,
      json('PUT', { blueprint, presentation }),
    ),
  remove: (id: string) =>
    request<void>(`/agents/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  run: (agentRevisionId: string, input: string) =>
    request<{ id: string }>(
      '/runs',
      json('POST', { agent_revision_id: agentRevisionId, input }),
    ),
  runEphemeral: (blueprint: AgentBlueprint, input: string) =>
    request<{ id: string }>(
      '/runs',
      json('POST', { blueprint, input }),
    ),
};
