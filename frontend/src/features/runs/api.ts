import { json, request } from '../../api/client';
import type { components } from '../../api/schema.generated';

export type Run = components['schemas']['RunResponse'];
export type RunItem = Run['items'][number];

export const runsApi = {
  list: () => request<Run[]>('/runs'),
  get: (id: string) => request<Run>(`/runs/${encodeURIComponent(id)}`),
  cancel: (id: string) => request<Run>(`/runs/${encodeURIComponent(id)}/cancel`, { method: 'POST' }),
  resolve: (runId: string, interruptionId: string, approved: boolean, rejectionMessage?: string) =>
    request<Run>(
      `/runs/${encodeURIComponent(runId)}/interruptions/${encodeURIComponent(interruptionId)}`,
      json('POST', { approved, rejection_message: rejectionMessage ?? null }),
    ),
};
