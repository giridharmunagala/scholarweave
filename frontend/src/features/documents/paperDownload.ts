import { useSyncExternalStore } from 'react';
import { json, request } from '../../api/client';
import type { components } from '../../api/schema.generated';

type Document = components['schemas']['DocumentResponse'];
type PaperDownload =
  | { status: 'idle' }
  | { status: 'running'; url: string; title: string; startedAt: number }
  | { status: 'completed'; paper: Document }
  | { status: 'failed'; url: string; title: string; error: unknown };

let download: PaperDownload = { status: 'idle' };
const listeners = new Set<() => void>();
const snapshot = () => download;
const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
};

function update(next: PaperDownload) {
  download = next;
  listeners.forEach((listener) => listener());
}

export function usePaperDownload() {
  return useSyncExternalStore(subscribe, snapshot);
}

export function clearPaperDownload(result: PaperDownload): boolean {
  if (download !== result || (download.status !== 'completed' && download.status !== 'failed')) return false;
  update({ status: 'idle' });
  return true;
}

export async function downloadPaper(url: string, title: string) {
  if (download.status === 'running') return;
  update({ status: 'running', url, title, startedAt: Date.now() });
  try {
    const paper = await request<Document>(
      '/documents/download',
      json('POST', { url, title: title || null }),
    );
    update({ status: 'completed', paper });
  } catch (error) {
    update({ status: 'failed', url, title, error });
  }
}
