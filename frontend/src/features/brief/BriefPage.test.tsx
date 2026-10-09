// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { RouterProvider } from '../../app/router';
import BriefPage from './BriefPage';
import type { BriefCatalog, BriefEdition, BriefItem } from './api';

const stamp = '2026-10-09T09:00:00Z';
const paper: BriefItem = {
  id: 'paper-1', title: 'Adaptive reasoning for language models', url: 'https://arxiv.org/abs/2610.01234',
  kind: 'paper', source_id: 'arxiv', source_name: 'arXiv', published_at: stamp,
  arxiv_id: '2610.01234v2', announcement: 'replace', abstract: 'Test-time compute improves reasoning.',
  authors: ['A. Researcher'], topics: ['Test-time compute'], downloaded: false, size_bytes: 0,
};
const page: BriefItem = {
  id: 'page-1', title: 'A new inference architecture', url: 'https://openai.com/research/inference',
  kind: 'lab', source_id: 'openai', source_name: 'OpenAI', published_at: null,
  abstract: '', topics: ['Inference efficiency'], downloaded: false, size_bytes: 0,
};
const catalog: BriefCatalog = {
  sources: [{ id: 'arxiv', name: 'arXiv', url: 'https://rss.arxiv.org', kind: 'paper' }, { id: 'openai', name: 'OpenAI', url: 'https://openai.com', kind: 'lab' }],
  topics: [{ id: 'test-time', label: 'Test-time compute' }, { id: 'inference', label: 'Inference efficiency' }],
  preferences: { topic_ids: ['test-time', 'inference'], source_ids: ['arxiv', 'openai'], extra_interests: '', exclude_terms: [], lookback_days: 7 },
};
let container: HTMLDivElement;
let root: Root;
let edition: BriefEdition;
let calls: { path: string; method: string; body: unknown }[];
let failDownload: boolean;
let failRefresh: boolean;
let confirmClear: ReturnType<typeof vi.fn>;

beforeEach(() => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  window.history.replaceState({}, '', '/brief');
  vi.stubGlobal('scrollTo', vi.fn());
  confirmClear = vi.fn(() => true);
  vi.stubGlobal('confirm', confirmClear);
  failDownload = false;
  failRefresh = false;
  calls = [];
  edition = {
    day: '2026-10-09', fetched_at: stamp, cleared: false, temporary_bytes: 0,
    items: [{ ...paper }, { ...page }],
    sources: [{ source_id: 'arxiv', name: 'arXiv', fetched_at: stamp, count: 1 },
      { source_id: 'openai', name: 'OpenAI', fetched_at: stamp, count: 0, error: 'HTTP 503' }],
  };
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL(String(input), 'http://localhost').pathname;
    const method = init?.method ?? 'GET';
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    calls.push({ path, method, body });
    if (path === '/api/brief/catalog') return Response.json(catalog);
    if (path === '/api/brief/preferences') return Response.json({ ...catalog, preferences: body });
    if (path === '/api/brief/refresh') return failRefresh
      ? Response.json({ message: 'Connection interrupted' }, { status: 503 }) : Response.json(edition);
    if (path === '/api/brief' && method === 'DELETE') {
      edition = { day: '2026-10-09', cleared: true, items: [], sources: [], temporary_bytes: 0 };
      return Response.json(edition);
    }
    const match = path.match(/\/api\/brief\/items\/([^/]+)(?:\/(fetch|keep))?$/);
    if (match) {
      const item = edition.items!.find((value) => value.id === match[1])!;
      if (match[2] === 'fetch') {
        if (failDownload) return Response.json({ message: 'Remote PDF unavailable' }, { status: 400 });
        item.downloaded = true;
        item.size_bytes = 1024;
        edition.temporary_bytes = (edition.temporary_bytes ?? 0) + 1024;
        return Response.json({ item, media_type: item.kind === 'paper' ? 'application/pdf' : 'text/plain',
          text: item.kind === 'paper' ? null : 'Downloaded technical article content.' });
      }
      if (match[2] === 'keep') {
        if (item.kind === 'paper') item.saved_document_id = 'saved-paper-1';
        else item.saved_path = 'knowledge/inference--note-1.md';
        return Response.json(item);
      }
      if (method === 'DELETE') {
        edition.items = edition.items!.filter((value) => value.id !== item.id);
        edition.temporary_bytes = Math.max(0, (edition.temporary_bytes ?? 0) - (item.size_bytes ?? 0));
        return Response.json(edition);
      }
    }
    throw new Error(`Unexpected ${method} ${path}`);
  }));
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});
const mount = async () => { await act(async () => root.render(<RouterProvider><BriefPage /></RouterProvider>)); };
const button = (text: string) => [...container.querySelectorAll('button')].find((value) => value.textContent?.trim() === text)!;
const click = async (text: string) => { await act(async () => button(text).click()); };
async function input(label: string, value: string) {
  const field = container.querySelector<HTMLInputElement>(`[aria-label="${label}"]`)!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(field, value);
    field.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

it('shows headlines without downloading content and transparently labels revisions, dates and failures', async () => {
  await mount();
  expect(container.textContent).toContain(paper.title);
  expect(container.textContent).toContain('Revised preprint');
  expect(container.textContent).toContain('Date unverified');
  expect(container.textContent).toContain('1 source could not refresh');
  expect(container.textContent).toContain('HTTP 503');
  expect(calls.filter((call) => call.path.includes('/items/'))).toHaveLength(0);
  expect(container.querySelector('iframe')).toBeNull();
  expect(calls.find((call) => call.path.endsWith('/refresh'))!.body).toEqual({ force: false });
  await click('Test-time compute');
  expect(container.textContent).not.toContain(page.title);
  await click('All interests');
  await input('Search brief headlines', 'architecture');
  expect(container.textContent).not.toContain(paper.title);
  expect(container.textContent).toContain(page.title);
});

it('downloads into a temporary reader, keeps explicitly, and clears only fetched data', async () => {
  await mount();
  await click('Download PDF');
  expect(container.querySelector('iframe')?.getAttribute('src')).toBe('/api/brief/items/paper-1/raw');
  expect(container.textContent).toContain('TEMPORARY PREVIEW');
  expect(calls.some((call) => call.path.endsWith('/keep'))).toBe(false);
  await click('Keep in library');
  expect(container.textContent).toContain('Kept in your library');
  expect(container.querySelector('a[href="/library?paper=saved-paper-1"]')).not.toBeNull();
  await click('Clear fetched data');
  expect(confirmClear).toHaveBeenCalledWith(expect.stringContaining('Everything you kept in the library is safe'));
  expect(container.querySelector('iframe')).toBeNull();
  expect(container.textContent).toContain('A clean slate.');
  expect(container.textContent).not.toContain(paper.title);
  expect(calls.some((call) => call.method === 'DELETE' && call.path.startsWith('/api/documents'))).toBe(false);
  expect(calls.filter((call) => call.method === 'DELETE').map((call) => call.path)).toEqual(['/api/brief']);
});

it('downloads page text without writing a note and saves a kept page into existing notes', async () => {
  await mount();
  await click('Download page');
  expect(container.querySelector('.brief-page-text')?.textContent).toBe('Downloaded technical article content.');
  expect(calls.some((call) => call.path.endsWith('/keep'))).toBe(false);
  await click('Keep in library');
  expect(container.querySelector('a[href="/library/notes?path=knowledge%2Finference--note-1.md"]')).not.toBeNull();
  await click('Discard');
  expect(container.querySelector('.brief-reader')).toBeNull();
  expect(container.textContent).not.toContain(page.title);
  expect(container.textContent).toContain('Your library copy is unchanged');
});

it('retains headlines on download failure and never presents a successful preview', async () => {
  failDownload = true;
  await mount();
  await click('Download PDF');
  expect(container.querySelector('[role="alert"]')?.textContent).toContain('Remote PDF unavailable');
  expect(container.querySelector('.brief-reader')).toBeNull();
  expect(container.textContent).toContain(paper.title);
  expect(calls.some((call) => call.path.endsWith('/keep'))).toBe(false);
});

it('allows retry after an initial connection error and respects clear cancellation', async () => {
  failRefresh = true;
  await mount();
  expect(container.textContent).toContain('Connection interrupted');
  failRefresh = false;
  await click('Try again');
  expect(container.textContent).toContain(paper.title);
  confirmClear.mockReturnValue(false);
  await click('Clear fetched data');
  expect(calls.some((call) => call.method === 'DELETE')).toBe(false);
  expect(container.textContent).toContain(paper.title);
});

it('persists interest settings and refreshes explicitly', async () => {
  await mount();
  await click('Interests & sources');
  const field = [...container.querySelectorAll('label')].find((label) => label.textContent?.includes('Additional interests'))!.querySelector('input')!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(field, 'mixture of experts, diffusion');
    field.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await act(async () => container.querySelector('.brief-settings')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
  const write = calls.find((call) => call.path.endsWith('/preferences'))!;
  expect(write.method).toBe('PUT');
  expect(write.body).toMatchObject({ extra_interests: 'mixture of experts, diffusion', topic_ids: ['test-time', 'inference'] });
  const refreshes = calls.filter((call) => call.path.endsWith('/refresh'));
  expect(refreshes[refreshes.length - 1].body).toEqual({ force: true });
  expect(container.querySelector('.brief-settings')).toBeNull();
});

it('starts with a short reading list and lets the reader expand it', async () => {
  edition.items = Array.from({ length: 9 }, (_, index) => ({ ...paper, id: `paper-${index}`, title: `Reasoning paper ${index}` }));
  await mount();
  expect(container.querySelectorAll('.brief-headline')).toHaveLength(5);
  await act(async () => container.querySelector<HTMLButtonElement>('.brief-more')!.click());
  expect(container.querySelectorAll('.brief-headline')).toHaveLength(9);
});
