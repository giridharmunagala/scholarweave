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
});
