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
      title: 'Delete Me',
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
      container.querySelector<HTMLButtonElement>('[aria-label="Delete paper Delete Me"]')!.click();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(requests.some(({ url, init }) =>
      url.endsWith('/api/documents/paper-1') && init?.method === 'DELETE'
    )).toBe(true);
    expect(container.querySelector('[aria-label="Delete paper Delete Me"]')).toBeNull();

    act(() => root.unmount());
    vi.unstubAllGlobals();
  });
});
