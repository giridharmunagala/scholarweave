import { json, request } from '../../api/client';
import type { components } from '../../api/schema.generated';

export type OcrBatchItem = components['schemas']['OcrBatchItemResponse'];
export type OcrBatch = components['schemas']['OcrBatchResponse'];

export function isActiveOcrBatch(batch: OcrBatch) {
  return batch.status === 'pending' || batch.status === 'running' || batch.status === 'stopping';
}

export const ocrBatchesApi = {
  list: async (signal?: AbortSignal) => {
    const batches = await request<OcrBatch[]>('/documents/ocr-batches', { signal });
    if (!Array.isArray(batches)) throw new Error('The server returned an invalid OCR batch list. Refresh runs to retry.');
    return batches;
  },
  get: (id: string, signal?: AbortSignal) => request<OcrBatch>(`/documents/ocr-batches/${encodeURIComponent(id)}`, { signal }),
  start: (documentIds: string[] | null) => request<OcrBatch>('/documents/ocr-batches', json('POST', { document_ids: documentIds })),
  stop: (id: string) => request<OcrBatch>(`/documents/ocr-batches/${encodeURIComponent(id)}/stop`, json('POST')),
};
