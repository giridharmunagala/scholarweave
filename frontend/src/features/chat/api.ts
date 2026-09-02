import { json, request } from '../../api/client';
import type { components } from '../../api/schema.generated';
import type { ReasoningEffort } from './ReasoningEffortSelect';

export type Conversation = components['schemas']['ConversationResponse'];
export type ConversationDetail = components['schemas']['ConversationDetailResponse'];
export type SessionItem = components['schemas']['SessionItemResponse'];
export type Run = components['schemas']['RunResponse'];
export type StopAndAnswerResponse = components['schemas']['StopAndAnswerResponse'];
export type SteeringMessage = components['schemas']['SteeringMessageResponse'];
export type ModelReference = components['schemas']['ModelReferenceSpec'];
type ConversationMessageRequest = components['schemas']['ConversationMessageRequest'];
export type WorkMode = ConversationMessageRequest['work_mode'];
export type WorkBudget = ConversationMessageRequest['work_budget'];

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
    reasoningEffort: ReasoningEffort | null = null,
    workMode: WorkMode = 'direct',
    workBudget: WorkBudget = 'medium',
  ) =>
    request<components['schemas']['ConversationMessageResponse']>(
      `/agent/conversations/${encodeURIComponent(id)}/messages`,
      json('POST', {
        content,
        reasoning_effort: reasoningEffort ?? undefined,
        work_mode: workMode,
        work_budget: workBudget,
      }),
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
  steer: (id: string, content: string) =>
    request<SteeringMessage>(
      `/runs/${encodeURIComponent(id)}/steering`,
      json('POST', { content }),
    ),
};
