import { json, request } from '../../api/client';
import type { components } from '../../api/schema.generated';

export type Conversation = components['schemas']['ConversationResponse'];
export type ConversationDetail = components['schemas']['ConversationDetailResponse'];
export type SessionItem = components['schemas']['SessionItemResponse'];
export type Run = components['schemas']['RunResponse'];
export type StopAndAnswerResponse = components['schemas']['StopAndAnswerResponse'];
export type ModelReference = components['schemas']['ModelReferenceSpec'];

export const chatApi = {
  list: () => request<Conversation[]>('/agent/conversations'),
  create: (title: string, modelReference: ModelReference) =>
    request<Conversation>(
      '/agent/conversations',
      json('POST', { title, model_reference: modelReference }),
    ),
  get: (id: string) =>
    request<ConversationDetail>(`/agent/conversations/${encodeURIComponent(id)}`),
  send: (id: string, content: string) =>
    request<components['schemas']['ConversationMessageResponse']>(
      `/agent/conversations/${encodeURIComponent(id)}/messages`,
      json('POST', { content }),
    ),
  remove: (id: string) =>
    request<void>(`/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  runs: (conversationId: string) =>
    request<Run[]>(`/runs?conversation_id=${encodeURIComponent(conversationId)}`),
  run: (id: string) => request<Run>(`/runs/${encodeURIComponent(id)}`),
  cancelRun: (id: string) =>
    request<Run>(`/runs/${encodeURIComponent(id)}/cancel`, json('POST', {})),
  stopAndAnswer: (id: string) =>
    request<StopAndAnswerResponse>(
      `/runs/${encodeURIComponent(id)}/stop-and-answer`,
      json('POST', {}),
    ),
};
