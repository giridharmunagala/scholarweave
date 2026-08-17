import { json, request } from '../../api/client';
import type { components } from '../../api/schema.generated';
import type { ReasoningEffort } from '../chat/ReasoningEffortSelect';

export type DirectAgent = components['schemas']['DirectAgentResponse'];
export type DirectConversation = components['schemas']['DirectConversationResponse'];
export type DirectConversationDetail = components['schemas']['DirectConversationDetailResponse'];
export type Document = components['schemas']['DocumentResponse'];
export type ModelReference = components['schemas']['ModelReferenceSpec'];
export type Run = components['schemas']['RunResponse'];

export const directAgentsApi = {
  agents: () => request<DirectAgent[]>('/research-agents'),
  conversations: () =>
    request<DirectConversation[]>('/research-agent-conversations'),
  documents: () => request<Document[]>('/documents'),
  create: (
    agentKey: DirectAgent['key'],
    documentIds: string[],
    title: string,
    modelReference: ModelReference,
  ) =>
    request<DirectConversation>(
      '/research-agent-conversations',
      json('POST', {
        agent_key: agentKey,
        document_ids: documentIds,
        title,
        model_reference: modelReference,
      }),
    ),
  get: (id: string) =>
    request<DirectConversationDetail>(
      `/research-agent-conversations/${encodeURIComponent(id)}`,
    ),
  send: (id: string, content: string, reasoningEffort: ReasoningEffort | null) =>
    request<components['schemas']['DirectConversationMessageResponse']>(
      `/research-agent-conversations/${encodeURIComponent(id)}/messages`,
      json('POST', {
        content,
        reasoning_effort: reasoningEffort ?? undefined,
      }),
    ),
  run: (id: string) => request<Run>(`/runs/${encodeURIComponent(id)}`),
};
