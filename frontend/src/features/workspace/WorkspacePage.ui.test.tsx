// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, expect, it, vi } from 'vitest';
import { RouterProvider } from '../../app/router';
import WorkspacePage from './WorkspacePage';

afterEach(() => vi.unstubAllGlobals());

it('hands saved notes to an editable research draft without leaking content or discarding edits', async () => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  const initialUrl = window.location.href;
  vi.stubGlobal('scrollTo', vi.fn());
  let file = {
    path: 'notes/Ideas & questions/note.md', name: 'note.md', content: 'Private saved content.',
    media_type: 'text/markdown', size_bytes: 22, modified_at: '2026-01-01T00:00:00Z',
    tags: [], kind: 'file',
  };
  const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (init?.method === 'PUT') file = { ...file, ...JSON.parse(String(init.body)) };
    return new Response(JSON.stringify(url.endsWith('/workspace/files') ? [file] : file), {
      status: 200, headers: { 'content-type': 'application/json' },
    });
  });
  vi.stubGlobal('fetch', fetch);
  const container = document.createElement('div');
  const root = createRoot(container);
  try {
    await act(async () => root.render(<RouterProvider><WorkspacePage /></RouterProvider>));
    expect(container.querySelector('a[href^="/?research="]')).toBeNull();
    await act(async () => container.querySelector<HTMLButtonElement>('.file-row')!.click());
    const link = container.querySelector<HTMLAnchorElement>('a[href^="/?research="]')!;
    const prompt = new URL(link.href).searchParams.get('research')!;
    expect(prompt).toContain(file.path);
    expect(prompt).toContain('Do not create another saved summary');
    expect(prompt).not.toContain(file.content);
    expect(container.textContent).toContain('Opens a draft you can edit before sending.');
    expect(fetch.mock.calls.every(([, init]) => !init?.method)).toBe(true);

    await act(async () => {
      Array.from(container.querySelectorAll('button')).find((button) => button.textContent === 'Edit')!.click();
    });
    const editor = container.querySelector<HTMLTextAreaElement>('.workspace-editor')!;
    const setValue = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')!.set!;
    await act(async () => {
      setValue.call(editor, 'Changed private content.');
      editor.dispatchEvent(new Event('input', { bubbles: true }));
    });
    expect(container.querySelector('a[href^="/?research="]')).toBeNull();
    expect(Array.from(container.querySelectorAll('button'))
      .find((button) => button.textContent === 'Analyze saved work')?.disabled).toBe(true);
    expect(container.textContent).toContain('Save your changes before discussing them in chat.');
    await act(async () => {
      Array.from(container.querySelectorAll('button')).find((button) => button.textContent === 'Save')!.click();
    });
    expect(container.querySelector('a[href^="/?research="]')).not.toBeNull();
    expect(file.content).toBe('Changed private content.');
    const requestCount = fetch.mock.calls.length;
    await act(async () => container.querySelector<HTMLAnchorElement>('a[href^="/?research="]')!.click());
    expect(window.location.pathname).toBe('/');
    expect(new URLSearchParams(window.location.search).get('research')).toBe(prompt);
    expect(fetch.mock.calls).toHaveLength(requestCount);
  } finally {
    await act(async () => root.unmount());
    window.history.replaceState({}, '', initialUrl);
  }
});
