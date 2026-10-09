// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { RouterProvider } from '../../app/router';
import { LibraryTabs } from '../../shared/components/Ui';
import PapersPage from './PapersPage';

function respond(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });
}

function noContent() {
  return new Response(null, { status: 204 });
}

it('browses bounded sorted pages and searches title/content by relevance within folder and status filters', async () => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  const oldUrl = window.location.href;
  window.history.replaceState({}, '', '/library');
  const papers = Array.from({ length: 60 }, (_, index) => ({
    id: `paper-${index}`, title: `Paper ${String(index).padStart(2, '0')}`, status: 'ready',
    source_filename: `${index}.pdf`, page_count: 3, content_type: 'application/pdf',
    metadata: index % 2 ? { folder_id: 'methods' } : {},
    artifacts: [], chunks: [],
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
    if (url.pathname.endsWith('/paper-folders')) return respond([{ id: 'methods', name: 'Methods' }]);
    if (url.pathname.endsWith('/providers') || url.pathname.endsWith('/summaries')) return respond([]);
    if (url.pathname.endsWith('/settings')) return respond({ default_model_references: {} });
    if (url.pathname.endsWith('/ingestion-options')) return respond({
      total_pages: 3, embedded_text_pages: 3, embedded_text_ratio: 1,
      recommended_mode: 'embedded', ocr_available: true, ocr_engine: 'tesseract',
    });
    const paper = papers.find((item) => url.pathname.endsWith(`/documents/${item.id}`));
    if (paper) return respond(paper);
    throw new Error(`Unexpected request: ${url}`);
  }));
  const container = document.createElement('div');
  const root = createRoot(container);
  const change = async (label: string, value: string) => {
    const field = container.querySelector<HTMLInputElement | HTMLSelectElement>(`[aria-label="${label}"]`)!;
    await act(async () => {
      const prototype = field instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(prototype, 'value')!.set!.call(field, value);
      field.dispatchEvent(new Event(field instanceof HTMLSelectElement ? 'change' : 'input', { bubbles: true }));
    });
  };
  try {
    await act(async () => root.render(<RouterProvider><PapersPage /></RouterProvider>));
    expect(container.querySelectorAll('.paper-row-main')).toHaveLength(25);
    expect(container.querySelector('.paper-row-main strong')?.textContent).toBe('Paper 59');
    const next = [...container.querySelectorAll('button')].find((item) => item.textContent === 'Next')!;
    await act(async () => next.click());
    expect(container.textContent).toContain('Page 2 of 3');
    await change('Sort papers', 'title');
    expect(container.textContent).toContain('Page 1 of 3');
    expect(container.querySelector('.paper-row-main strong')?.textContent).toBe('Paper 00');
    await act(async () => [...container.querySelectorAll<HTMLButtonElement>('.paper-folder-target')]
      .find((item) => item.textContent?.includes('Methods'))!.click());
    await change('Paper status', 'ready');
    await change('Search papers', 'quasar');
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 250)); });
    expect(searches).toHaveLength(1);
    expect(searches[0].searchParams.get('query')).toBe('quasar');
    expect(searches[0].searchParams.get('folder_id')).toBe('methods');
    expect(searches[0].searchParams.get('paper_status')).toBe('ready');
    expect(searches[0].searchParams.get('limit')).toBe('26');
    expect(searches[0].searchParams.get('offset')).toBe('0');
    expect(container.querySelectorAll('.paper-row-main')).toHaveLength(2);
    expect(container.querySelector('.paper-row-main strong')?.textContent).toBe('Paper 01');
    expect(container.textContent).toContain('p.3: Quasar measurements in the body text.');
    expect(container.textContent).toContain('Title match: Quasar discovery');
    expect(container.querySelector<HTMLSelectElement>('[aria-label="Sort papers"]')!.disabled).toBe(true);
    await act(async () => container.querySelectorAll<HTMLButtonElement>('.paper-row-main')[1].click());
    expect(container.querySelector('[aria-label="Delete paper Paper 03"]')).not.toBeNull();
  } finally {
    await act(async () => root.unmount());
    window.history.replaceState({}, '', oldUrl);
    vi.unstubAllGlobals();
  }
});

it('opens the exact kept paper linked from the brief instead of the newest library item', async () => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  const oldUrl = window.location.href;
  window.history.replaceState({}, '', '/library?paper=kept-paper');
  const papers = ['Newest paper', 'Kept paper'].map((title, index) => ({
    id: index ? 'kept-paper' : 'newest-paper', title, status: 'uploaded',
    source_filename: `${index}.pdf`, page_count: 1, metadata: {}, artifacts: [], chunks: [],
    created_at: `2026-01-0${2 - index}T00:00:00Z`,
  }));
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith('/api/documents')) return respond(papers);
    if (url.endsWith('/api/paper-folders') || url.endsWith('/api/providers') || url.endsWith('/summaries')) return respond([]);
    if (url.endsWith('/api/settings')) return respond({ default_model_references: {} });
    if (url.endsWith('/ingestion-options')) return respond({
      total_pages: 1, embedded_text_pages: 1, embedded_text_ratio: 1,
      recommended_mode: 'embedded', ocr_available: true, ocr_engine: 'tesseract',
    });
    const paper = papers.find((item) => url.endsWith(`/documents/${item.id}`));
    if (paper) return respond(paper);
    throw new Error(`Unexpected request: ${url}`);
  }));
  const container = document.createElement('div');
  const root = createRoot(container);
  try {
    await act(async () => root.render(<RouterProvider><PapersPage /></RouterProvider>));
    expect(container.querySelector('[aria-label="Delete paper Kept paper"]')).not.toBeNull();
    expect(container.querySelector('[aria-label="Delete paper Newest paper"]')).toBeNull();
  } finally {
    await act(async () => root.unmount());
    window.history.replaceState({}, '', oldUrl);
    vi.unstubAllGlobals();
  }
});

it('expands preparation for unready papers and isolates delayed summary results when switching papers', async () => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  const papers = ['ready', 'uploaded', 'failed'].map((status, index) => ({
    id: `paper-${index}`, title: `Paper ${index}`, status, source_filename: `${index}.pdf`,
    page_count: 1, metadata: {}, artifacts: [], chunks: [],
    created_at: `2026-01-0${3 - index}T00:00:00Z`,
  }));
  const version = {
    id: 'old-version', created_at: '2026-01-01T00:00:00Z', citation_count: 1,
    prompt_revision: 'prompt', status: 'ready', path: 'papers/paper-0/summary.md',
  };
  let resolveOldSummary!: (response: Response) => void;
  const oldSummary = new Promise<Response>((resolve) => { resolveOldSummary = resolve; });
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith('/api/documents')) return respond(papers);
    if (url.endsWith('/api/paper-folders') || url.endsWith('/api/providers')) return respond([]);
    if (url.endsWith('/api/settings')) return respond({ default_model_references: {} });
    if (url.endsWith('/summaries/old-version')) return oldSummary;
    if (url.endsWith('/summaries')) return respond(url.includes('/paper-0/') ? [version] : []);
    if (url.endsWith('/ingestion-options')) return respond({
      total_pages: 1, embedded_text_pages: 1, embedded_text_ratio: 1,
      recommended_mode: 'embedded', ocr_available: true, ocr_engine: 'tesseract',
    });
    const paper = papers.find((item) => url.endsWith(`/documents/${item.id}`));
    if (paper) return respond(paper);
    throw new Error(`Unexpected request: ${url}`);
  }));
  const container = document.createElement('div');
  const root = createRoot(container);
  const extraction = () => container.querySelector<HTMLDetailsElement>('.library-details')!;
  const selectPaper = async (index: number) => {
    await act(async () => container.querySelectorAll<HTMLButtonElement>('.paper-row-main')[index].click());
  };
  try {
    await act(async () => root.render(<RouterProvider><PapersPage /></RouterProvider>));
    expect(extraction().open).toBe(false);
    const openVersion = Array.from(container.querySelectorAll('button'))
      .find((button) => button.textContent?.includes('1 citations'))!;
    await act(async () => openVersion.click());
    await selectPaper(1);
    expect(extraction().open).toBe(true);
    expect(extraction().textContent).toContain('Use embedded text');
    await act(async () => extraction().querySelector('summary')!.click());
    expect(extraction().open).toBe(false);
    await act(async () => resolveOldSummary(respond({
      version, content: 'Stale content from the previous paper.',
    })));
    expect(container.querySelector('.summary-preview')).toBeNull();
    expect(container.textContent).not.toContain('Stale content from the previous paper.');
    const handoff = container.querySelector<HTMLAnchorElement>('a[href^="/?research="]')!;
    expect(new URL(handoff.href).searchParams.get('research')).toContain('paper ID: paper-1');
    expect(container.querySelectorAll('a[href^="/?research="]')).toHaveLength(1);
    await selectPaper(2);
    expect(extraction().open).toBe(true);
    await selectPaper(0);
    expect(extraction().open).toBe(false);
    expect(container.querySelector('.summary-preview')).toBeNull();
  } finally {
    await act(async () => root.unmount());
    vi.unstubAllGlobals();
  }
});

describe('LibraryTabs', () => {
  it('makes both library destinations visible and identifies the current view', () => {
    const container = document.createElement('div');
    container.innerHTML = renderToStaticMarkup(
      <RouterProvider>
        <LibraryTabs active="notes" />
      </RouterProvider>,
    );

    expect(container.querySelector('nav')?.getAttribute('aria-label')).toBe('Library view');
    expect(container.querySelectorAll('a')).toHaveLength(2);
    expect(container.querySelector('[aria-current="page"]')?.textContent).toContain('Notes');
  });

  it('loads paper folders and creates a new folder', async () => {
    (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
    const requests: Array<{ url: string; init?: RequestInit }> = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        requests.push({ url, init });
        if (url.endsWith('/api/documents')) return respond([]);
        if (url.endsWith('/api/paper-folders') && init?.method === 'POST') {
          return respond({
            id: 'folder-theory',
            name: 'Theory',
            created_at: '2025-01-01T00:00:00Z',
            updated_at: '2025-01-01T00:00:00Z',
          });
        }
        if (url.endsWith('/api/paper-folders')) {
          return respond([
            {
              id: 'folder-methods',
              name: 'Methods',
              created_at: '2025-01-01T00:00:00Z',
              updated_at: '2025-01-01T00:00:00Z',
            },
          ]);
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );
    const container = document.createElement('div');
    const root = createRoot(container);

    await act(async () => {
      root.render(
        <RouterProvider>
          <PapersPage />
        </RouterProvider>,
      );
      await Promise.resolve();
    });
    expect(container.querySelector('[aria-label="Paper folders"]')?.textContent).toContain('Methods');

    const input = container.querySelector<HTMLInputElement>('[aria-label="New paper folder"]')!;
    const setValue = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype,
      'value',
    )!.set!;
    await act(async () => {
      setValue.call(input, 'Theory');
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
    const addButton = container.querySelector<HTMLButtonElement>('.paper-folder-form button')!;
    expect(addButton.disabled).toBe(false);
    await act(async () => {
      container.querySelector<HTMLFormElement>('.paper-folder-form')!.dispatchEvent(
        new SubmitEvent('submit', { bubbles: true, cancelable: true }),
      );
      expect(requests.filter((request) => request.init?.method === 'POST')).toHaveLength(1);
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(container.querySelector('[aria-label="Paper folders"]')?.textContent).toContain('Theory');
    expect(JSON.parse(String(requests.find((request) => request.init?.method === 'POST')?.init?.body))).toEqual({
      name: 'Theory',
    });
    act(() => root.unmount());
    vi.unstubAllGlobals();
  });

  it('deletes folders without deleting their papers and deletes papers from the list', async () => {
    (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
    let folderDeleted = false;
    let paperDeleted = false;
    const requests: Array<{ url: string; init?: RequestInit }> = [];
    const paper = {
      id: 'paper-1',
      title: 'Delete Me & Explain?',
      source_filename: 'delete-me.pdf',
      content_type: 'application/pdf',
      status: 'ready',
      page_count: 1,
      metadata: { folder_id: 'folder-methods' } as Record<string, unknown>,
      created_at: '2025-01-01T00:00:00Z',
      updated_at: '2025-01-01T00:00:00Z',
    };
    vi.stubGlobal('confirm', vi.fn(() => true));
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        requests.push({ url, init });
        if (url.endsWith('/api/paper-folders/folder-methods') && init?.method === 'DELETE') {
          folderDeleted = true;
          paper.metadata = {};
          return noContent();
        }
        if (url.endsWith('/api/documents/paper-1') && init?.method === 'DELETE') {
          paperDeleted = true;
          return noContent();
        }
        if (url.endsWith('/api/documents')) return respond(paperDeleted ? [] : [paper]);
        if (url.endsWith('/api/paper-folders')) {
          return respond(folderDeleted ? [] : [{
            id: 'folder-methods',
            name: 'Methods',
            created_at: '2025-01-01T00:00:00Z',
            updated_at: '2025-01-01T00:00:00Z',
          }]);
        }
        if (url.endsWith('/api/documents/paper-1')) {
          return respond({ ...paper, artifacts: [], chunks: [] });
        }
        if (url.endsWith('/api/documents/paper-1/ingestion-options')) {
          return respond({
            total_pages: 1,
            embedded_text_pages: 1,
            embedded_text_ratio: 1,
            recommended_mode: 'embedded',
            ocr_available: true,
            ocr_engine: 'tesseract',
          });
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );
    const container = document.createElement('div');
    const root = createRoot(container);

    await act(async () => {
      root.render(
        <RouterProvider>
          <PapersPage />
        </RouterProvider>,
      );
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    const discussion = container.querySelector<HTMLAnchorElement>('a[href^="/?research="]')!;
    expect(discussion.textContent).toContain('Discuss paper');
    const prompt = new URL(discussion.href).searchParams.get('research')!;
    expect(prompt).toContain(`"${paper.title}" (paper ID: paper-1)`);
    expect(prompt).toContain('do not create or overwrite a saved summary');
    const extraction = container.querySelector<HTMLDetailsElement>('.library-details')!;
    expect(extraction.querySelector('summary')?.textContent).toBe('Extraction and indexing');
    expect(extraction.open).toBe(false);
    expect(extraction.textContent).toContain('Re-index embedded text');
    expect(discussion.closest('details')).toBeNull();
    await act(async () => extraction.querySelector('summary')!.click());
    expect(extraction.open).toBe(true);
    await act(async () => extraction.querySelector('summary')!.click());
    expect(extraction.open).toBe(false);

    await act(async () => {
      container.querySelector<HTMLButtonElement>('[aria-label="Delete folder Methods"]')!.click();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(requests.some(({ url, init }) =>
      url.endsWith('/api/paper-folders/folder-methods') && init?.method === 'DELETE'
    )).toBe(true);
    expect(container.querySelector('[aria-label="Delete folder Methods"]')).toBeNull();
    expect(container.textContent).toContain('Delete Me');

    await act(async () => {
      container.querySelector<HTMLButtonElement>('[aria-label="Delete paper Delete Me & Explain?"]')!.click();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(requests.some(({ url, init }) =>
      url.endsWith('/api/documents/paper-1') && init?.method === 'DELETE'
    )).toBe(true);
    expect(container.querySelector('[aria-label="Delete paper Delete Me & Explain?"]')).toBeNull();

    act(() => root.unmount());
    vi.unstubAllGlobals();
  });
});
