import { json, request } from '../../api/client';
import type { components } from '../../api/schema.generated';
import type { ReasoningEffort } from './ReasoningEffortSelect';

export type Conversation = components['schemas']['ConversationResponse'];
export type ConversationDetail = components['schemas']['ConversationDetailResponse'];
export type SessionItem = components['schemas']['SessionItemResponse'];
export type Run = components['schemas']['RunResponse'];
export type PromptSnapshot = components['schemas']['PromptSnapshotResponse'];
export type StopAndAnswerResponse = components['schemas']['StopAndAnswerResponse'];
export type SteeringMessage = components['schemas']['SteeringMessageResponse'];
export type ModelReference = components['schemas']['ModelReferenceSpec'];
type ConversationMessageRequest = components['schemas']['ConversationMessageRequest'];

function conversationApi(basePath: string) {
  return {
  list: () => request<Conversation[]>(basePath),
  create: (modelReference: ModelReference) =>
    request<Conversation>(
      basePath,
      json('POST', { model_reference: modelReference }),
    ),
  get: (id: string) =>
    request<ConversationDetail>(`${basePath}/${encodeURIComponent(id)}`),
  send: (
    id: string,
    content: string,
    reasoningEffort: ReasoningEffort | null = null,
    webEnabled = true,
    deepWork = false,
    fastAnswer = false,
    webSearchLimit = 1,
    contextWindowTokens?: number,
  ) =>
    request<components['schemas']['ConversationMessageResponse']>(
      `${basePath}/${encodeURIComponent(id)}/messages`,
      json('POST', {
        content,
        reasoning_effort: reasoningEffort ?? undefined,
        web_enabled: webEnabled,
        deep_work: deepWork,
        fast_answer: fastAnswer,
        web_search_limit: webSearchLimit,
        context_window_tokens: contextWindowTokens,
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
}
export const chatApi = conversationApi('/agent/conversations');
