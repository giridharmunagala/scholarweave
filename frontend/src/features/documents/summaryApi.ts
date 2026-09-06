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
  get: (documentId: string, versionId: string) =>
    request<SummaryContent>(`${base(documentId)}/${encodeURIComponent(versionId)}`),
  promote: (documentId: string, versionId: string) =>
    request<SummaryPromotion>(
      `${base(documentId)}/${encodeURIComponent(versionId)}/promote`,
      { method: 'POST' },
    ),
};
