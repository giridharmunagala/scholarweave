import { json, request } from '../../api/client';
import type { components } from '../../api/schema.generated';

export type SummaryRun = components['schemas']['PaperSummaryRunResponse'];
export type SummaryVersion = components['schemas']['PaperSummaryVersionResponse'];
export type SummaryContent = components['schemas']['PaperSummaryContentResponse'];
export type SummaryPromotion = components['schemas']['PaperSummaryPromotionResponse'];
export type SummaryRequest = components['schemas']['PaperSummaryRunRequest'];
export type SummaryBatch = components['schemas']['PaperSummaryBatchResponse'];

const base = (documentId: string) =>
  `/documents/${encodeURIComponent(documentId)}/summaries`;

export const summaryApi = {
  start: (documentId: string, options: SummaryRequest = { mode: 'reviewed' }) =>
    request<SummaryRun>(base(documentId), json('POST', { model_reference: {}, ...options })),
  startBatch: (documentIds: string[], options: SummaryRequest) =>
    request<SummaryBatch>('/documents/summary-batches',
      json('POST', { ...options, document_ids: documentIds })),
  versions: (documentId: string) =>
    request<SummaryVersion[]>(base(documentId)),
  activeRuns: (documentId?: string) =>
    request<SummaryRun[]>(`/documents/summary-runs${documentId ? `?${new URLSearchParams({ document_id: documentId })}` : ''}`),
  get: (documentId: string, versionId: string) =>
    request<SummaryContent>(`${base(documentId)}/${encodeURIComponent(versionId)}`),
  canonical: (path: string) =>
    request<components['schemas']['WorkspaceFileContentResponse']>(`/workspace/files/content?${new URLSearchParams({ path })}`),
  cancel: (runId: string) =>
    request<components['schemas']['RunResponse']>(`/runs/${encodeURIComponent(runId)}/cancel`, { method: 'POST' }),
  promote: (documentId: string, versionId: string, expectedSha256: string) =>
    request<SummaryPromotion>(
      `${base(documentId)}/${encodeURIComponent(versionId)}/promote`,
      json('POST', { expected_sha256: expectedSha256, replace_existing: true }),
    ),
};
