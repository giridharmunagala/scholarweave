// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { Link, RouterProvider } from '../../app/router';
import BriefPage from './BriefPage';
import type { BriefCatalog, BriefEdition, BriefItem, BriefHighlight } from './api';

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
let hasCachedEdition: boolean;
let confirmClear: ReturnType<typeof vi.fn>;
let briefCatalog: BriefCatalog;
let providerModels: unknown[];
let archives: BriefHighlight[];

const highlights: BriefHighlight = {
  day: '2026-10-09', created_at: stamp,
  text: 'Adaptive reasoning is a central theme, with preliminary abstract-backed results.\n\nPublisher headlines indicate new inference work; full sources have not been reviewed.',
  model_reference: { provider_profile_id: 'chat-profile', model: 'chat-model' },
  source_hash: 'source-hash', item_count: 2, batch_count: 1,
  input_token_limit: 32768, token_counting: 'utf8-byte-upper-bound',
  workspace_path: 'knowledge/morning-brief/2026-10-09.json',
};

beforeEach(() => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  window.history.replaceState({}, '', '/brief');
  vi.stubGlobal('scrollTo', vi.fn());
  HTMLElement.prototype.scrollIntoView = vi.fn();
  confirmClear = vi.fn(() => true);
  vi.stubGlobal('confirm', confirmClear);
  failDownload = false;
  failRefresh = false;
  hasCachedEdition = true;
  calls = [];
  briefCatalog = structuredClone(catalog);
  providerModels = [];
  archives = [];
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
    if (path === '/api/brief/catalog') return Response.json(briefCatalog);
    if (path === '/api/providers') return Response.json(providerModels);
    if (path === '/api/brief/summaries') return Response.json(archives);
    if (path === '/api/brief/highlights') {
      edition = { ...edition, highlights: { completed_parts: 0, total_parts: 0, ...edition.highlights, status: 'running' } };
      return Response.json(edition);
    }
    if (path === '/api/brief' && method === 'GET') return Response.json(hasCachedEdition ? edition : {});
    if (path === '/api/brief/preferences') return Response.json({ ...briefCatalog, preferences: body,
      summary_model_reference: body.summary_model_reference ?? briefCatalog.summary_model_reference });
    if (path === '/api/brief/refresh') return failRefresh
      ? Response.json({ message: 'Connection interrupted' }, { status: 503 }) : Response.json(edition);
    if (path === '/api/brief' && method === 'DELETE') {
      edition = { day: '2026-10-09', cleared: true, items: [], sources: [], temporary_bytes: 0,
        highlights: edition.highlights?.summary ? { status: 'ready', summary: edition.highlights.summary,
          completed_parts: 0, total_parts: 0 } : undefined };
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
const mountWithExit = async () => {
  await act(async () => root.render(<RouterProvider><><BriefPage /><Link to="/library">Leave brief</Link></></RouterProvider>));
};

it('shows cached highlights before sources without generating again and keeps the daily archive after clearing', async () => {
  edition.highlights = { status: 'ready', summary: highlights, completed_parts: 1, total_parts: 1 };
  archives = [highlights];
  await mount();
  const summary = container.querySelector('.brief-highlights')!;
  const sources = container.querySelector('.brief-reading-layout')!;
  expect(summary.compareDocumentPosition(sources) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(summary.textContent).toContain('Adaptive reasoning is a central theme');
  expect(calls.some((call) => call.path === '/api/brief/highlights')).toBe(false);
  expect(calls.some((call) => call.path === '/api/brief/summaries')).toBe(false);
  await click('Clear fetched data');
  expect(container.textContent).toContain('Adaptive reasoning is a central theme');
  expect(container.textContent).not.toContain(paper.title);
  await click('Past highlights');
  const history = container.querySelector('[aria-label="Past highlights"]')!;
  expect(history.textContent).toContain('2026-10-09');
  expect(history.textContent).toContain('Publisher headlines indicate new inference work');
});

it('persists an independent brief model choice without changing chat settings', async () => {
  briefCatalog.summary_model_reference = highlights.model_reference;
  providerModels = [{ id: 'brief-profile', name: 'Brief provider', kind: 'openai_compatible', state: 'active',
    models: [{ name: 'brief-model', enabled: true, capabilities: ['chat'] }] }];
  await mount();
  await openSettings();
  const picker = container.querySelector<HTMLButtonElement>('[aria-label="Morning Brief model"]')!;
  expect(picker.textContent).toContain('Same as chat');
  await act(async () => picker.click());
  const option = [...container.querySelectorAll<HTMLButtonElement>('[role="option"]')]
    .find((value) => value.textContent?.includes('brief-model'))!;
  await act(async () => option.click());
  const preferenceCall = calls.find((call) => call.path === '/api/brief/preferences')!;
  expect(preferenceCall.body).toMatchObject({
    summary_model_reference: { provider_profile_id: 'brief-profile', model: 'brief-model' },
  });
  expect(calls.some((call) => call.path === '/api/settings' && call.method === 'PUT')).toBe(false);
  expect(calls.some((call) => call.path === '/api/brief/refresh')).toBe(false);
  await closeSettings();
  await click('Generate highlights');
  expect(calls.find((call) => call.path === '/api/brief/highlights')?.body).toEqual({
    model_reference: { provider_profile_id: 'brief-profile', model: 'brief-model' },
  });
  expect(button('Download PDF').disabled).toBe(false);
});

it('generates highlights only after the user explicitly asks', async () => {
  providerModels = [{ id: 'brief-profile', name: 'Brief provider', kind: 'openai_compatible', state: 'active',
    models: [{ name: 'brief-model', enabled: true, capabilities: ['chat'] }] }];
  await mount();
  expect(calls.some((call) => call.path === '/api/brief/highlights')).toBe(false);
  await openSettings();
  const picker = container.querySelector<HTMLButtonElement>('[aria-label="Morning Brief model"]')!;
  await act(async () => picker.click());
  const option = [...container.querySelectorAll<HTMLButtonElement>('[role="option"]')]
    .find((value) => value.textContent?.includes('brief-model'))!;
  await act(async () => option.click());
  expect(calls.some((call) => call.path === '/api/brief/highlights')).toBe(false);
  await closeSettings();
  await click('Generate highlights');
  expect(calls.find((call) => call.path === '/api/brief/highlights')?.body).toEqual({
    model_reference: { provider_profile_id: 'brief-profile', model: 'brief-model' },
  });
});

it('polls only active generation while the sources stay usable', async () => {
  vi.useFakeTimers();
  try {
    edition.highlights = { status: 'running', completed_parts: 1, total_parts: 3 };
    await mount();
    expect(container.textContent).toContain('1/3 stages complete');
    expect(button('Download PDF').disabled).toBe(false);
    expect(button('Refresh').disabled).toBe(false);
    edition = { ...edition, highlights: { status: 'ready', summary: highlights, completed_parts: 3, total_parts: 3 } };
    await act(async () => vi.advanceTimersByTimeAsync(1500));
    expect(container.textContent).toContain('Adaptive reasoning is a central theme');
    const reads = calls.filter((call) => call.path === '/api/brief' && call.method === 'GET').length;
    await act(async () => vi.advanceTimersByTimeAsync(4500));
    expect(calls.filter((call) => call.path === '/api/brief' && call.method === 'GET')).toHaveLength(reads);
    expect(calls.some((call) => call.path === '/api/brief/refresh')).toBe(false);
  } finally { vi.useRealTimers(); }
});

it('surfaces failed generation and allows a retry without hiding a previous summary', async () => {
  briefCatalog.summary_model_reference = highlights.model_reference;
  edition.highlights = { status: 'failed', summary: highlights, error: 'The model returned an empty summary.',
    completed_parts: 0, total_parts: 1 };
  await mount();
  expect(container.querySelector('[role="alert"]')?.textContent).toContain('empty summary');
  expect(container.textContent).toContain('Adaptive reasoning is a central theme');
  expect(button('Regenerate highlights').disabled).toBe(false);
  await click('Regenerate highlights');
  expect(container.textContent).toContain('previous summary stays visible');
});
const button = (text: string) => [...container.querySelectorAll('button')].find((value) => value.textContent?.trim() === text)!;
const openSettings = async () => {
  await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Brief settings"]')!.click());
};
const closeSettings = async () => {
  await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Close brief settings"]')!.click());
};
const click = async (text: string) => {
  if (text === 'Clear fetched data') await openSettings();
  await act(async () => button(text).click());
};
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
  expect(calls.some((call) => call.path.endsWith('/refresh'))).toBe(false);
  await click('Test-time compute');
  expect(container.textContent).not.toContain(page.title);
  await click('All interests');
  await input('Search brief headlines', 'architecture');
  expect(container.textContent).not.toContain(paper.title);
  expect(container.textContent).toContain(page.title);
});

it('shows cached headlines before a slower interests/source catalog finishes loading', async () => {
  let finishCatalog!: (response: Response) => void;
  const fetch = globalThis.fetch;
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) =>
    String(input).endsWith('/brief/catalog')
      ? new Promise<Response>((resolve) => { finishCatalog = resolve; })
      : fetch(input, init),
  ));
  await mount();
  expect(container.textContent).toContain(paper.title);
  expect(calls.some((call) => call.path.endsWith('/refresh'))).toBe(false);
  await act(async () => finishCatalog(Response.json(catalog)));
  expect(container.textContent).not.toContain('Loading your brief');
  expect(container.textContent).toContain(paper.title);
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
  hasCachedEdition = false;
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

it('allows leaving while reads are pending and ignores their stale completion', async () => {
  let finishEdition!: (response: Response) => void;
  const fetch = globalThis.fetch;
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL(String(input), 'http://localhost').pathname;
    if (path === '/api/brief' && (init?.method ?? 'GET') === 'GET') {
      return new Promise<Response>((resolve) => { finishEdition = resolve; });
    }
    return fetch(input, init);
  }));
  await mountWithExit();
  expect(button('Refresh').disabled).toBe(true);
  await act(async () => container.querySelector<HTMLAnchorElement>('a[href="/library"]')!.click());
  expect(window.location.pathname).toBe('/library');
  await act(async () => root.render(<RouterProvider><div>Another page</div></RouterProvider>));
  await act(async () => finishEdition(Response.json(edition)));
  expect(container.textContent).toContain('Another page');
  expect(container.textContent).not.toContain(paper.title);
});

it('guards only unsaved interest edits during navigation', async () => {
  await mountWithExit();
  await openSettings();
  const field = [...container.querySelectorAll('label')]
    .find((label) => label.textContent?.includes('Additional interests'))!.querySelector('input')!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(field, 'unsaved interest');
    field.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await closeSettings();
  confirmClear.mockReturnValue(false);
  await act(async () => container.querySelector<HTMLAnchorElement>('a[href="/library"]')!.click());
  expect(confirmClear).toHaveBeenCalledWith(expect.stringContaining('unsaved Morning Brief interest changes'));
  expect(window.location.pathname).toBe('/brief');
  confirmClear.mockReturnValue(true);
  await act(async () => container.querySelector<HTMLAnchorElement>('a[href="/library"]')!.click());
  expect(window.location.pathname).toBe('/library');
});

it('keeps configuration off the reading surface and retains interest drafts when dismissed', async () => {
  await mount();
  const dialog = container.querySelector<HTMLDialogElement>('#brief-settings')!;
  expect(dialog.open).toBe(false);
  expect(container.querySelector('[aria-label="Morning Brief model"]')!.closest('dialog')).toBe(dialog);
  expect(button('Clear fetched data').closest('dialog')).toBe(dialog);
  expect(button('Generate highlights').closest('dialog')).toBeNull();
  const trigger = container.querySelector<HTMLButtonElement>('[aria-label="Brief settings"]')!;
  trigger.focus();
  await openSettings();
  const field = [...dialog.querySelectorAll('label')].find((label) => label.textContent?.includes('Additional interests'))!.querySelector('input')!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(field, 'Keep my interest');
    field.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await closeSettings();
  expect(dialog.open).toBe(false);
  expect(document.activeElement).toBe(trigger);
  expect(calls.some((call) => call.method !== 'GET')).toBe(false);
  await openSettings();
  expect(field.value).toBe('Keep my interest');
});

it('fetches headlines only when there is no daily edition and reuses it after navigation', async () => {
  hasCachedEdition = false;
  await mount();
  expect(calls.filter((call) => call.path.endsWith('/refresh')).map((call) => call.body)).toEqual([{ force: false }]);
  expect(calls.some((call) => call.path === '/api/brief/highlights')).toBe(false);
  hasCachedEdition = true;
  await act(async () => root.render(<RouterProvider><div>Another page</div></RouterProvider>));
  await mount();
  expect(container.textContent).toContain(paper.title);
  expect(calls.filter((call) => call.path.endsWith('/refresh'))).toHaveLength(1);
  await click('Clear fetched data');
  await act(async () => root.render(<RouterProvider><div>Another page</div></RouterProvider>));
  await mount();
  expect(container.textContent).toContain('A clean slate.');
  expect(calls.filter((call) => call.path.endsWith('/refresh'))).toHaveLength(1);
});

it('persists interest settings and refreshes explicitly', async () => {
  await mount();
  await openSettings();
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
  expect(container.querySelector<HTMLDialogElement>('#brief-settings')!.open).toBe(false);
});

it('keeps a separately saved model choice when saving an older interests draft', async () => {
  providerModels = [{ id: 'brief-profile', name: 'Brief provider', kind: 'openai_compatible', state: 'active',
    models: [{ name: 'brief-model', enabled: true, capabilities: ['chat'] }] }];
  await mount();
  await openSettings();
  const interest = [...container.querySelectorAll('label')]
    .find((label) => label.textContent?.includes('Additional interests'))!.querySelector('input')!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(interest, 'diffusion models');
    interest.dispatchEvent(new Event('input', { bubbles: true }));
  });
  const picker = container.querySelector<HTMLButtonElement>('[aria-label="Morning Brief model"]')!;
  await act(async () => picker.click());
  const option = [...container.querySelectorAll<HTMLButtonElement>('[role="option"]')]
    .find((value) => value.textContent?.includes('brief-model'))!;
  await act(async () => option.click());
  const modelReference = { provider_profile_id: 'brief-profile', model: 'brief-model' };
  expect(calls.filter((call) => call.path === '/api/brief/preferences')[0].body)
    .toMatchObject({ summary_model_reference: modelReference });
  await act(async () => container.querySelector('.brief-settings')!
    .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
  const writes = calls.filter((call) => call.path === '/api/brief/preferences');
  expect(writes).toHaveLength(2);
  expect(writes[1].body).toMatchObject({
    extra_interests: 'diffusion models',
    summary_model_reference: modelReference,
  });
});

it('starts with a short reading list and lets the reader expand it', async () => {
  edition.items = Array.from({ length: 9 }, (_, index) => ({ ...paper, id: `paper-${index}`, title: `Reasoning paper ${index}` }));
  await mount();
  expect(container.querySelectorAll('.brief-headline')).toHaveLength(5);
  await act(async () => container.querySelector<HTMLButtonElement>('.brief-more')!.click());
  expect(container.querySelectorAll('.brief-headline')).toHaveLength(9);
});
