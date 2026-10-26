// @vitest-environment jsdom
import { act, StrictMode, useState } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Link, RouterProvider, useLocation } from '../../app/router';
import { LibraryTabs } from '../../shared/components/Ui';
import PapersPage from './PapersPage';
import { cleanQuote } from './PdfReader';
import { splitReadingContext } from '../chat/ChatPage';
import type { OcrBatch } from '../runs/api';
import { OCR_POLL_INTERVAL } from '../runs/useOcrBatches';

// pdf.js needs a real canvas; the reader's own behaviour is covered by its helpers.
vi.mock('./PdfReader', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./PdfReader')>();
  return {
    ...actual,
    PdfReader: ({ title, initialPage = 1, onQuote }: { title: string; initialPage?: number; onQuote: (action: 'note' | 'ask', quote: { text: string; page: number }) => void }) => {
      const [page] = useState(initialPage);
      return <div className="mock-pdf" data-title={title} data-initial-page={page}>
        <button type="button" onClick={() => onQuote('note', { text: 'Quasars are bright.', page: 2 })}>mock note</button>
        <button type="button" onClick={() => onQuote('ask', { text: 'Quasars are bright.', page: 2 })}>mock ask</button>
      </div>;
    },
  };
});

function respond(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
}
const noContent = () => new Response(null, { status: 204 });
const options = {
  total_pages: 1, embedded_text_pages: 1, embedded_text_ratio: 1,
  recommended_mode: 'embedded', ocr_available: true, ocr_engine: 'tesseract',
  quick_ocr_available: false,
};
const folder = (id: string, name: string) => ({ id, name, created_at: '2025-01-01T00:00:00Z', updated_at: '2025-01-01T00:00:00Z' });
const makePaper = (id: string, title: string, extra: Record<string, unknown> = {}) => ({
  id, title, status: 'ready', usefulness: 'unrated', source_filename: `${id}.pdf`, page_count: 3, content_type: 'application/pdf',
  metadata: {} as Record<string, unknown>, artifacts: [], chunks: [], created_at: '2026-01-01T00:00:00Z', ...extra,
});

let container: HTMLDivElement;
let root: Root;
const oldUrl = window.location.href;

beforeEach(() => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  localStorage.clear();
  vi.stubGlobal('scrollTo', vi.fn());
  container = document.createElement('div');
  root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount());
  vi.useRealTimers();
  window.history.replaceState({}, '', oldUrl);
  vi.unstubAllGlobals();
});

const mount = async (url: string) => {
  window.history.replaceState({}, '', url);
  await act(async () => root.render(<RouterProvider><PapersPage /></RouterProvider>));
  await act(async () => { await Promise.resolve(); });
};
const button = (text: string) => [...container.querySelectorAll<HTMLButtonElement>('button')].find((item) => item.textContent?.trim() === text)!;
const change = async (label: string, value: string) => {
  const field = container.querySelector<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>(`[aria-label="${label}"]`)!;
  await act(async () => {
    const prototype = field instanceof HTMLSelectElement ? HTMLSelectElement.prototype
      : field instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, 'value')!.set!.call(field, value);
    field.dispatchEvent(new Event(field instanceof HTMLSelectElement ? 'change' : 'input', { bubbles: true }));
  });
};
const click = async (element: Element | null | undefined) => {
  await act(async () => { (element as HTMLElement).click(); });
  await act(async () => { await Promise.resolve(); });
};

function LibraryRoutes() {
  const { pathname } = useLocation();
  return <>
    <Link to="/library">Go to papers</Link>
    <Link to="/chat">Go to chat</Link>
    {pathname === '/chat' ? <div>Chat page</div> : <PapersPage />}
  </>;
}

const ocrJob = (status: OcrBatch['status'], itemStatus: OcrBatch['items'][number]['status']): OcrBatch => ({
  id: 'remote-batch', status, created_at: '2026-10-22T10:00:00Z', updated_at: '2026-10-22T10:01:00Z',
  finished_at: null, parallel_requests: 4,
  items: [{ document_id: 'remote', title: 'Remote paper', status: itemStatus, completed_pages: 0, total_pages: 3, phase_label: null, error: null }],
});

it('runs explicit quick CPU extraction without starting or reconfiguring Surya batches', async () => {
  vi.stubGlobal('EventSource', class {
    addEventListener() {}
    close() {}
  });
  let paper = makePaper('quick', 'Quick reading', { status: 'uploaded', chunks: [] });
  const writes: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost');
    if (init?.method === 'POST') {
      writes.push(url.pathname + url.search);
      if (url.pathname === '/api/documents/quick/ingest' && url.searchParams.get('mode') === 'quick') {
        paper = { ...paper, status: 'ready', metadata: { ocr_indra_pages: [1] } };
        return respond(paper);
      }
      throw new Error(`Unexpected write: ${url}`);
    }
    if (url.pathname.endsWith('/documents')) return respond([paper]);
    if (url.pathname.endsWith('/ingestion-options')) return respond({
      ...options, ocr_engine: 'surya', ocr_available: false, quick_ocr_available: true,
    });
    if (url.pathname.endsWith('/settings')) return respond({ ocr_engine: 'surya', default_model_references: {} });
    if (url.pathname.endsWith('/paper-folders') || url.pathname.endsWith('/summaries')
      || url.pathname.endsWith('/summary-runs') || url.pathname.endsWith('/ocr-batches')
      || url.pathname.endsWith('/workspace/search') || url.pathname.endsWith('/providers')) return respond([]);
    if (url.pathname.endsWith('/documents/quick')) return respond(paper);
    throw new Error(`Unexpected request: ${url}`);
  }));
  await mount('/library?paper=quick');
  await click(container.querySelector('[aria-label="Papers settings"]'));
  expect(button('Quick CPU OCR').disabled).toBe(false);
  await click(button('Quick CPU OCR'));
  expect(writes).toEqual(['/api/documents/quick/ingest?mode=quick']);
  expect(container.textContent).toContain('Indra CPU extraction on pages 1');
  expect(container.textContent).toContain('check equations, table values and reading order');
});

it('restores batch history without fetching every paper detail or starting OCR after navigation', async () => {
  const history = ocrJob('completed_with_errors', 'completed');
  history.items.push(
    { ...history.items[0], document_id: 'failed', status: 'failed', error: 'Old OCR failure' },
    { ...history.items[0], document_id: 'stopped', status: 'stopped' },
  );
  const requests: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL(String(input), 'http://localhost').pathname;
    expect(init?.method).not.toBe('POST');
    requests.push(path);
    if (path === '/api/documents/ocr-batches') return respond([history]);
    if (path === '/api/documents') return respond([makePaper('remote', 'Remote paper')]);
    if (path === '/api/paper-folders') return respond([]);
    throw new Error(`Unexpected request: ${path}`);
  }));
  await mount('/library');
  expect(requests.filter((path) => path === '/api/documents')).toHaveLength(1);
  expect(requests.filter((path) => /^\/api\/documents\/(remote|failed|stopped)$/.test(path))).toEqual([]);
  await act(async () => root.render(<div />));
  await mount('/library');
  expect(requests.filter((path) => path === '/api/documents')).toHaveLength(2);
  expect(requests.filter((path) => /^\/api\/documents\/(remote|failed|stopped)$/.test(path))).toEqual([]);
});

it('refreshes remote OCR start and completion through the list, retaining SSE without per-page refreshes', async () => {
  vi.useFakeTimers();
  let job = ocrJob('pending', 'pending');
  let stored = makePaper('remote', 'Remote paper');
  const requests: string[] = [];
  const sources: { url: string; close: ReturnType<typeof vi.fn> }[] = [];
  vi.stubGlobal('EventSource', class {
    close = vi.fn();
    constructor(url: string) { sources.push({ url, close: this.close }); }
    addEventListener() {}
  });
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL(String(input), 'http://localhost').pathname;
    expect(init?.method).not.toBe('POST');
    requests.push(path);
    if (path === '/api/documents/ocr-batches') return respond([job]);
    if (path === '/api/documents') return respond([stored]);
    if (path === '/api/paper-folders') return respond([]);
    throw new Error(`Unexpected request: ${path}`);
  }));
  await mount('/library');
  const initialLists = requests.filter((path) => path === '/api/documents').length;
  stored = { ...stored, status: 'processing' };
  job = ocrJob('running', 'running');
  await act(async () => vi.advanceTimersByTimeAsync(OCR_POLL_INTERVAL));
  expect(container.querySelector('.status-processing')).not.toBeNull();
  expect(sources.map((source) => source.url)).toEqual(['/api/documents/remote/ingest/events']);
  expect(requests.filter((path) => path === '/api/documents')).toHaveLength(initialLists + 1);
  job = { ...job, items: [{ ...job.items[0], completed_pages: 2 }] };
  await act(async () => vi.advanceTimersByTimeAsync(OCR_POLL_INTERVAL));
  expect(requests.filter((path) => path === '/api/documents')).toHaveLength(initialLists + 1);
  stored = { ...stored, title: 'OCR complete title', status: 'ready', metadata: { extraction_mode: 'ocr', ocr_pages: [1, 2, 3] } };
  job = ocrJob('completed', 'completed');
  await act(async () => vi.advanceTimersByTimeAsync(OCR_POLL_INTERVAL));
  expect(container.textContent).toContain('OCR complete title');
  expect(container.querySelector('.status-processing')).toBeNull();
  expect(sources[0].close).toHaveBeenCalled();
  expect(requests.filter((path) => path === '/api/documents')).toHaveLength(initialLists + 2);
  expect(requests.filter((path) => path === '/api/documents/remote')).toEqual([]);
});

it.each([false, true])('shows and retries batch-list errors outside the OCR panel (reader=%s)', async (reader) => {
  let failing = true;
  const paper = makePaper('remote', 'Remote paper');
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const path = new URL(String(input), 'http://localhost').pathname;
    if (path === '/api/documents/ocr-batches') return failing
      ? Response.json({ detail: 'OCR progress unavailable' }, { status: 503 }) : respond([]);
    if (path === '/api/documents') return respond([paper]);
    if (path === '/api/paper-folders') return respond([]);
    if (path === '/api/documents/remote') return respond(paper);
    if (path.endsWith('/ingestion-options')) return respond(options);
    throw new Error(`Unexpected request: ${path}`);
  }));
  await mount(reader ? '/library?paper=remote' : '/library');
  expect(container.textContent).toContain('OCR progress unavailable');
  expect(container.textContent).not.toContain('in this selection');
  failing = false;
  await click(button('Retry OCR progress refresh'));
  expect(container.textContent).not.toContain('OCR progress unavailable');
});

it('surfaces and retries a paper-list refresh failure on a remote OCR status transition', async () => {
  vi.useFakeTimers();
  let job = ocrJob('pending', 'pending');
  let failing = false;
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const path = new URL(String(input), 'http://localhost').pathname;
    if (path === '/api/documents/ocr-batches') return respond([job]);
    if (path === '/api/documents') return failing
      ? Response.json({ detail: 'Paper list refresh failed' }, { status: 503 }) : respond([makePaper('remote', 'Remote paper')]);
    if (path === '/api/paper-folders') return respond([]);
    throw new Error(`Unexpected request: ${path}`);
  }));
  await mount('/library');
  failing = true;
  job = ocrJob('running', 'running');
  await act(async () => vi.advanceTimersByTimeAsync(OCR_POLL_INTERVAL));
  expect(container.textContent).toContain('Paper list refresh failed');
  failing = false;
  await click(button('Retry OCR progress refresh'));
  expect(container.textContent).not.toContain('Paper list refresh failed');
  expect(container.textContent).toContain('Remote paper');
});

it('aborts batch-triggered detail refreshes when changing readers and ignores their late failures', async () => {
  vi.useFakeTimers();
  let job = ocrJob('pending', 'pending');
  const papers = [makePaper('remote', 'Remote paper'), makePaper('other', 'Other paper')];
  let finish!: (response: Response) => void;
  let signal: AbortSignal | undefined;
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL(String(input), 'http://localhost').pathname;
    if (path === '/api/documents/ocr-batches') return respond([job]);
    if (path === '/api/documents') return respond(papers);
    if (path === '/api/paper-folders') return respond([]);
    if (path.endsWith('/ingestion-options')) return respond(options);
    if (path === '/api/documents/remote' && job.status === 'running') {
      signal = init?.signal as AbortSignal;
      return new Promise<Response>((resolve) => { finish = resolve; });
    }
    const paper = papers.find((entry) => path.endsWith(`/${entry.id}`));
    if (paper) return respond(paper);
    throw new Error(`Unexpected request: ${path}`);
  }));
  await mount('/library?paper=remote');
  job = ocrJob('running', 'running');
  await act(async () => vi.advanceTimersByTimeAsync(OCR_POLL_INTERVAL));
  await act(async () => {
    window.history.pushState({}, '', '/library?paper=other');
    window.dispatchEvent(new PopStateEvent('popstate'));
  });
  expect(signal?.aborted).toBe(true);
  expect(container.querySelector('.reader-title')?.textContent).toContain('Other paper');
  await act(async () => finish(Response.json({ detail: 'Old selected detail failed' }, { status: 503 })));
  expect(container.querySelector('.reader-title')?.textContent).toContain('Other paper');
  expect(container.textContent).not.toContain('Old selected detail failed');
});

it('shows download progress and elapsed time until the prepared paper opens, even with the form closed', async () => {
  const paper = makePaper('downloaded', 'Downloaded paper');
  let finish!: (response: Response) => void;
  const downloading = new Promise<Response>((resolve) => { finish = resolve; });
  const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL(String(input), 'http://localhost').pathname;
    if (path === '/api/documents/download') {
      expect(JSON.parse(String(init?.body))).toEqual({ url: 'https://example.org/paper.pdf', title: 'Downloaded paper' });
      return downloading;
    }
    if (path === '/api/documents') return respond([paper]);
    if (path === '/api/paper-folders' || path.includes('/workspace/search')) return respond([]);
    if (path.endsWith('/ingestion-options')) return respond(options);
    if (path === '/api/documents/downloaded') return respond(paper);
    throw new Error(`Unexpected request: ${path}`);
  });
  vi.stubGlobal('fetch', fetch);
  await mount('/library?add=1');
  vi.useFakeTimers();
  expect(container.querySelector('[role="progressbar"][aria-label="Paper download and preparation"]')).toBeNull();
  await change('PDF URL', 'https://example.org/paper.pdf');
  await change('Paper title', 'Downloaded paper');
  await act(async () => {
    button('Download PDF').form!.dispatchEvent(new SubmitEvent('submit', { bubbles: true, cancelable: true }));
  });
  expect(fetch.mock.calls.some(([input]) => String(input).endsWith('/documents/download'))).toBe(true);

  const progress = () => container.querySelector('[role="progressbar"][aria-label="Paper download and preparation"]');
  expect(progress()).not.toBeNull();
  expect(progress()?.hasAttribute('aria-valuenow')).toBe(false);
  expect(container.querySelector('[role="timer"]')?.textContent).toBe('0s elapsed');
  expect(button('Downloading…').disabled).toBe(true);
  expect(container.querySelector<HTMLInputElement>('[aria-label="PDF URL"]')?.disabled).toBe(true);
  await click(container.querySelector('[aria-label="Close add paper"]'));
  expect(container.querySelector('[aria-label="PDF URL"]')).toBeNull();
  await act(async () => vi.advanceTimersByTime(61_000));
  expect(progress()).not.toBeNull();
  expect(container.querySelector('[role="timer"]')?.textContent).toBe('1m 1s elapsed');
  expect(container.textContent).toContain('Time remaining is unavailable');

  await act(async () => finish(respond(paper)));
  expect(window.location.search).toBe('?paper=downloaded');
  expect(container.querySelector('.reader-title')?.textContent).toContain('Downloaded paper');
  expect(progress()).toBeNull();
  expect(vi.getTimerCount()).toBe(0);
});

it('keeps download timing when browsing another paper, clears failures, and starts retries from zero', async () => {
  const paper = makePaper('existing', 'Existing paper');
  let finish!: (response: Response) => void;
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const path = new URL(String(input), 'http://localhost').pathname;
    if (path === '/api/documents/download') return new Promise<Response>((resolve) => { finish = resolve; });
    if (path === '/api/documents') return respond([paper]);
    if (path === '/api/paper-folders' || path.includes('/workspace/search')) return respond([]);
    if (path.endsWith('/ingestion-options')) return respond(options);
    if (path === '/api/documents/existing') return respond(paper);
    throw new Error(`Unexpected request: ${path}`);
  }));
  await mount('/library?add=1');
  vi.useFakeTimers();
  await change('PDF URL', 'https://example.org/paper.pdf');
  await act(async () => {
    button('Download PDF').form!.dispatchEvent(new SubmitEvent('submit', { bubbles: true, cancelable: true }));
  });
  await act(async () => vi.advanceTimersByTime(3_000));
  await click(container.querySelector('.paper-row-main'));
  expect(container.querySelector('.reader-title')?.textContent).toContain('Existing paper');
  expect(container.querySelector('[role="progressbar"][aria-label="Paper download and preparation"]')).not.toBeNull();
  expect(container.querySelector('[role="timer"]')?.textContent).toBe('3s elapsed');

  await act(async () => finish(Response.json({ detail: 'Remote download failed' }, { status: 400 })));
  expect(container.textContent).toContain('Remote download failed');
  expect(container.querySelector('[role="progressbar"][aria-label="Paper download and preparation"]')).toBeNull();
  expect(vi.getTimerCount()).toBe(0);

  await click(container.querySelector('[aria-label="Back to papers"]'));
  expect(container.querySelector<HTMLInputElement>('[aria-label="PDF URL"]')?.value).toBe('https://example.org/paper.pdf');
  expect(button('Download PDF').disabled).toBe(false);
  await act(async () => {
    button('Download PDF').form!.dispatchEvent(new SubmitEvent('submit', { bubbles: true, cancelable: true }));
  });
  expect(container.textContent).not.toContain('Remote download failed');
  expect(container.querySelector('[role="timer"]')?.textContent).toBe('0s elapsed');
  await act(async () => root.render(<div />));
  expect(vi.getTimerCount()).toBe(0);
  await act(async () => finish(Response.json({ detail: 'Remote download failed' }, { status: 400 })));
  await mount('/library');
  expect(container.textContent).toContain('Remote download failed');
});

it('restores an active download after leaving Papers and does not redirect another page on completion', async () => {
  const paper = makePaper('downloaded', 'Downloaded paper');
  let finish!: (response: Response) => void;
  const downloading = new Promise<Response>((resolve) => { finish = resolve; });
  const fetch = vi.fn(async (input: RequestInfo | URL) => {
    const path = new URL(String(input), 'http://localhost').pathname;
    if (path === '/api/documents/download') return downloading;
    if (path === '/api/documents') return respond([paper]);
    if (path === '/api/paper-folders' || path.includes('/workspace/search')) return respond([]);
    if (path.endsWith('/ingestion-options')) return respond(options);
    if (path === '/api/documents/downloaded') return respond(paper);
    throw new Error(`Unexpected request: ${path}`);
  });
  vi.stubGlobal('fetch', fetch);
  window.history.replaceState({}, '', '/library?add=1');
  await act(async () => root.render(<StrictMode><RouterProvider><LibraryRoutes /></RouterProvider></StrictMode>));
  vi.useFakeTimers();
  await change('PDF URL', 'https://example.org/paper.pdf');
  await act(async () => {
    button('Download PDF').form!.dispatchEvent(new SubmitEvent('submit', { bubbles: true, cancelable: true }));
  });

  await click(container.querySelector('a[href="/chat"]'));
  expect(container.textContent).toContain('Chat page');
  expect(vi.getTimerCount()).toBe(0);
  await act(async () => vi.advanceTimersByTime(61_000));
  await click(container.querySelector('a[href="/library"]'));
  expect(container.querySelector('[aria-label="Paper download and preparation"]')).not.toBeNull();
  expect(container.querySelector('[role="timer"]')?.textContent).toBe('1m 1s elapsed');
  await click(button('Add paper'));
  expect(container.querySelector<HTMLInputElement>('[aria-label="PDF URL"]')?.value).toBe('https://example.org/paper.pdf');
  expect(button('Downloading…').disabled).toBe(true);
  expect(fetch.mock.calls.filter(([input]) => String(input).endsWith('/documents/download'))).toHaveLength(1);

  await click(container.querySelector('a[href="/chat"]'));
  await act(async () => finish(respond(paper)));
  expect(window.location.pathname).toBe('/chat');
  expect(container.textContent).toContain('Chat page');
  const historyLength = window.history.length;
  await click(container.querySelector('a[href="/library"]'));
  expect(window.location.search).toBe('?paper=downloaded');
  expect(window.history.length).toBe(historyLength + 2);
  expect(container.querySelector('.reader-title')?.textContent).toContain('Downloaded paper');
  expect(container.querySelector('[aria-label="Paper download and preparation"]')).toBeNull();
  expect(vi.getTimerCount()).toBe(0);
});

it('reports a download failure that happened on another page and allows a fresh retry', async () => {
  const paper = makePaper('downloaded', 'Downloaded paper');
  let finish!: (response: Response) => void;
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const path = new URL(String(input), 'http://localhost').pathname;
    if (path === '/api/documents/download') return new Promise<Response>((resolve) => { finish = resolve; });
    if (path === '/api/documents') return respond([]);
    if (path === '/api/paper-folders' || path.includes('/workspace/search')) return respond([]);
    if (path.endsWith('/ingestion-options')) return respond(options);
    if (path === '/api/documents/downloaded') return respond(paper);
    throw new Error(`Unexpected request: ${path}`);
  }));
  window.history.replaceState({}, '', '/library?add=1');
  await act(async () => root.render(<RouterProvider><LibraryRoutes /></RouterProvider>));
  await change('PDF URL', 'https://example.org/paper.pdf');
  await change('Paper title', 'Downloaded paper');
  await act(async () => {
    button('Download PDF').form!.dispatchEvent(new SubmitEvent('submit', { bubbles: true, cancelable: true }));
  });
  await click(container.querySelector('a[href="/chat"]'));
  await act(async () => finish(Response.json({ detail: 'Download could not finish' }, { status: 400 })));
  expect(window.location.pathname).toBe('/chat');
  await click(container.querySelector('a[href="/library"]'));
  expect(container.textContent).toContain('Download could not finish');
  expect(container.querySelector('[aria-label="Paper download and preparation"]')).toBeNull();
  await click(button('Add paper'));
  expect(container.querySelector<HTMLInputElement>('[aria-label="PDF URL"]')?.value).toBe('https://example.org/paper.pdf');
  expect(container.querySelector<HTMLInputElement>('[aria-label="Paper title"]')?.value).toBe('Downloaded paper');
  expect(button('Download PDF').disabled).toBe(false);
  await act(async () => {
    button('Download PDF').form!.dispatchEvent(new SubmitEvent('submit', { bubbles: true, cancelable: true }));
  });
  expect(container.textContent).not.toContain('Download could not finish');
  expect(container.querySelector('[role="timer"]')?.textContent).toBe('0s elapsed');
  await act(async () => finish(respond(paper)));
  expect(container.querySelector('.reader-title')?.textContent).toContain('Downloaded paper');
});

it('browses bounded sorted pages and searches title/content by relevance within folder and status filters', async () => {
  const papers = Array.from({ length: 60 }, (_, index) => makePaper(`paper-${index}`, `Paper ${String(index).padStart(2, '0')}`, {
    source_filename: `${index}.pdf`, metadata: index % 2 ? { folder_id: 'methods' } : {},
    created_at: new Date(Date.UTC(2026, 0, 1, 0, index)).toISOString(),
  }));
  const searches: URL[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), 'http://localhost');
    if (url.pathname.endsWith('/documents/search')) {
      searches.push(url);
      return respond([
        { ...papers[1], score: 2, excerpt: 'Quasar measurements in the body text.', citation: 'p.3', page_start: 3 },
        { ...papers[3], score: 1, excerpt: 'Quasar discovery', citation: null, page_start: null },
      ]);
    }
    if (url.pathname.endsWith('/documents')) return respond(papers);
    if (url.pathname.endsWith('/paper-folders')) return respond([folder('methods', 'Methods')]);
    if (url.pathname.includes('/summary-runs') || url.pathname.endsWith('/workspace/search') || url.pathname.endsWith('/summaries') || url.pathname.endsWith('/providers')) return respond([]);
    if (url.pathname.endsWith('/settings')) return respond({ default_model_references: {} });
    if (url.pathname.endsWith('/ingestion-options')) return respond(options);
    const paper = papers.find((item) => url.pathname.endsWith(`/documents/${item.id}`));
    if (paper) return respond(paper);
    throw new Error(`Unexpected request: ${url}`);
  }));
  await mount('/library');

  expect(container.querySelector('.rail, .papers-layout')).toBeNull();
  expect(container.querySelectorAll('.paper-row-main')).toHaveLength(25);
  expect(container.querySelector('.paper-row-main strong')?.textContent).toBe('Paper 59');
  await click(button('Next'));
  expect(container.textContent).toContain('Page 2 of 3');
  await change('Sort papers', 'title');
  expect(container.textContent).toContain('Page 1 of 3');
  expect(container.querySelector('.paper-row-main strong')?.textContent).toBe('Paper 00');

  await click([...container.querySelectorAll('.folder-chip')].find((item) => item.textContent?.includes('Methods')));
  expect(container.querySelector('[aria-label="Paper folders"] [aria-current="true"]')?.textContent).toContain('Methods');
  await change('Paper status', 'ready');
  await change('Filter by usefulness', 'maybe_useful');
  expect(container.querySelector('.library-filter-row [role="status"]')?.textContent).toContain('Ready to read');
  await change('Search papers', 'quasar');
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 250)); });
  expect(searches).toHaveLength(1);
  expect(searches[0].searchParams.get('query')).toBe('quasar');
  expect(searches[0].searchParams.get('folder_id')).toBe('methods');
  expect(searches[0].searchParams.get('paper_status')).toBe('ready');
  expect(searches[0].searchParams.get('usefulness')).toBe('maybe_useful');
  expect(searches[0].searchParams.get('limit')).toBe('26');
  expect(searches[0].searchParams.get('offset')).toBe('0');
  expect(container.querySelectorAll('.paper-row-main')).toHaveLength(2);
  expect(container.textContent).toContain('p.3: Quasar measurements in the body text.');
  expect(container.textContent).toContain('Title match: Quasar discovery');
  expect(container.querySelector<HTMLSelectElement>('[aria-label="Sort papers"]')!.disabled).toBe(true);

  await click(button('Clear'));
  expect(container.querySelector('.library-filter-row')?.textContent).not.toContain('Usefulness:');
  expect(container.querySelector<HTMLInputElement>('[aria-label="Search papers"]')!.value).toBe('');
  expect(container.querySelector('[aria-label="Paper folders"] [aria-current="true"]')?.textContent).toContain('Methods');

  // Opening a paper gives the reader the whole page; Back returns to the library.
  await click(container.querySelector('.paper-row-main'));
  expect(window.location.search).toBe('?paper=paper-1');
  expect(container.querySelector('.reader-page .reader-title')?.textContent).toContain('Paper 01');
  expect(container.querySelector('.paper-table')).toBeNull();
  await click(container.querySelector('[aria-label="Next paper"]'));
  expect(container.querySelector('.reader-title')?.textContent).toContain('Paper 03');
  await click(container.querySelector('[aria-label="Previous paper"]'));
  expect(container.querySelector('.reader-title')?.textContent).toContain('Paper 01');
  await click(container.querySelector('a[aria-label="Back to papers"]'));
  expect(window.location.pathname + window.location.search).toBe('/library');
  expect(container.querySelector('.paper-table')).not.toBeNull();
});

it('shows abstracts below titles and expands them without navigating or selecting a paper', async () => {
  const abstract = 'This study compares calibration methods under domain shift. '.repeat(12) + 'Final conclusion.';
  const shortAbstract = `${abstract.slice(0, 279)}.`;
  const boundaryAbstract = `${abstract.slice(0, 280)}.`;
  const papers = [
    makePaper('abstract', 'Paper with abstract', { abstract }),
    makePaper('missing', 'Paper without abstract', { abstract: null }),
    makePaper('short', 'Short abstract', { abstract: shortAbstract }),
    makePaper('boundary', 'Just over preview limit', { abstract: boundaryAbstract }),
  ];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const path = new URL(String(input), 'http://localhost').pathname;
    if (path === '/api/documents') return respond(papers);
    if (path === '/api/paper-folders' || path === '/api/documents/summary-runs') return respond([]);
    throw new Error(`Unexpected request: ${path}`);
  }));
  await mount('/library');
  const row = container.querySelector('.paper-row-content')!;
  expect(row.querySelector('.paper-row-main')?.textContent).toBe('Paper with abstract');
  expect(row.querySelector('.paper-abstract')?.textContent).toBe(`Abstract: ${abstract.slice(0, 280).trimEnd()}…`);
  expect(row.querySelector('.paper-abstract')?.textContent).not.toContain('Final conclusion.');
  expect(container.textContent).toContain('No abstract available.');
  expect(container.querySelector('#paper-abstract-short')?.textContent).toBe(`Abstract: ${shortAbstract}`);
  expect(container.querySelector('[aria-label="Read full abstract for Short abstract"]')).toBeNull();
  const boundaryToggle = container.querySelector<HTMLButtonElement>('[aria-label="Read full abstract for Just over preview limit"]')!;
  await click(boundaryToggle);
  expect(container.querySelector('#paper-abstract-boundary')?.textContent).toBe(`Abstract: ${boundaryAbstract}`);
  const toggle = row.querySelector<HTMLButtonElement>('.paper-abstract-toggle')!;
  await click(toggle);
  expect(toggle.getAttribute('aria-expanded')).toBe('true');
  expect(row.querySelector('.paper-abstract')?.textContent).toBe(`Abstract: ${abstract}`);
  expect(window.location.pathname + window.location.search).toBe('/library');
  expect(container.querySelector<HTMLInputElement>('[aria-label="Select Paper with abstract"]')?.checked).toBe(false);
  await click(toggle);
  expect(toggle.getAttribute('aria-expanded')).toBe('false');
  expect(row.querySelector('.paper-abstract')?.textContent).not.toContain('Final conclusion.');
});

it('hides the folder column by default and remembers opting in without changing membership', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const path = new URL(String(input), 'http://localhost').pathname;
    if (path === '/api/documents') return respond([makePaper('filed', 'Filed paper', { metadata: { folder_id: 'methods' } })]);
    if (path === '/api/paper-folders') return respond([folder('methods', 'Methods')]);
    if (path === '/api/documents/summary-runs') return respond([]);
    throw new Error(`Unexpected request: ${path}`);
  }));
  await mount('/library');
  expect(container.querySelector('.paper-table-folder')).toBeNull();
  expect(container.querySelector('.paper-table-head')?.textContent).not.toContain('Folder');
  expect(container.querySelector('[aria-label="Paper folders"]')?.textContent).toContain('Methods');
  const toggle = [...container.querySelectorAll('label')].find((label) => label.textContent?.includes('Show folder column'))?.querySelector('input');
  await click(toggle);
  expect(container.querySelector('.paper-table-folder')?.textContent).toBe('Methods');
  expect(container.querySelector('.paper-table-head')?.textContent).toContain('Folder');
  await act(async () => root.unmount());
  root = createRoot(container);
  await mount('/library');
  expect(container.querySelector('.paper-table-folder')?.textContent).toBe('Methods');
  await click([...container.querySelectorAll('label')].find((label) => label.textContent?.includes('Show folder column'))?.querySelector('input'));
  expect(container.querySelector('.paper-table-folder')).toBeNull();
});

it('saves usefulness from the list and reader without changing reading progress', async () => {
  let paper = makePaper('rated', 'Rated paper');
  const saved: string[] = [];
  const reading = JSON.stringify({ rated: { page: 2, pages: 3, at: 100 } });
  localStorage.setItem('sw.reading', reading);
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith('/usefulness')) {
      const payload = JSON.parse(String(init?.body));
      expect(init?.method).toBe('PUT');
      expect(Object.keys(payload)).toEqual(['usefulness']);
      saved.push(payload.usefulness);
      paper = { ...paper, ...payload, metadata: { ...paper.metadata, ...payload } };
      return respond(paper);
    }
    if (url.endsWith('/api/documents')) return respond([paper]);
    if (url.endsWith('/paper-folders') || url.includes('/workspace/search')) return respond([]);
    if (url.endsWith('/ingestion-options')) return respond(options);
    if (url.endsWith('/documents/rated')) return respond(paper);
    throw new Error(`Unexpected request: ${url}`);
  }));
  await mount('/library');
  const field = () => container.querySelector<HTMLSelectElement>('[aria-label="Usefulness of Rated paper"]')!;
  expect(field().value).toBe('unrated');
  expect([...field().options].map((option) => option.textContent)).toEqual(['Unrated', 'Useful', 'Maybe useful', 'Not useful']);
  const progress = container.querySelector('.paper-table-read')?.outerHTML;
  await change('Usefulness of Rated paper', 'useful');
  expect(field().value).toBe('useful');
  expect(container.querySelector('.paper-table-read')?.outerHTML).toBe(progress);
  expect(window.location.search).toBe('');
  await click(container.querySelector('.paper-row-main'));
  expect(field().closest('.reader-bar')).not.toBeNull();
  expect(field().value).toBe('useful');
  await change('Usefulness of Rated paper', 'maybe_useful');
  await click(container.querySelector('[aria-label="Back to papers"]'));
  expect(field().value).toBe('maybe_useful');
  await change('Usefulness of Rated paper', 'not_useful');
  await change('Usefulness of Rated paper', 'unrated');
  expect(field().value).toBe('unrated');
  expect(saved).toEqual(['useful', 'maybe_useful', 'not_useful', 'unrated']);
  expect(localStorage.getItem('sw.reading')).toBe(reading);
  expect(container.querySelectorAll('.paper-row-main')).toHaveLength(1);
});

it.each([false, true])('filters usefulness and recovers pagination after a rating changes (search=%s)', async (withSearch) => {
  let papers = Array.from({ length: 30 }, (_, index) => makePaper(`rated-${index}`, `Rated ${index}`, {
    usefulness: index < 26 ? 'useful' : index === 26 ? 'maybe_useful' : index === 27 ? 'not_useful' : undefined,
    created_at: new Date(Date.UTC(2026, 0, 1, 0, index)).toISOString(),
  }));
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.includes('/documents/search?')) {
      const params = new URL(url, 'http://localhost').searchParams;
      const offset = Number(params.get('offset'));
      const limit = Number(params.get('limit'));
      return respond([...papers].reverse().filter((paper) =>
        !params.get('usefulness') || paper.usefulness === params.get('usefulness'),
      ).slice(offset, offset + limit));
    }
    if (url.endsWith('/usefulness')) {
      const id = url.split('/').at(-2);
      const payload = JSON.parse(String(init?.body));
      papers = papers.map((paper) => paper.id === id ? { ...paper, ...payload } : paper);
      return respond(papers.find((paper) => paper.id === id));
    }
    if (url.endsWith('/api/documents')) return respond(papers);
    if (url.endsWith('/paper-folders')) return respond([]);
    throw new Error(`Unexpected request: ${url}`);
  }));
  await mount('/library');
  for (const [value, count] of [['unrated', 2], ['maybe_useful', 1], ['not_useful', 1], ['useful', 25]] as const) {
    await change('Filter by usefulness', value);
    expect(container.querySelectorAll('.paper-row-main')).toHaveLength(count);
  }
  expect(container.querySelector('.library-filter-row')?.textContent).toContain('Usefulness: Useful');
  if (withSearch) {
    await change('Search papers', 'rated');
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 250)); });
  }
  await click(button('Next'));
  if (withSearch) await act(async () => { await new Promise((resolve) => setTimeout(resolve, 250)); });
  expect(container.textContent).toContain(withSearch ? 'Page 2' : 'Page 2 of 2');
  expect(container.querySelectorAll('.paper-row-main')).toHaveLength(1);
  await change('Usefulness of Rated 0', 'not_useful');
  if (withSearch) {
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 250)); });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 250)); });
  }
  expect(container.querySelectorAll('.paper-row-main')).toHaveLength(25);
  expect(container.textContent).not.toContain('Page 2');
  expect(container.querySelector('[aria-label="Usefulness of Rated 0"]')).toBeNull();
  await click(button('Clear'));
  expect(container.querySelector('.library-filter-row')?.textContent).not.toContain('Usefulness:');
  expect(container.textContent).toContain('Page 1 of 2');
});

it('filters topic shelves and shows an empty state when no usefulness labels match', async () => {
  localStorage.setItem('scholarweave.papersLayout', 'topics');
  const papers = Array.from({ length: 8 }, (_, index) => makePaper(`topic-${index}`, `Topic paper ${index}`, {
    usefulness: index ? 'useful' : 'not_useful',
  }));
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith('/api/documents')) return respond(papers);
    if (url.endsWith('/paper-folders') || url.endsWith('/providers')) return respond([]);
    if (url.endsWith('/paper-topics')) return respond({
      topics: [{ id: 'topic', name: 'Observations', description: 'Observational studies', paper_ids: papers.map((paper) => paper.id) }],
      naming_model: { model: 'test' }, created_at: '2026-01-01T00:00:00Z',
    });
    throw new Error(`Unexpected request: ${url}`);
  }));
  await mount('/library');
  expect(container.querySelector('[aria-label="Observations, 8 papers"]')).not.toBeNull();
  await change('Filter by usefulness', 'useful');
  await click(container.querySelector('[aria-label="Observations, 7 papers"]'));
  expect(container.querySelectorAll('.paper-row-main')).toHaveLength(7);
  expect(container.querySelector('[aria-label="Usefulness of Topic paper 0"]')).toBeNull();
  await change('Filter by usefulness', 'maybe_useful');
  expect(container.querySelector('.paper-table')?.hasAttribute('hidden')).toBe(false);
  expect(container.textContent).toContain('No matching papers.');
  await click(button('Clear'));
  expect(container.querySelector('[aria-label="Observations, 8 papers"]')).not.toBeNull();
});

it('keeps the saved rating and surfaces failures, then allows a retry', async () => {
  const paper = makePaper('rated', 'Rated paper', { usefulness: 'useful' });
  let fail = true;
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith('/usefulness')) {
      return fail ? Response.json({ message: 'Could not save usefulness' }, { status: 500 })
        : respond({ ...paper, ...JSON.parse(String(init?.body)) });
    }
    if (url.endsWith('/api/documents')) return respond([paper]);
    if (url.endsWith('/paper-folders')) return respond([]);
    throw new Error(`Unexpected request: ${url}`);
  }));
  await mount('/library');
  await change('Usefulness of Rated paper', 'not_useful');
  const field = () => container.querySelector<HTMLSelectElement>('[aria-label="Usefulness of Rated paper"]')!;
  expect(field().value).toBe('useful');
  expect(field().disabled).toBe(false);
  expect(container.textContent).toContain('Could not save usefulness');
  fail = false;
  await change('Usefulness of Rated paper', 'not_useful');
  expect(field().value).toBe('not_useful');
  expect(container.textContent).not.toContain('Could not save usefulness');
});

it('does not replace a different open paper when a usefulness save finishes late', async () => {
  const papers = [makePaper('a', 'First paper'), makePaper('b', 'Second paper')];
  let finish!: (response: Response) => void;
  const saving = new Promise<Response>((resolve) => { finish = resolve; });
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith('/usefulness')) return saving;
    if (url.endsWith('/api/documents')) return respond(papers);
    if (url.endsWith('/paper-folders') || url.includes('/workspace/search')) return respond([]);
    if (url.endsWith('/ingestion-options')) return respond(options);
    const paper = papers.find((item) => url.endsWith(`/documents/${item.id}`));
    if (paper) return respond(paper);
    throw new Error(`Unexpected request: ${url}`);
  }));
  await mount('/library?paper=a');
  await change('Usefulness of First paper', 'useful');
  expect(container.querySelector<HTMLSelectElement>('[aria-label="Usefulness of First paper"]')!.disabled).toBe(true);
  await click(container.querySelector('[aria-label="Next paper"]'));
  await act(async () => finish(respond({ ...papers[0], usefulness: 'useful' })));
  expect(container.querySelector('.reader-title')?.textContent).toContain('Second paper');
  expect(container.querySelector<HTMLSelectElement>('[aria-label="Usefulness of Second paper"]')!.value).toBe('unrated');
});

it('keeps library controls directly accessible above the paper results', async () => {
  const paper = makePaper('p1', 'Quasar survey');
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), 'http://localhost');
    if (url.pathname.endsWith('/documents')) return respond([paper]);
    if (url.pathname.endsWith('/paper-folders')) return respond([]);
    throw new Error(`Unexpected request: ${url}`);
  }));
  await mount('/library');

  const controls = container.querySelector('[aria-label="Paper controls"]')!;
  expect(container.querySelector('h1')).toBeNull();
  expect(container.querySelector('[aria-label="Papers settings"]')).toBeNull();
  expect(button('Add paper')).toBeTruthy();
  expect(container.querySelector('[aria-label="Search papers"]')).not.toBeNull();
  expect(container.querySelector('[aria-label="Select Quasar survey"]')).not.toBeNull();

  expect(controls.querySelector('[aria-label="Paper status"]')).not.toBeNull();
  expect(controls.querySelector('[aria-label="Sort papers"]')).not.toBeNull();
  expect(controls.contains(button('Summarize…'))).toBe(true);

  await change('Paper status', 'ready');
  expect(container.querySelector('.library-filter-row [role="status"]')?.textContent).toContain('Ready to read');
  await click(button('Clear'));
  expect(container.querySelector<HTMLSelectElement>('[aria-label="Paper status"]')!.value).toBe('');
  expect(button('Add paper')).toBeTruthy();
  expect(container.querySelector('[aria-label="Search papers"]')).not.toBeNull();
  expect(container.querySelector('[aria-label="Selected papers"]')).toBeNull();
});

it('keeps the latest paper when an earlier detail request finishes late', async () => {
  const papers = ['First', 'Slow', 'Latest'].map((title, index) => makePaper(`race-${index}`, title, {
    status: 'uploaded', created_at: `2026-01-0${3 - index}T00:00:00Z`,
  }));
  let resolveSlow!: (response: Response) => void;
  const slow = new Promise<Response>((resolve) => { resolveSlow = resolve; });
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith('/api/documents')) return respond(papers);
    if (url.includes('/workspace/search') || url.endsWith('/paper-folders')) return respond([]);
    if (url.endsWith('/ingestion-options')) return respond(options);
    if (url.endsWith('/documents/race-1')) return slow;
    const paper = papers.find((item) => url.endsWith(`/documents/${item.id}`));
    if (paper) return respond(paper);
    throw new Error(`Unexpected request: ${url}`);
  }));
  await mount('/?paper=race-1');
  await act(async () => { window.history.pushState({}, '', '/?paper=race-2'); window.dispatchEvent(new PopStateEvent('popstate')); });
  await act(async () => { await Promise.resolve(); });
  expect(container.querySelector('.reader-title')?.textContent).toContain('Latest');
  await act(async () => resolveSlow(respond(papers[1])));
  expect(container.querySelector('.reader-title')?.textContent).toContain('Latest');
  expect(container.textContent).not.toContain('Slow');
});

it.each([false, true])('does not replace the reader after another paper finishes ingestion (failure=%s)', async (failure) => {
  const papers = [makePaper('a', 'Preparing A'), makePaper('b', 'Reading B')];
  let finish!: (response: Response) => void;
  const ingestion = new Promise<Response>((resolve) => { finish = resolve; });
  vi.stubGlobal('EventSource', class {
    addEventListener() {}
    close() {}
  });
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/ingest?')) return ingestion;
    if (url.endsWith('/api/documents')) return respond(papers);
    if (url.includes('/summary-runs') || url.includes('/workspace/search') || url.endsWith('/paper-folders') || url.endsWith('/summaries') || url.endsWith('/providers')) return respond([]);
    if (url.endsWith('/settings')) return respond({ default_model_references: {} });
    if (url.endsWith('/ingestion-options')) return respond(options);
    const paper = papers.find((item) => url.endsWith(`/documents/${item.id}`));
    if (paper) return respond(paper);
    throw new Error(`Unexpected request: ${url}`);
  }));
  await mount('/library?paper=a');
  await click(container.querySelector('[aria-label="Papers settings"]'));
  await click(button('Details'));
  await click(button('Re-index embedded text'));
  await click(container.querySelector('[aria-label="Next paper"]'));
  expect(container.querySelector('.reader-title')?.textContent).toContain('Reading B');
  await act(async () => finish(failure
    ? Response.json({ message: 'A extraction failed' }, { status: 500 })
    : respond({ ...papers[0], status: 'ready', page_count: 8 })));
  expect(window.location.search).toBe('?paper=b');
  expect(container.querySelector('.reader-title')?.textContent).toContain('Reading B');
  expect(container.textContent).not.toContain('Opening paper…');
  expect(container.textContent).not.toContain('A extraction failed');
});

it('keeps previous/next in the visible relevance order instead of the whole folder', async () => {
  const papers = ['Alphabetic first', 'Not a match', 'Relevant first'].map((title, index) => makePaper(`p${index}`, title));
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/documents/search?')) return respond([papers[2], papers[0]]);
    if (url.endsWith('/api/documents')) return respond(papers);
    if (url.includes('/workspace/search') || url.endsWith('/paper-folders')) return respond([]);
    if (url.endsWith('/ingestion-options')) return respond(options);
    const paper = papers.find((item) => url.endsWith(`/documents/${item.id}`));
    if (paper) return respond(paper);
    throw new Error(`Unexpected request: ${url}`);
  }));
  await mount('/library');
  await change('Search papers', 'relevance');
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 250)); });
  await click(container.querySelector('.paper-row-main'));
  expect(container.querySelector('.reader-title')?.textContent).toContain('Relevant first');
  expect(container.querySelector<HTMLButtonElement>('[aria-label="Previous paper"]')!.disabled).toBe(true);
  await click(container.querySelector('[aria-label="Next paper"]'));
  expect(container.querySelector('.reader-title')?.textContent).toContain('Alphabetic first');
  expect(container.querySelector<HTMLButtonElement>('[aria-label="Next paper"]')!.disabled).toBe(true);
  await click(container.querySelector('[aria-label="Previous paper"]'));
  expect(container.querySelector('.reader-title')?.textContent).toContain('Relevant first');
});

it('exposes extracted text and fallback-page warnings even when no source PDF is stored', async () => {
  const paper = makePaper('text-only', 'Text-only paper', {
    metadata: { ocr_pages: [1, 3], ocr_fallback_pages: [2] },
    artifacts: [
      { id: 'text', kind: 'extracted_markdown', media_type: 'text/markdown' },
      { id: 'figure', kind: 'extracted_image', media_type: 'image/png', sha256: 'figure-hash',
        metadata: { markdown_path: 'figures/page-0001-003.png' } },
    ],
  });
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith('/api/documents')) return respond([paper]);
    if (url.includes('/workspace/search') || url.endsWith('/paper-folders')) return respond([]);
    if (url.endsWith('/ingestion-options')) return respond(options);
    if (url.endsWith('/documents/text-only')) return respond(paper);
    if (url.endsWith('/artifacts/text/content')) return respond({ content: '## Findings\n\nExtracted source findings.\n\nEmbedded text from page 2.\n\n![Source figure](figures/page-0001-003.png)' });
    throw new Error(`Unexpected request: ${url}`);
  }));
  await mount('/library?paper=text-only');
  expect(container.querySelector('[aria-label="Paper view"]')).not.toBeNull();
  expect(button('PDF')).toBeUndefined();
  expect(button('Text').disabled).toBe(false);
  await click(button('Text'));
  expect(container.querySelector('[aria-label="Retrieved paper text"]')?.textContent).toContain('Extracted source findings.');
  expect(container.querySelector('[aria-label="Retrieved paper text"]')?.textContent).toContain('Embedded text from page 2.');
  expect(container.querySelector('[aria-label="Retrieved paper text"] h2')?.textContent).toBe('Findings');
  expect(container.querySelector('[alt="Source figure"]')?.getAttribute('src')).toBe('/api/artifacts/figure/raw?sha256=figure-hash');
  expect(container.textContent).not.toContain('No PDF stored');
  await click(container.querySelector('[aria-label="Papers settings"]'));
  await click(button('Details'));
  expect(container.querySelector('.library-details')?.textContent).toContain('OCR could not recover pages 2');
  expect(container.querySelector('.library-details')?.textContent).toContain('Embedded text was used where available');
});

it('opens the exact paper linked from the brief and keeps preparation tools in Details', async () => {
  const papers = [
    makePaper('newest-paper', 'Newest paper', { created_at: '2026-01-02T00:00:00Z' }),
    makePaper('kept-paper', 'Kept paper', { status: 'uploaded' }),
  ];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith('/api/documents')) return respond(papers);
    if (url.includes('/summary-runs') || url.includes('/workspace/search') || url.endsWith('/paper-folders') || url.endsWith('/providers') || url.endsWith('/summaries')) return respond([]);
    if (url.endsWith('/settings')) return respond({ default_model_references: {} });
    if (url.endsWith('/ingestion-options')) return respond(options);
    const paper = papers.find((item) => url.endsWith(`/documents/${item.id}`));
    if (paper) return respond(paper);
    throw new Error(`Unexpected request: ${url}`);
  }));
  await mount('/library?paper=kept-paper');
  expect(container.querySelector('.reader-title')?.textContent).toContain('Kept paper');
  expect(container.querySelector('.library-details')).toBeNull();

  await click(container.querySelector('[aria-label="Papers settings"]'));
  await click(button('Details'));
  const extraction = container.querySelector<HTMLDetailsElement>('.library-details')!;
  expect(extraction.querySelector('summary')?.textContent).toBe('Extraction and indexing');
  expect(extraction.open).toBe(true);
  expect(extraction.textContent).toContain('Use embedded text');
  expect(container.querySelector('[aria-label="Delete paper Kept paper"]')).not.toBeNull();
  expect(container.querySelector('[aria-label="Delete paper Newest paper"]')).toBeNull();

  await click(container.querySelector('[aria-label="Previous paper"]'));
  expect(container.querySelector('.reader-title')?.textContent).toContain('Newest paper');
  expect(container.querySelector<HTMLDetailsElement>('.library-details')!.open).toBe(false);
  expect(container.textContent).toContain('Re-index embedded text');
  await click(container.querySelector('[aria-label="Papers settings"]'));
  await click(button('Summarize paper'));
  expect(container.querySelector('[aria-label="Paper summary"]')?.textContent).toContain('Quick summary in chat');
  expect(container.querySelector('[aria-label="Paper summary"]')?.textContent).toContain('Create summary');

  // Without the shell, Ask falls back to an editable draft in the full chat.
  await click(button('Ask a question'));
  expect(window.location.pathname).toBe('/chat');
  const params = new URLSearchParams(window.location.search);
  expect(params.get('research')).toBeNull();
  expect(JSON.parse(params.get('reading') ?? '{}')).toMatchObject({ document_id: 'newest-paper', title: 'Newest paper' });
});

it('opens explicit citation pages and remounts the same paper for a new target while retaining remembered defaults', async () => {
  const paper = makePaper('cited', 'Cited paper', {
    page_count: 12, artifacts: [{ id: 'pdf', kind: 'source_pdf', media_type: 'application/pdf', sha256: 'abc' }],
  });
  localStorage.setItem('sw.reading', JSON.stringify({ cited: { page: 5, pages: 12, at: 1 } }));
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith('/api/documents')) return respond([paper]);
    if (url.endsWith('/documents/cited')) return respond(paper);
    if (url.endsWith('/ingestion-options')) return respond(options);
    if (url.includes('/workspace/search') || url.endsWith('/paper-folders')) return respond([]);
    throw new Error(`Unexpected request: ${url}`);
  }));
  await mount('/library?document=cited&page=2');
  const page = () => container.querySelector('.mock-pdf')?.getAttribute('data-initial-page');
  expect(page()).toBe('2');
  const navigate = async (query: string) => {
    await act(async () => {
      window.history.pushState({}, '', `/library?document=cited${query}`);
      window.dispatchEvent(new PopStateEvent('popstate'));
    });
  };
  await navigate('&page=7');
  expect(page()).toBe('7');
  await navigate('');
  expect(page()).toBe('5');
  for (const invalid of ['0', '-1', '1.5', 'abc', '9007199254740992']) {
    await navigate(`&page=${invalid}`);
    expect(page()).toBe('5');
  }
  await navigate('&page=1');
  expect(page()).toBe('1');
});

it('omits References from the paper list and reader', async () => {
  const paper = makePaper('p1', 'Quasar survey');
  const requests: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), 'http://localhost');
    requests.push(url.pathname);
    if (url.pathname.endsWith('/documents')) return respond([paper]);
    if (url.pathname.endsWith('/paper-folders') || url.pathname.endsWith('/workspace/search')) return respond([]);
    if (url.pathname.endsWith('/ingestion-options')) return respond(options);
    if (url.pathname.endsWith('/documents/p1')) return respond(paper);
    throw new Error(`Unexpected request: ${url}`);
  }));
  await mount('/library');

  const hasReferencesButton = () => [...container.querySelectorAll('button')].some((item) => item.textContent?.trim() === 'References');
  expect(hasReferencesButton()).toBe(false);
  expect(container.querySelector('[aria-label="Reference management"]')).toBeNull();
  await click(container.querySelector('.paper-row-main'));

  expect(container.querySelector('.reader-title')?.textContent).toContain('Quasar survey');
  expect(hasReferencesButton()).toBe(false);
  expect(container.querySelector('[aria-label="Reference management"]')).toBeNull();
  expect(requests.some((path) => path.endsWith('/references'))).toBe(false);
});

it.each([false, true])('collects a selected quote, explicitly ensuring missing notes only when needed: %s', async (missing) => {
  const paper = makePaper('paper-1', 'Bright objects', {
    artifacts: [{ id: 'pdf-1', kind: 'source_pdf', media_type: 'application/pdf', sha256: 'abc' }],
  });
  const notesPath = 'library/papers/bright--paper-1/notes.md';
  let content = '# Notes: Bright objects\n';
  let exists = !missing;
  const creates: RequestInit[] = [];
  const patches: Record<string, unknown>[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost');
    if (url.pathname.endsWith('/documents')) return respond([paper]);
    if (url.pathname.endsWith('/paper-folders')) return respond([]);
    if (url.pathname.endsWith('/ingestion-options')) return respond(options);
    if (url.pathname.endsWith('/documents/paper-1/notes')) {
      expect(init?.method).toBe('POST');
      creates.push(init!);
      exists = true;
      return respond({ path: notesPath, content, sha256: `sha-${content.length}` });
    }
    if (url.pathname.endsWith('/workspace/search')) {
      expect(url.searchParams.get('tags')).toBe('paper:paper-1');
      expect(url.searchParams.get('kinds')).toBe('paper_notes');
      return respond(exists ? [{ path: notesPath, kind: 'paper_notes', name: 'notes.md', tags: [], media_type: 'text/markdown', modified_at: '', size_bytes: 1 }] : []);
    }
    if (url.pathname.endsWith('/workspace/files/content')) {
      if (init?.method === 'PATCH') {
        const body = JSON.parse(String(init.body)) as Record<string, string>;
        patches.push(body);
        content += body.content;
      }
      return respond({ path: notesPath, content, sha256: `sha-${content.length}`, kind: 'paper_notes', name: 'notes.md', tags: [], media_type: 'text/markdown', modified_at: '', size_bytes: content.length });
    }
    if (url.pathname.endsWith('/documents/paper-1')) return respond(paper);
    throw new Error(`Unexpected request: ${url}`);
  }));
  await mount('/?paper=paper-1');
  expect(container.querySelector('.mock-pdf')?.getAttribute('data-title')).toBe('Bright objects');
  expect(creates).toEqual([]);

  await click(button('mock note'));
  await act(async () => { await Promise.resolve(); });
  expect(patches).toEqual([{
    path: notesPath, operation: 'append', selection: null, expected_sha256: `sha-${'# Notes: Bright objects\n'.length}`,
    content: expect.stringMatching(/^\n<!-- scholarweave-highlight:[0-9a-f-]+ -->\n> Quasars are bright\.\n>\n> — p\. 2\n$/),
  }]);
  expect(creates).toHaveLength(missing ? 1 : 0);
  if (missing) expect(creates[0].body).toBeUndefined();
  expect(container.querySelector('.toast')?.textContent).toContain('Added to notes');

  await click(button('Show notes'));
  expect(container.querySelector('[aria-label="Paper notes"]')?.textContent).toContain('Quasars are bright.');

  await click(button('mock ask'));
  expect(window.location.pathname).toBe('/chat');
  const params = new URLSearchParams(window.location.search);
  expect(params.get('research')).toBe('> Quasars are bright.\n\n');
  expect(JSON.parse(params.get('reading') ?? '{}')).toMatchObject({ document_id: expect.any(String), page: 2 });
});

function mockNotesReader() {
  const papers = [
    makePaper('p1', 'First paper', { artifacts: [{ id: 'pdf-1', kind: 'source_pdf', media_type: 'application/pdf', sha256: 'one' }] }),
    makePaper('p2', 'Second paper', { artifacts: [{ id: 'pdf-2', kind: 'source_pdf', media_type: 'application/pdf', sha256: 'two' }] }),
  ];
  const files = new Map(papers.map((paper) => [paper.id, {
    path: `library/papers/${paper.id}/notes.md`, content: `Notes for ${paper.title}\n`, sha256: `${paper.id}-initial`,
  }]));
  const writes: { method: string; body: Record<string, string> }[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost');
    if (url.pathname.endsWith('/documents')) return respond(papers);
    if (url.pathname.endsWith('/paper-folders') || url.pathname.endsWith('/summary-runs') || url.pathname.endsWith('/summaries') || url.pathname.endsWith('/providers')) return respond([]);
    if (url.pathname.endsWith('/settings')) return respond({ default_model_references: {} });
    if (url.pathname.endsWith('/ingestion-options')) return respond(options);
    if (url.pathname.endsWith('/workspace/search')) {
      const id = url.searchParams.get('tags')!.slice('paper:'.length);
      return respond([files.get(id)]);
    }
    if (url.pathname.endsWith('/workspace/files/content')) {
      const body = init?.body ? JSON.parse(String(init.body)) as Record<string, string> : null;
      const file = [...files.values()].find((item) => item.path === (body?.path ?? url.searchParams.get('path')))!;
      if (body) {
        writes.push({ method: init!.method!, body });
        if (body.expected_sha256 !== file.sha256) return Response.json({ message: 'Notes changed; reload before saving.' }, { status: 409 });
        file.content = init?.method === 'PATCH' ? file.content + body.content : body.content;
        file.sha256 += '-updated';
      }
      return respond(file);
    }
    const paper = papers.find((item) => url.pathname.endsWith(`/documents/${item.id}`));
    if (paper) return respond(paper);
    throw new Error(`Unexpected request: ${url}`);
  }));
  return { files, writes };
}

it('retains drafts across closing and swapping panels, and guards navigation before switching papers', async () => {
  const { files, writes } = mockNotesReader();
  const confirm = vi.fn(() => false);
  vi.stubGlobal('confirm', confirm);
  await mount('/library?paper=p1');
  await click(button('Notes'));
  await click(button('Edit'));
  await change('Edit notes', 'First paper unsaved draft');
  await click(container.querySelector('[aria-label="Close notes"]'));
  expect(container.querySelector('[aria-label="Paper notes"]')).toBeNull();
  await click(button('Notes'));
  expect(container.querySelector<HTMLTextAreaElement>('textarea[aria-label="Edit notes"]')?.value).toBe('First paper unsaved draft');

  await click(container.querySelector('[aria-label="Papers settings"]'));
  await click(button('Details'));
  expect(container.querySelector('[aria-label="Paper details"]')).not.toBeNull();
  await click(button('Notes'));
  expect(container.querySelector<HTMLTextAreaElement>('textarea[aria-label="Edit notes"]')?.value).toBe('First paper unsaved draft');
  const unload = new Event('beforeunload', { cancelable: true });
  window.dispatchEvent(unload);
  expect(unload.defaultPrevented).toBe(true);
  await click(container.querySelector('[aria-label="Next paper"]'));
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(window.location.search).toBe('?paper=p1');
  expect(container.querySelector<HTMLTextAreaElement>('textarea[aria-label="Edit notes"]')?.value).toBe('First paper unsaved draft');
  await click(container.querySelector('[aria-label="Back to papers"]'));
  expect(window.location.search).toBe('?paper=p1');
  expect(writes).toEqual([]);

  confirm.mockReturnValue(true);
  await click(container.querySelector('[aria-label="Next paper"]'));
  expect(window.location.search).toBe('?paper=p2');
  expect(container.querySelector('textarea[aria-label="Edit notes"]')).toBeNull();
  await click(button('Edit'));
  expect(container.querySelector<HTMLTextAreaElement>('textarea[aria-label="Edit notes"]')?.value).toBe('Notes for Second paper\n');
  await change('Edit notes', 'Second paper saved notes');
  await click(button('Save'));
  expect(writes).toEqual([{ method: 'PUT', body: {
    path: files.get('p2')!.path, content: 'Second paper saved notes', expected_sha256: 'p2-initial',
  } }]);
  expect(files.get('p1')!.content).toBe('Notes for First paper\n');
  const cleanUnload = new Event('beforeunload', { cancelable: true });
  window.dispatchEvent(cleanUnload);
  expect(cleanUnload.defaultPrevented).toBe(false);
});

it('preserves the original draft revision and the appended quote when a save conflicts', async () => {
  const { files, writes } = mockNotesReader();
  const confirm = vi.fn(() => false);
  vi.stubGlobal('confirm', confirm);
  await mount('/library?paper=p1');
  await click(button('Notes'));
  await click(button('Edit'));
  await change('Edit notes', 'My manual draft');
  await click(button('mock note'));
  expect(files.get('p1')!.content).toContain('Quasars are bright.');
  expect(container.querySelector<HTMLTextAreaElement>('textarea[aria-label="Edit notes"]')?.value).toBe('My manual draft');
  await click(button('Save'));
  expect(writes[1]).toEqual({ method: 'PUT', body: {
    path: files.get('p1')!.path, content: 'My manual draft', expected_sha256: 'p1-initial',
  } });
  expect(container.querySelector('[aria-label="Paper notes"]')?.textContent).toContain('Notes changed');
  expect(container.querySelector<HTMLTextAreaElement>('textarea[aria-label="Edit notes"]')?.value).toBe('My manual draft');
  expect(files.get('p1')!.content).toContain('Quasars are bright.');
  await click(container.querySelector('[aria-label="Close notes"]'));
  await click(container.querySelector('[aria-label="Next paper"]'));
  expect(window.location.search).toBe('?paper=p1');
  await click(button('Notes'));
  expect(container.querySelector<HTMLTextAreaElement>('textarea[aria-label="Edit notes"]')?.value).toBe('My manual draft');
  await click(button('Cancel'));
  expect(container.querySelector('textarea[aria-label="Edit notes"]')).not.toBeNull();
  confirm.mockReturnValue(true);
  await click(button('Cancel'));
  expect(container.querySelector('textarea[aria-label="Edit notes"]')).toBeNull();
  expect(container.querySelector('[aria-label="Paper notes"]')?.textContent).toContain('Quasars are bright.');
});

it('creates folders, moves selected papers in bulk and deletes folders without deleting papers', async () => {
  const papers = [makePaper('p1', 'Alpha'), makePaper('p2', 'Beta'), makePaper('p3', 'Gamma')];
  let folders = [folder('folder-methods', 'Methods')];
  const requests: Array<{ url: string; init?: RequestInit }> = [];
  vi.stubGlobal('confirm', vi.fn(() => true));
  vi.stubGlobal('prompt', vi.fn(() => 'Reading group'));
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    requests.push({ url, init });
    if (url.endsWith('/api/paper-folders') && init?.method === 'POST') {
      const created = folder(`folder-${folders.length}`, JSON.parse(String(init.body)).name as string);
      folders = [...folders, created];
      return respond(created);
    }
    if (url.endsWith('/api/paper-folders')) return respond(folders);
    if (url.endsWith('/api/paper-folders/folder-methods') && init?.method === 'DELETE') {
      folders = folders.filter((item) => item.id !== 'folder-methods');
      for (const paper of papers) if (paper.metadata.folder_id === 'folder-methods') paper.metadata = {};
      return noContent();
    }
    const move = url.match(/\/api\/documents\/(p\d)\/folder$/);
    if (move && init?.method === 'PUT') {
      const paper = papers.find((item) => item.id === move[1])!;
      const target = JSON.parse(String(init.body)).folder_id as string | null;
      paper.metadata = target ? { folder_id: target } : {};
      return respond(paper);
    }
    const remove = url.match(/\/api\/documents\/(p\d)$/);
    if (remove && init?.method === 'DELETE') {
      papers.splice(papers.findIndex((item) => item.id === remove[1]), 1);
      return noContent();
    }
    if (url.endsWith('/api/documents')) return respond(papers);
    throw new Error(`Unexpected request: ${url}`);
  }));
  await mount('/');

  // A folder created inline becomes the current folder.
  await change('New paper folder', 'Theory');
  await act(async () => {
    container.querySelector('.paper-folder-form')!.dispatchEvent(new SubmitEvent('submit', { bubbles: true, cancelable: true }));
  });
  await act(async () => { await Promise.resolve(); });
  expect(container.querySelector('[aria-label="Paper folders"] [aria-current="true"]')?.textContent).toContain('Theory');
  await click([...container.querySelectorAll('.folder-chip')].find((item) => item.textContent?.includes('All papers')));

  // Select two papers and move them into a brand-new folder in one step.
  await click(container.querySelector('[aria-label="Select Alpha"]'));
  await click(container.querySelector('[aria-label="Select Gamma"]'));
  expect(container.querySelector('[aria-label="Selected papers"]')?.textContent).toContain('2 selected');
  await change('Move selected papers', '__new__');
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
  const moves = requests.filter(({ url, init }) => init?.method === 'PUT' && url.endsWith('/folder'));
  expect(moves.map(({ url }) => url.match(/(p\d)\/folder/)![1])).toEqual(['p1', 'p3']);
  expect(moves.every(({ init }) => JSON.parse(String(init!.body)).folder_id === 'folder-2')).toBe(true);
  expect(container.querySelector('[aria-label="Selected papers"]')).toBeNull();
  expect(container.querySelector('.toast')?.textContent).toContain('Moved 2 papers to Reading group');
  expect([...container.querySelectorAll('.folder-chip')].find((item) => item.textContent?.includes('Reading group'))?.textContent).toContain('2');

  // Deleting a folder keeps its papers.
  papers[1].metadata = { folder_id: 'folder-methods' };
  await click(container.querySelector('[aria-label="Delete folder Methods"]'));
  await act(async () => { await Promise.resolve(); });
  expect(container.querySelector('[aria-label="Delete folder Methods"]')).toBeNull();
  expect(container.textContent).toContain('Beta');

  // Bulk delete asks once and removes only the selected papers.
  await click([...container.querySelectorAll('.folder-chip')].find((item) => item.textContent?.includes('All papers')));
  await click(container.querySelector('[aria-label="Select Beta"]'));
  await click(button('Delete'));
  await act(async () => { await Promise.resolve(); });
  expect(window.confirm).toHaveBeenLastCalledWith('Delete 1 paper and all of their saved files? This cannot be undone.');
  expect(container.querySelector('[aria-label="Select Beta"]')).toBeNull();
  expect(container.querySelector('[aria-label="Select Alpha"]')).not.toBeNull();
});

it('shows saved reading progress in paper rows and continue-reading cards without inventing unknown totals', async () => {
  localStorage.setItem('sw.reading', JSON.stringify({
    p1: { page: 4, pages: 10, at: 3 },
    p2: { page: 14, pages: 10, at: 2 },
    p3: { page: 4, pages: 0, at: 1 },
  }));
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith('/api/documents')) return respond([
      makePaper('p1', 'Alpha'), makePaper('p2', 'Beta'), makePaper('p3', 'Unknown total'), makePaper('p4', 'Unread'),
    ]);
    if (url.endsWith('/api/paper-folders')) return respond([]);
    throw new Error(`Unexpected request: ${url}`);
  }));
  await mount('/');
  const card = container.querySelector('[aria-label="Continue reading"] .continue-card');
  expect(card?.textContent).toContain('Alpha');
  expect(card?.textContent).toContain('Page 4 of 10');
  for (const [title, value, count] of [['Alpha', '40', 2], ['Beta', '100', 2], ['Unread', '0', 1]] as const) {
    const bars = container.querySelectorAll(`[role="progressbar"][aria-label="Reading progress for ${title}"]`);
    expect(bars).toHaveLength(count);
    for (const bar of bars) {
      expect(bar.getAttribute('aria-valuenow')).toBe(value);
      expect(bar.querySelector<HTMLElement>('.paper-reading-track > span')?.style.width).toBe(`${value}%`);
      expect(bar.textContent).toContain(`${value}%`);
    }
  }
  const unknown = container.querySelectorAll('[role="progressbar"][aria-label="Reading progress for Unknown total"]');
  expect(unknown).toHaveLength(2);
  for (const bar of unknown) {
    expect(bar.hasAttribute('aria-valuenow')).toBe(false);
    expect(bar.getAttribute('aria-valuetext')).toBe('Page 4');
  }
});

it('lists nested collections, filters direct members and preserves children when parent deletion is rejected', async () => {
  const papers = [
    makePaper('parent-paper', 'Parent member', { metadata: { folder_id: 'parent' } }),
    makePaper('child-paper', 'Child member', { metadata: { folder_id: 'child' } }),
  ];
  let folders = [folder('parent', 'Methods'), folder('child', 'Methods/Study')];
  vi.stubGlobal('confirm', vi.fn(() => true));
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (init?.method === 'DELETE' && url.endsWith('/paper-folders/parent')) {
      return new Response(JSON.stringify({ message: 'Delete child collections first.' }), { status: 409 });
    }
    if (init?.method === 'DELETE' && url.endsWith('/paper-folders/child')) {
      folders = folders.filter((item) => item.id !== 'child');
      papers[1].metadata = {};
      return noContent();
    }
    if (url.endsWith('/api/documents')) return respond(papers);
    if (url.endsWith('/api/paper-folders')) return respond(folders);
    throw new Error(`Unexpected request: ${url}`);
  }));
  await mount('/');
  const hierarchy = container.querySelector('[aria-label="Collection hierarchy"]')!;
  expect([...hierarchy.querySelectorAll('.folder-chip')].map((item) => item.getAttribute('aria-label'))).toEqual(['Methods', 'Methods/Study']);
  expect(hierarchy.querySelector('[aria-label="Methods/Study"]')?.parentElement?.style.marginInlineStart).toBe('1.1rem');
  await click(container.querySelector('[aria-label="Methods"]'));
  expect(container.querySelector('[aria-label="Select Parent member"]')).not.toBeNull();
  expect(container.querySelector('[aria-label="Select Child member"]')).toBeNull();
  await click(container.querySelector('[aria-label="Methods/Study"]'));
  expect(container.querySelector('[aria-label="Select Parent member"]')).toBeNull();
  expect(container.querySelector('[aria-label="Select Child member"]')).not.toBeNull();
  await click(container.querySelector('[aria-label="Delete folder Methods"]'));
  expect(container.textContent).toContain('Delete child collections first.');
  expect(container.querySelector('[aria-label="Methods/Study"]')).not.toBeNull();
  await click(container.querySelector('[aria-label="Delete folder Methods/Study"]'));
  expect(container.querySelector('[aria-label="Methods/Study"]')).toBeNull();
  await click([...container.querySelectorAll('.folder-chip')].find((item) => item.textContent?.includes('Unfiled')));
  expect(container.querySelector('[aria-label="Select Child member"]')).not.toBeNull();
});

describe('PdfReader quotes', () => {
  it('joins hyphenated line breaks and collapses layout whitespace', () => {
    expect(cleanQuote('  The measure-\n  ment of   bright\nquasars ')).toBe('The measurement of bright quasars');
  });
});

describe('LibraryTabs', () => {
  it('makes both library destinations visible and identifies the current view', () => {
    const element = document.createElement('div');
    element.innerHTML = renderToStaticMarkup(<RouterProvider><LibraryTabs active="notes" /></RouterProvider>);
    expect(element.querySelector('nav')?.getAttribute('aria-label')).toBe('Library view');
    expect(element.querySelectorAll('a')).toHaveLength(2);
    expect(element.querySelector('[aria-current="page"]')?.textContent).toContain('Notes');
  });
});

describe('topics, summaries and reading questions', () => {
  it('groups the current folder into named topic shelves and keeps new papers visible', async () => {
    const papers = Array.from({ length: 10 }, (_, index) => makePaper(`p${index}`, `Paper ${index}`));
    const posts: unknown[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'http://localhost');
      if (url.pathname.endsWith('/paper-topics')) {
        if (init?.method === 'POST') {
          posts.push(JSON.parse(String(init.body)));
          return respond({
            folder_id: null, created_at: '2026-01-02T00:00:00Z', paper_ids: papers.slice(0, 9).map((paper) => paper.id),
            embedding_model: { provider_profile_id: 'llama', model: 'embeddinggemma-2' },
            naming_model: { provider_profile_id: 'llama', model: 'gemma4-26b-a4b' },
            topics: [
              { id: 't1', name: 'Quasar surveys', description: 'Sky surveys of quasars.', paper_ids: ['p0', 'p1', 'p2', 'p3', 'p4'] },
              { id: 't2', name: 'Galaxy dynamics', description: 'Rotation and mass.', paper_ids: ['p5', 'p6', 'p7', 'p8'] },
            ],
          });
        }
        return respond(null);
      }
      if (url.pathname.endsWith('/documents')) return respond(papers);
      if (url.pathname.endsWith('/paper-folders')) return respond([]);
      if (url.pathname.endsWith('/settings')) return respond({
        default_model_references: {
          chat: { provider_profile_id: 'llama', model: 'qwen-27b' },
          small_tasks: { provider_profile_id: 'llama', model: 'gemma4-26b-a4b' },
        },
      });
      if (url.pathname.endsWith('/providers')) {
        return respond([{ id: 'llama', name: 'llama', kind: 'openai_compatible', models: [
          { name: 'qwen-27b', capabilities: ['chat'], enabled: true },
          { name: 'gemma4-26b-a4b', capabilities: ['chat'], enabled: true },
        ] }]);
      }
      throw new Error(`Unexpected request: ${url}`);
    }));
    await mount('/library');

    await click(button('Topics'));
    expect(container.querySelector('[aria-label="Paper results"]')).toBeNull();
    expect(container.querySelector('[aria-label="Select all shown papers"]')).toBeNull();
    expect(localStorage.getItem('scholarweave.papersLayout')).toBe('topics');
    expect(container.textContent).toContain('Group these 10 papers into topics');
    await click(button('Group by topic'));
    await act(async () => { await Promise.resolve(); });

    expect(posts).toEqual([{ folder_id: null, naming_model: {} }]);
    expect(localStorage.getItem('scholarweave.topicNamer')).toBeNull();
    const cards = [...container.querySelectorAll('.topic-card')];
    expect(cards.map((card) => card.querySelector('strong')?.textContent)).toEqual(['Quasar surveys', 'Galaxy dynamics', 'Not yet grouped']);
    expect(cards[2].textContent).toContain('Paper 9');
    await click(cards[0]);
    expect(container.querySelector('.topic-detail')?.getAttribute('aria-label')).toBe('Quasar surveys');
    expect(container.querySelectorAll('.topic-detail .paper-row-main').length).toBeGreaterThan(0);
    await click(button('All topics'));
    expect(container.querySelectorAll('.topic-card')).toHaveLength(3);

    await click(button('List'));
    expect(container.querySelector('.topic-view')).toBeNull();
    expect(container.querySelector('[aria-label="Select all shown papers"]')).not.toBeNull();
    expect(container.querySelectorAll('.paper-row-main')).toHaveLength(10);
  });

  it('opens a saved summary in the reader and returns to the paper', async () => {
    const paper = makePaper('p1', 'Quasar survey');
    const version = {
      id: 'v1', created_at: '2026-01-03T00:00:00Z', citation_count: 2, status: 'complete',
      path: 'papers/p1/summary.md', prompt_revision: 'abc', review_summary: null,
    };
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), 'http://localhost');
      if (url.pathname.endsWith('/summaries/v1')) return respond({ version, content: '# Summary body' });
      if (url.pathname.includes('/summary-runs')) return respond([]);
      if (url.pathname.endsWith('/summaries')) return respond([version]);
      if (url.pathname.endsWith('/documents')) return respond([paper]);
      if (url.pathname.endsWith('/paper-folders') || url.pathname.endsWith('/providers') || url.pathname.endsWith('/workspace/search')) return respond([]);
      if (url.pathname.endsWith('/settings')) return respond({ default_model_references: {} });
      if (url.pathname.endsWith('/ingestion-options')) return respond(options);
      if (url.pathname.endsWith('/notes')) return respond({ content: '' });
      if (url.pathname.endsWith('/documents/p1')) {
        return respond({ ...paper, artifacts: [{ id: 'a1', kind: 'source_pdf', sha256: 'x', relative_path: 'p1.pdf' }] });
      }
      return respond(null);
    }));
    await mount('/library?paper=p1');
    await click(container.querySelector('[aria-label="Papers settings"]'));
    await click(button('Details'));
    await click([...container.querySelectorAll('.summary-version button')][0]);
    await act(async () => { await Promise.resolve(); });

    expect(container.querySelector('.reader-main [aria-label="Saved summary"]')?.textContent).toContain('Summary body');
    expect(container.querySelector('.mock-pdf')).toBeNull();
    await act(async () => { window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })); });
    expect(container.querySelector('[aria-label="Saved summary"]')).toBeNull();
    expect(container.querySelector('.mock-pdf')).not.toBeNull();
  });

  it('starts server-managed OCR for selected eligible papers and refreshes completed library metadata', async () => {
    const papers = [
      makePaper('chosen', 'Chosen native paper', { metadata: { extraction_mode: 'embedded', ocr_pages: [] } }),
      makePaper('unselected', 'Unselected native paper'),
      makePaper('already-ocr', 'Already OCR paper', { metadata: { extraction_mode: 'ocr', ocr_pages: [1, 2, 3] } }),
    ];
    vi.stubGlobal('EventSource', class { addEventListener() {} close() {} });
    const posts: string[] = [];
    let batches: unknown[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'http://localhost');
      if (url.pathname === '/api/documents') return respond(papers);
      if (url.pathname === '/api/documents/ocr-batches' && init?.method !== 'POST') return respond(batches);
      if (url.pathname === '/api/paper-folders') return respond([]);
      if (url.pathname === '/api/settings') return respond({ ocr_engine: 'surya', ocr_surya_model: 'configured-ocr' });
      if (url.pathname.endsWith('/ingestion-options')) return respond({ ...options, ocr_engine: 'surya' });
      if (init?.method === 'POST') {
        posts.push(`${url.pathname}${url.search}`);
        expect(JSON.parse(String(init.body))).toEqual({ document_ids: ['chosen'] });
        papers[0] = { ...papers[0], metadata: { extraction_mode: 'ocr', ocr_pages: [1, 2, 3] } };
        const completed = {
          id: 'ocr-batch', status: 'completed', parallel_requests: 1,
          created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:01:00Z', finished_at: '2026-01-01T00:01:00Z',
          items: [{ document_id: 'chosen', title: papers[0].title, status: 'completed', completed_pages: 3, total_pages: 3, error: null, phase_label: null }],
        };
        batches = [completed];
        return respond(completed);
      }
      const paper = papers.find((item) => url.pathname.endsWith(`/${item.id}`));
      if (paper) return respond(paper);
      throw new Error(`Unexpected request: ${url.pathname}`);
    }));
    await mount('/library');
    await click(container.querySelector('[aria-label="Select Chosen native paper"]'));
    await click(container.querySelector('[aria-label="Select Already OCR paper"]'));
    await click(button('OCR non-OCR papers'));
    expect(container.textContent).toContain('configured-ocr');
    await click(button('Start OCR for 1 selected papers'));
    expect(posts).toEqual(['/api/documents/ocr-batches']);
    expect(container.querySelector('a[href="/runs"]')).not.toBeNull();
    expect(container.textContent).toContain('0 papers with missing or incomplete OCR in this selection');
  });

  it('shows only the question and paper title for server-added reading context', () => {
    expect(splitReadingContext('How was redshift measured?\n\n<reading-context>\ntitle: Quasar survey\nI\'m reading...\n</reading-context>'))
      .toEqual({ body: 'How was redshift measured?', readingTitle: 'Quasar survey' });
    expect(splitReadingContext('Plain question')).toEqual({ body: 'Plain question', readingTitle: null });
  });
});
