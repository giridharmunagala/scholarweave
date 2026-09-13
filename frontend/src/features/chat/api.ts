import { json, request } from '../../api/client';
import type { components } from '../../api/schema.generated';

export type Conversation = components['schemas']['ConversationResponse'];
export type ConversationDetail = components['schemas']['ConversationDetailResponse'];
export type ConversationAttachment = components['schemas']['ConversationAttachmentResponse'];
export type SessionItem = components['schemas']['SessionItemResponse'];
export type Run = components['schemas']['RunResponse'];
export type PromptSnapshot = components['schemas']['PromptSnapshotResponse'];
export type StopAndAnswerResponse = components['schemas']['StopAndAnswerResponse'];
export type SteeringMessage = components['schemas']['SteeringMessageResponse'];
export type ModelReference = components['schemas']['ModelReferenceSpec'];
type ConversationMessageRequest = components['schemas']['ConversationMessageRequest'];
export type ResponseEffort = NonNullable<ConversationMessageRequest['response_effort']>;
type SendMessageOptions = Partial<Pick<ConversationMessageRequest,
  'reasoning_effort' | 'web_enabled' | 'response_effort' | 'context_window_tokens' | 'attachment_paths'
>>;

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
    options: SendMessageOptions = {},
  ) =>
    request<components['schemas']['ConversationMessageResponse']>(
      `${basePath}/${encodeURIComponent(id)}/messages`,
      json('POST', {
        content,
        ...options,
        web_enabled: options.web_enabled ?? true,
        response_effort: options.response_effort ?? 'auto',
        attachment_paths: options.attachment_paths?.length ? options.attachment_paths : undefined,
      } satisfies Pick<ConversationMessageRequest, 'content' | keyof SendMessageOptions>),
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
export const chatApi = {
  ...conversationApi('/agent/conversations'),
  uploadAttachment: (file: File, signal?: AbortSignal) => {
    const body = new FormData();
    body.append('file', file);
    return request<ConversationAttachment>('/agent/attachments', {
      method: 'POST',
      body,
      signal,
    });
  },
};
