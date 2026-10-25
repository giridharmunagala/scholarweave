// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { RouterProvider } from '../../app/router';
import { OcrBatchPanel, needsOcr } from './OcrBatchPanel';
import type { components } from '../../api/schema.generated';
import type { OcrBatch } from '../runs/api';

type Paper = components['schemas']['DocumentSummaryResponse'];
const paper = (id: string, metadata: Record<string, unknown> = {}, status = 'ready', page_count = 1): Paper => ({
  id, title: `Paper ${id}`, status, metadata, usefulness: 'unrated',
  source_filename: `${id}.pdf`, content_type: 'application/pdf', page_count,
  created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
});
const respond = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'content-type': 'application/json' },
});
const settings = { ocr_engine: 'surya', ocr_surya_model: 'configured-surya', ocr_surya_parallel: 2 };
const options = { total_pages: 1, recommended_mode: 'embedded', ocr_available: true, ocr_engine: 'surya' };
const active: OcrBatch = {
  id: 'restored', status: 'running', created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
  finished_at: null, parallel_requests: 2,
  items: [{ document_id: 'native', title: 'Paper native', status: 'running', completed_pages: 1, total_pages: 3, phase_label: null, error: null }],
};
let element: HTMLDivElement;
let root: Root;
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  element = document.createElement('div');
  root = createRoot(element);
});
afterEach(async () => { await act(async () => root.unmount()); vi.unstubAllGlobals(); });
const render = async (papers: Paper[]) => {
  await act(async () => root.render(<RouterProvider><OcrBatchPanel papers={papers} /></RouterProvider>));
};
const start = () => [...element.querySelectorAll('button')].find((button) => button.textContent?.startsWith('Start OCR'))!;

it('starts a durable job for unique eligible selection and links to Runs without browser ingestion', async () => {
  const posts: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (path.endsWith('/settings')) return respond(settings);
    if (path.endsWith('/ingestion-options')) return respond(options);
    if (init?.method === 'POST') {
      posts.push(path);
      expect(JSON.parse(String(init.body))).toEqual({ document_ids: ['native', 'partial'] });
      return respond(active);
    }
    return respond([]);
  }));
  await render([
    paper('native'), paper('partial', { ocr_pages: [1] }, 'ready', 3), paper('complete', { ocr_pages: [1] }),
    paper('processing', {}, 'processing'), paper('native'),
  ]);
  expect(posts).toEqual([]);
  expect(element.textContent).toContain('configured-surya');
  expect(element.textContent).toContain('Shared page-request limit: 2');
  expect(start().textContent).toBe('Start OCR for 2 selected papers');
  await act(async () => start().click());
  expect(posts).toEqual(['/api/documents/ocr-batches']);
  expect(start().disabled).toBe(true);
  expect(element.querySelector('a[href="/runs"]')).not.toBeNull();
  expect(element.textContent).toContain('Open Runs to see progress or stop');
});

it('restores an active server job after reopening and does not start a duplicate', async () => {
  const fetch = vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    return respond(path.endsWith('/settings') ? settings : path.endsWith('/ingestion-options') ? options : [active]);
  });
  vi.stubGlobal('fetch', fetch);
  await render([paper('native')]);
  expect(start().disabled).toBe(true);
  await act(async () => root.unmount());
  root = createRoot(element);
  await render([paper('native')]);
  expect(start().disabled).toBe(true);
  expect(element.textContent).toContain('running');
  expect(fetch.mock.calls.filter(([path]) => String(path).endsWith('/ocr-batches'))).toHaveLength(2);
});

it('disables OCR when the runtime is unavailable and allows retry after loading errors', async () => {
  let failing = true;
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path.endsWith('/settings')) return respond(settings);
    if (path.endsWith('/ingestion-options')) return failing
      ? respond({ detail: 'OCR configuration unavailable' }, 503) : respond({ ...options, ocr_available: false });
    return respond([]);
  }));
  await render([paper('native')]);
  expect(element.textContent).toContain('OCR configuration unavailable');
  expect(start().disabled).toBe(true);
  failing = false;
  await act(async () => [...element.querySelectorAll('button')].find((button) => button.textContent?.startsWith('Retry'))!.click());
  expect(element.textContent).toContain('OCR runtime is unavailable');
  expect(start().disabled).toBe(true);
});

it('blocks duplicate clicks while the start request is pending and exposes request errors', async () => {
  let finish!: (response: Response) => void;
  let posts = 0;
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (path.endsWith('/settings')) return respond(settings);
    if (path.endsWith('/ingestion-options')) return respond(options);
    if (init?.method === 'POST') { posts++; return new Promise<Response>((resolve) => { finish = resolve; }); }
    return respond([]);
  }));
  await render([paper('native')]);
  await act(async () => { start().click(); start().click(); });
  expect(posts).toBe(1);
  expect(element.textContent).toContain('Starting OCR');
  await act(async () => finish(respond({ detail: 'OCR start failed' }, 503)));
  expect(element.textContent).toContain('OCR start failed');
  expect(start().disabled).toBe(true);
});

it('recognizes incomplete coverage, duplicate or out-of-range pages, fallback, and legacy OCR metadata', () => {
  expect(needsOcr(paper('legacy', { ocr_pages: [1] }))).toBe(false);
  expect(needsOcr(paper('forced', { extraction_mode: 'ocr' }, 'ready', 3))).toBe(false);
  expect(needsOcr(paper('empty', { extraction_mode: 'ocr', ocr_pages: [] }))).toBe(true);
  expect(needsOcr(paper('partial', { ocr_pages: [1] }, 'ready', 3))).toBe(true);
  expect(needsOcr(paper('duplicates', { ocr_pages: [1, 1, 1] }, 'ready', 3))).toBe(true);
  expect(needsOcr(paper('out-of-range', { ocr_pages: [1, 2, 4] }, 'ready', 3))).toBe(true);
  expect(needsOcr(paper('full', { ocr_pages: [1, 2, 3] }, 'ready', 3))).toBe(false);
  expect(needsOcr(paper('fallback', { extraction_mode: 'ocr', ocr_fallback_pages: [1] }))).toBe(true);
  expect(needsOcr(paper('skipped', { extraction_mode: 'ocr', ocr_skipped_pages: [1] }))).toBe(true);
  expect(needsOcr(paper('native', { extraction_mode: 'embedded' }))).toBe(true);
  expect(needsOcr(paper('processing', {}, 'processing'))).toBe(false);
});
