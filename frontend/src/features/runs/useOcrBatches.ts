import { useSyncExternalStore } from 'react';
import { isActiveOcrBatch, ocrBatchesApi, type OcrBatch } from './api';

export const OCR_POLL_INTERVAL = 1500;
type State = {
  batches: OcrBatch[];
  loading: boolean;
  refreshing: boolean;
  starting: boolean;
  stopping: string[];
  error: unknown;
};
const empty = (): State => ({ batches: [], loading: true, refreshing: false, starting: false, stopping: [], error: null });
let state = empty();
const listeners = new Set<() => void>();
let timer: ReturnType<typeof setTimeout> | undefined;
let controller: AbortController | null = null;
let refreshPromise: Promise<void> | null = null;
let revision = 0;
const snapshot = () => state;

function update(changes: Partial<State>) {
  state = { ...state, ...changes };
  listeners.forEach((listener) => listener());
}

function clearPoll() {
  clearTimeout(timer);
  timer = undefined;
}

function schedule() {
  clearPoll();
  if (listeners.size && state.batches.some(isActiveOcrBatch)) {
    timer = setTimeout(() => { void refreshOcrBatches(); }, OCR_POLL_INTERVAL);
  }
}

export function refreshOcrBatches(): Promise<void> {
  if (refreshPromise) return refreshPromise;
  clearPoll();
  const requestController = new AbortController();
  controller = requestController;
  const requestRevision = revision;
  update({ refreshing: true });
  const pending = ocrBatchesApi.list(requestController.signal)
    .then((batches) => {
      if (requestController.signal.aborted || requestRevision !== revision) return;
      update({ batches, error: null, loading: false });
    })
    .catch((error: unknown) => {
      if (!requestController.signal.aborted && requestRevision === revision) update({ error, loading: false });
    })
    .finally(() => {
      if (controller !== requestController) return;
      controller = null;
      refreshPromise = null;
      update({ refreshing: false });
      schedule();
    });
  refreshPromise = pending;
  return pending;
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  if (listeners.size === 1) void refreshOcrBatches();
  return () => {
    listeners.delete(listener);
    if (listeners.size) return;
    clearPoll();
    controller?.abort();
    controller = null;
    refreshPromise = null;
    revision++;
    state = { ...empty(), starting: state.starting, stopping: state.stopping };
  };
}

function beginMutation() {
  revision++;
  controller?.abort();
  controller = null;
  refreshPromise = null;
  clearPoll();
  update({ refreshing: false, error: null });
}

function accept(batch: OcrBatch) {
  revision++;
  controller?.abort();
  controller = null;
  refreshPromise = null;
  update({ loading: false, refreshing: false, batches: [batch, ...state.batches.filter((item) => item.id !== batch.id)]
    .sort((a, b) => b.created_at.localeCompare(a.created_at)) });
}

async function start(documentIds: string[] | null) {
  if (state.loading || state.starting || state.stopping.length || state.batches.some(isActiveOcrBatch)) return;
  beginMutation();
  update({ starting: true });
  try {
    const batch = await ocrBatchesApi.start(documentIds);
    accept(batch);
    return batch;
  } catch (error) {
    update({ error });
    throw error;
  } finally {
    update({ starting: false });
    schedule();
  }
}

async function stop(id: string) {
  if (state.stopping.includes(id)) return;
  beginMutation();
  update({ stopping: [...state.stopping, id] });
  try {
    const batch = await ocrBatchesApi.stop(id);
    accept(batch);
    return batch;
  } catch (error) {
    update({ error });
    throw error;
  } finally {
    update({ stopping: state.stopping.filter((item) => item !== id) });
    schedule();
  }
}

export function useOcrBatches() {
  return { ...useSyncExternalStore(subscribe, snapshot), refresh: refreshOcrBatches, start, stop };
}
