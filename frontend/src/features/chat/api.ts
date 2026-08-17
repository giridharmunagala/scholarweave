import { json, request } from '../../api/client';
import type { components } from '../../api/schema.generated';
import type { ReasoningEffort } from './ReasoningEffortSelect';

export type Conversation = components['schemas']['ConversationResponse'];
export type ConversationDetail = components['schemas']['ConversationDetailResponse'];
export type SessionItem = components['schemas']['SessionItemResponse'];
export type Run = components['schemas']['RunResponse'];
export type ModelReference = components['schemas']['ModelReferenceSpec'];
export type WorkMode = 'direct' | 'extended';

export const CHAT_CONVERSATIONS_CHANGED = 'scholarweave:chat-conversations-changed';

export function notifyChatConversationsChanged(): void {
  window.dispatchEvent(new Event(CHAT_CONVERSATIONS_CHANGED));
}

export const chatApi = {
  list: () => request<Conversation[]>('/agent/conversations'),
  create: (title: string, modelReference: ModelReference) =>
    request<Conversation>(
      '/agent/conversations',
      json('POST', { title, model_reference: modelReference }),
    ),
  get: (id: string) =>
    request<ConversationDetail>(`/agent/conversations/${encodeURIComponent(id)}`),
  send: (
    id: string,
    content: string,
    reasoningEffort: ReasoningEffort | null,
    workMode: WorkMode,
  ) =>
    request<components['schemas']['ConversationMessageResponse']>(
      `/agent/conversations/${encodeURIComponent(id)}/messages`,
      json('POST', {
        content,
        reasoning_effort: reasoningEffort ?? undefined,
        work_mode: workMode,
      }),
    ),
  remove: (id: string) =>
    request<void>(`/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  runs: (conversationId: string) =>
    request<Run[]>(`/runs?conversation_id=${encodeURIComponent(conversationId)}`),
  run: (id: string) => request<Run>(`/runs/${encodeURIComponent(id)}`),
};
