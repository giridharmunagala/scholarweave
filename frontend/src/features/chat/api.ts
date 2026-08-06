import { json, request } from '../../api/client';
import type { components } from '../../api/schema.generated';

export type Conversation = components['schemas']['ConversationResponse'];
export type ConversationDetail = components['schemas']['ConversationDetailResponse'];
export type SessionItem = components['schemas']['SessionItemResponse'];
export type Run = components['schemas']['RunResponse'];
export type ModelReference = components['schemas']['ModelReferenceSpec'];

export const chatApi = {
  list: () => request<Conversation[]>('/builder/conversations'),
  create: (title: string, modelReference: ModelReference) =>
    request<Conversation>(
      '/builder/conversations',
      json('POST', { title, model_reference: modelReference }),
    ),
  get: (id: string) =>
    request<ConversationDetail>(`/builder/conversations/${encodeURIComponent(id)}`),
  send: (id: string, content: string) =>
    request<components['schemas']['ConversationMessageResponse']>(
      `/builder/conversations/${encodeURIComponent(id)}/messages`,
      json('POST', { content }),
    ),
  remove: (id: string) =>
    request<void>(`/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  run: (id: string) => request<Run>(`/runs/${encodeURIComponent(id)}`),
};
