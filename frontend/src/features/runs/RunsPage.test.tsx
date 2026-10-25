// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { RouterProvider } from '../../app/router';
import { RunsPage } from './RunsPage';
import type { OcrBatch, OcrBatchItem } from './api';
import { OCR_POLL_INTERVAL } from './useOcrBatches';

const respond = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'content-type': 'application/json' },
});
const settings = { ocr_engine: 'surya', ocr_surya_model: 'local-surya', ocr_surya_parallel: 6 };
const item = (document_id: string, overrides: Partial<OcrBatchItem> = {}): OcrBatchItem => ({
  document_id, title: `Paper ${document_id}`, status: 'pending', completed_pages: 0,
  total_pages: 10, phase_label: null, error: null, ...overrides,
});
const batch = (overrides: Partial<OcrBatch> = {}): OcrBatch => ({
  id: 'batch-one', status: 'running', created_at: '2026-10-22T10:00:00Z', updated_at: '2026-10-22T10:01:00Z',
  finished_at: null, parallel_requests: 4, items: [item('one')], ...overrides,
});
function heldResponse() {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>((finish) => { resolve = finish; });
  return { promise, resolve };
}
let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  container = document.createElement('div');
  root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount());
  vi.restoreAllMocks();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});
const render = async () => { await act(async () => root.render(<RouterProvider><RunsPage /></RouterProvider>)); };
const button = (text: string) => [...container.querySelectorAll('button')].find((entry) => entry.textContent === text)!;
const progress = (label: string) => container.querySelector<HTMLProgressElement>(`progress[aria-label="${label}"]`)!;
const pageCount = (label: string) => container.querySelector<HTMLParagraphElement>(`p[aria-label="${label}"]`)!;
const click = async (text: string) => { await act(async () => button(text).click()); };
const expandHistory = async () => {
  await act(async () => {
    for (const details of container.querySelectorAll('details')) {
      details.open = true;
      details.dispatchEvent(new Event('toggle'));
    }
  });
};

it('opens idle without starting anything and starts remaining eligible papers only on request', async () => {
  const posts: unknown[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (path.endsWith('/settings')) return respond(settings);
    if (init?.method === 'POST') {
      posts.push(JSON.parse(String(init.body)));
      return respond(batch({ status: 'pending', items: [item('one'), item('two', { total_pages: null })] }));
    }
    return respond([]);
  }));
  await render();
  expect(container.textContent).toContain('No OCR batches yet');
  expect(container.textContent).toContain('Nothing starts automatically');
  expect(container.textContent).toContain('Configured shared page-request cap: 6');
  expect(posts).toEqual([]);
  await click('Start OCR for remaining papers');
  expect(posts).toEqual([{ document_ids: null }]);
  expect(button('Start OCR for remaining papers').disabled).toBe(true);
  expect(container.textContent).toContain('2 queued');
  expect(container.textContent).toContain('Shared page-request limit: 4');
  expect(progress('Pages completed').hasAttribute('value')).toBe(false);
  expect(container.querySelector('a[href="/library?paper=one"]')).toBeNull();
  expect(container.textContent).toContain('No papers are currently running');
});

it('restores active progress and history on reload, using actual paper and page counts', async () => {
  const active = batch({ items: [
    item('done', { status: 'completed', completed_pages: 10 }),
    item('live', { status: 'running', completed_pages: 3, phase_label: 'Reading page 4' }),
    item('queued'),
  ] });
  const history = batch({ id: 'older', status: 'completed_with_errors', created_at: '2026-10-21T10:00:00Z', items: [
    item('error', { status: 'failed', completed_pages: 2, error: 'OCR model disconnected' }),
    item('fallback', {
      status: 'completed_with_errors', completed_pages: 2, total_pages: 2,
      phase_label: 'Saved with embedded-text fallback',
      error: 'OCR failed on pages 2. Embedded PDF text was saved instead; this paper remains retryable.',
    }),
  ] });
  const fetch = vi.fn(async (input: RequestInfo | URL) => respond(String(input).endsWith('/settings') ? settings : [active, history]));
  vi.stubGlobal('fetch', fetch);
  await render();
  const live = container.querySelector('[aria-label="Active OCR batches"]')!;
  expect(live.textContent).toContain('1 running1 queued1 completed0 failed');
  expect(progress('Papers completed').value).toBe(1);
  expect(progress('Papers completed').max).toBe(3);
  expect(progress('Pages completed').value).toBe(13);
  expect(progress('Pages completed').max).toBe(30);
  expect(pageCount('Pages for Paper live').textContent).toContain('3 / 10');
  expect(container.querySelectorAll('li progress')).toHaveLength(0);
  expect(container.textContent).toContain('Reading page 4');
  expect(container.querySelector('[aria-label="Recent OCR batches"]')?.textContent).not.toContain('OCR model disconnected');
  await expandHistory();
  expect(container.querySelector('[aria-label="Recent OCR batches"]')?.textContent).toContain('OCR model disconnected');
  expect(container.querySelector('[aria-label="Recent OCR batches"]')?.textContent).toContain('1 with page errors');
  expect(container.querySelector('[aria-label="Recent OCR batches"]')?.textContent).toContain('OCR failed on pages 2');
  expect(container.querySelector('[aria-label="Recent OCR batches"]')?.textContent).toContain('Embedded PDF text was saved instead');
  expect(pageCount('Pages for Paper fallback').textContent).toContain('2 / 2');
  await act(async () => root.unmount());
  root = createRoot(container);
  await render();
  expect(pageCount('Pages for Paper live').textContent).toContain('3 / 10');
  expect(fetch.mock.calls.filter(([input]) => String(input).endsWith('/ocr-batches'))).toHaveLength(2);
});

it('estimates only overall time from the last two minutes of page completions', async () => {
  vi.useFakeTimers();
  let time = 0;
  vi.spyOn(performance, 'now').mockImplementation(() => time);
  let current = batch({ items: [
    item('done', { status: 'completed', completed_pages: 10 }),
    item('live', { status: 'running', completed_pages: 2 }),
    item('queued', { total_pages: 5 }),
    item('failed', { status: 'failed', completed_pages: 2 }),
    item('skipped', { status: 'skipped', total_pages: null }),
  ] });
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) =>
    respond(String(input).endsWith('/settings') ? settings : [current])));
  await render();
  const overall = () => progress('Pages completed').closest('.ocr-run-overall')!;
  const paper = () => pageCount('Pages for Paper live').closest('li')!;
  expect(overall().textContent).toContain('Measuring speed');
  expect(overall().textContent).not.toMatch(/~\d/);
  expect(paper().textContent).not.toMatch(/~\d/);

  time = 60_000;
  current = { ...current, items: current.items.map((entry) => entry.document_id === 'live'
    ? { ...entry, completed_pages: 4 } : entry) };
  await act(async () => vi.advanceTimersByTimeAsync(OCR_POLL_INTERVAL));
  expect(overall().textContent).toContain('2.0 pages/min');
  expect(overall().textContent).toContain('30.0 s/page');
  expect(overall().textContent).toContain('~6 min remaining');
  expect(paper().textContent).not.toMatch(/~\d/);
  expect(pageCount('Pages for Paper queued')).toBeNull();
  expect(pageCount('Pages for Paper failed')).toBeNull();

  time = 120_000;
  current = { ...current, items: current.items.map((entry) => entry.document_id === 'live'
    ? { ...entry, completed_pages: 5 } : entry) };
  await act(async () => vi.advanceTimersByTimeAsync(OCR_POLL_INTERVAL));
  time = 180_000;
  current = { ...current, items: current.items.map((entry) => entry.document_id === 'live'
    ? { ...entry, completed_pages: 9 } : entry) };
  await act(async () => vi.advanceTimersByTimeAsync(OCR_POLL_INTERVAL));
  expect(overall().textContent).toContain('2.5 pages/min');
  expect(overall().textContent).toContain('24.0 s/page');
  expect(overall().textContent).toContain('~3 min remaining');

  current = { ...current, status: 'completed_with_errors', items: current.items.map((entry) =>
    entry.status === 'running' || entry.status === 'pending'
      ? { ...entry, status: 'completed', completed_pages: entry.total_pages! } : entry) };
  await act(async () => vi.advanceTimersByTimeAsync(OCR_POLL_INTERVAL));
  expect(container.querySelector('[aria-label="Active OCR batches"]')).toBeNull();
  expect(container.textContent).not.toMatch(/~\d/);
  expect(vi.getTimerCount()).toBe(0);
});

it('waits for unknown page totals before forecasting and removes estimates when stopping', async () => {
  vi.useFakeTimers();
  let time = 0;
  vi.spyOn(performance, 'now').mockImplementation(() => time);
  let current = batch({ items: [
    item('live', { status: 'running', completed_pages: 2 }),
    item('unknown', { total_pages: null }),
  ] });
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input).endsWith('/settings')) return respond(settings);
    if (init?.method === 'POST') {
      current = { ...current, status: 'stopping' };
      return respond(current);
    }
    return respond([current]);
  }));
  await render();
  time = 60_000;
  current = { ...current, items: [{ ...current.items[0], completed_pages: 4 }, current.items[1]] };
  await act(async () => vi.advanceTimersByTimeAsync(OCR_POLL_INTERVAL));
  const overall = progress('Pages completed').closest('.ocr-run-overall')!;
  expect(overall.textContent).toContain('2.0 pages/min');
  expect(overall.textContent).toContain('time remaining unavailable until the page total is known');
  expect(overall.textContent).not.toMatch(/~\d/);
  expect(pageCount('Pages for Paper live').closest('li')!.textContent).not.toMatch(/~\d/);

  current = { ...current, items: [current.items[0], { ...current.items[1], total_pages: 5 }] };
  await act(async () => vi.advanceTimersByTimeAsync(OCR_POLL_INTERVAL));
  expect(overall.textContent).toContain('~6 min remaining');
  await click('Stop batch');
  expect(container.textContent).toContain('Cancellation requested');
  expect(container.textContent).not.toMatch(/~\d/);
});

it('retains an in-flight start across navigation and ignores an older empty restoration read', async () => {
  const starting = heldResponse();
  const restoration = heldResponse();
  let lists = 0;
  let posts = 0;
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input).endsWith('/settings')) return respond(settings);
    if (init?.method === 'POST') { posts++; return starting.promise; }
    lists++;
    return lists === 1 ? respond([]) : restoration.promise;
  }));
  await render();
  await click('Start OCR for remaining papers');
  await act(async () => root.render(<div />));
  await render();
  expect(button('Starting OCR…').disabled).toBe(true);
  await act(async () => starting.resolve(respond(batch())));
  await act(async () => restoration.resolve(respond([])));
  expect(posts).toBe(1);
  expect(container.querySelector('[aria-label="Active OCR batches"]')).not.toBeNull();
  expect(button('Start OCR for remaining papers').disabled).toBe(true);
  expect(container.textContent).not.toContain('Loading OCR runs');
});

it('shows cancellation pending, then authoritative stopped counts without filling unfinished bars', async () => {
  const stopping = heldResponse();
  const running = batch({ items: [item('one', { status: 'running', completed_pages: 3 }), item('two')] });
  const stopped = batch({ status: 'stopped', items: [
    item('one', { status: 'stopped', completed_pages: 4 }), item('two', { status: 'stopped' }),
  ] });
  const posts: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (path.endsWith('/settings')) return respond(settings);
    if (init?.method === 'POST') { posts.push(path); return stopping.promise; }
    return respond([running]);
  }));
  await render();
  await click('Stop batch');
  expect(button('Stopping batch…').disabled).toBe(true);
  expect(container.textContent).toContain('Cancellation requested');
  await act(async () => button('Stopping batch…').click());
  expect(posts).toEqual(['/api/documents/ocr-batches/batch-one/stop']);
  await act(async () => stopping.resolve(respond(stopped)));
  expect(container.querySelector('[aria-label="Active OCR batches"]')).toBeNull();
  await expandHistory();
  expect(container.textContent).toContain('0 running0 queued0 completed0 failed2 stopped');
  expect(progress('Papers completed').value).toBe(0);
  expect(progress('Pages completed').value).toBe(4);
  expect(progress('Pages completed').max).toBe(20);
  expect(button('Start OCR for remaining papers').disabled).toBe(false);
});

it('polls only while active, never overlaps polls, and ignores a stale read after stop', async () => {
  vi.useFakeTimers();
  const polling = heldResponse();
  let lists = 0;
  let signal: AbortSignal | undefined;
  const running = batch({ items: [item('one', { status: 'running', completed_pages: 2 })] });
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (path.endsWith('/settings')) return respond(settings);
    if (init?.method === 'POST') return respond(batch({ status: 'stopped', items: [item('one', { status: 'stopped', completed_pages: 2 })] }));
    lists++;
    if (lists === 1) return respond([running]);
    signal = init?.signal as AbortSignal;
    return polling.promise;
  }));
  await render();
  await act(async () => vi.advanceTimersByTimeAsync(OCR_POLL_INTERVAL));
  expect(lists).toBe(2);
  await act(async () => vi.advanceTimersByTimeAsync(OCR_POLL_INTERVAL * 5));
  expect(lists).toBe(2);
  await click('Stop batch');
  expect(signal?.aborted).toBe(true);
  await act(async () => polling.resolve(respond([running])));
  await expandHistory();
  expect(container.textContent).toContain('1 stopped');
  expect(container.querySelector('[aria-label="Active OCR batches"]')).toBeNull();
  await act(async () => vi.advanceTimersByTimeAsync(OCR_POLL_INTERVAL * 5));
  expect(lists).toBe(2);
  expect(vi.getTimerCount()).toBe(0);
});

it('aborts pending reads and cleans up timers on unmount', async () => {
  vi.useFakeTimers();
  const polling = heldResponse();
  let lists = 0;
  let signal: AbortSignal | undefined;
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input).endsWith('/settings')) return respond(settings);
    lists++;
    if (lists === 1) return respond([batch()]);
    signal = init?.signal as AbortSignal;
    return polling.promise;
  }));
  await render();
  await act(async () => vi.advanceTimersByTimeAsync(OCR_POLL_INTERVAL));
  await act(async () => root.render(<div />));
  expect(signal?.aborted).toBe(true);
  await act(async () => polling.resolve(respond([batch()])));
  await act(async () => vi.advanceTimersByTimeAsync(OCR_POLL_INTERVAL * 3));
  expect(lists).toBe(2);
  expect(vi.getTimerCount()).toBe(0);
});

it('exposes fetch and start errors, retries safely, and restores interrupted history without restarting', async () => {
  let lists = 0;
  let posts = 0;
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input).endsWith('/settings')) return respond(settings);
    if (init?.method === 'POST') { posts++; return respond({ detail: 'OCR runtime unavailable' }, 503); }
    lists++;
    return lists === 1 ? respond({ detail: 'Runs unavailable' }, 503) : respond([batch({ status: 'interrupted', items: [item('one', { status: 'stopped' })] })]);
  }));
  await render();
  expect(container.textContent).toContain('Runs unavailable');
  expect(button('Start OCR for remaining papers').disabled).toBe(true);
  await click('Refresh runs');
  await expandHistory();
  expect(container.textContent).toContain('interrupted by a server restart');
  expect(posts).toBe(0);
  await click('Start OCR for remaining papers');
  expect(container.textContent).toContain('OCR runtime unavailable');
  expect(posts).toBe(1);
  expect(button('Start OCR for remaining papers').disabled).toBe(true);
  await click('Refresh runs');
  expect(container.textContent).not.toContain('OCR runtime unavailable');
  expect(button('Start OCR for remaining papers').disabled).toBe(false);
});

it('retains active control and retries polling after a transient progress failure', async () => {
  vi.useFakeTimers();
  let lists = 0;
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input).endsWith('/settings')) return respond(settings);
    if (init?.method === 'POST') return respond({ detail: 'Stop temporarily unavailable' }, 503);
    lists++;
    if (lists === 2) return respond({ detail: 'Progress temporarily unavailable' }, 503);
    return respond([batch({ items: [item('one', { status: 'running', completed_pages: lists })] })]);
  }));
  await render();
  await act(async () => vi.advanceTimersByTimeAsync(OCR_POLL_INTERVAL));
  expect(container.textContent).toContain('Progress temporarily unavailable');
  expect(pageCount('Pages for Paper one').textContent).toContain('1 / 10');
  await act(async () => vi.advanceTimersByTimeAsync(OCR_POLL_INTERVAL));
  expect(pageCount('Pages for Paper one').textContent).toContain('3 / 10');
  expect(container.textContent).not.toContain('Progress temporarily unavailable');
  await click('Stop batch');
  expect(container.textContent).toContain('Stop temporarily unavailable');
  expect(button('Stop batch').disabled).toBe(false);
  expect(container.querySelector('[aria-label="Active OCR batches"]')).not.toBeNull();
});

it('shows only running papers as progress changes and keeps just two collapsed batches in history', async () => {
  vi.useFakeTimers();
  let current = batch({ items: [
    item('live', { status: 'running', completed_pages: 2 }),
    item('queued'),
    item('done', { status: 'completed', completed_pages: 10 }),
    item('failed', { status: 'failed', error: 'Previous page error' }),
  ] });
  const older = (id: string, day: number) => batch({
    id, status: 'completed', created_at: `2026-10-${day}T10:00:00Z`,
    items: [item(id, { status: 'completed', completed_pages: 10 })],
  });
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => respond(
    String(input).endsWith('/settings') ? settings : [older('oldest', 19), current, older('newest', 21), older('middle', 20)],
  )));
  await render();
  expect([...container.querySelectorAll('.ocr-run-items a')].map((link) => link.textContent)).toEqual(['Paper live']);
  expect(container.textContent).toContain('1 running1 queued1 completed1 failed');
  expect(progress('Pages completed').value).toBe(12);
  const details = container.querySelectorAll('details');
  expect(details).toHaveLength(2);
  expect([...details].every((entry) => !entry.open)).toBe(true);
  expect(container.querySelector('aside .ocr-run')).toBeNull();
  await expandHistory();
  expect([...container.querySelectorAll('aside .ocr-run-items a')].map((link) => link.textContent)).toEqual(['Paper newest', 'Paper middle']);
  expect(container.textContent).not.toContain('Paper oldest');

  current = { ...current, items: current.items.map((entry) => entry.document_id === 'live'
    ? { ...entry, status: 'completed', completed_pages: 10 }
    : entry.document_id === 'queued' ? { ...entry, status: 'running', completed_pages: 1 } : entry) };
  await act(async () => vi.advanceTimersByTimeAsync(OCR_POLL_INTERVAL));
  const active = container.querySelector('[aria-label="Active OCR batches"]')!;
  expect([...active.querySelectorAll('.ocr-run-items a')].map((link) => link.textContent)).toEqual(['Paper queued']);
  expect(progress('Pages completed').value).toBe(21);
});
