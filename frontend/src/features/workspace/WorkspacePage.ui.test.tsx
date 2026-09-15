// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, expect, it, vi } from 'vitest';
import { Link, RouterProvider } from '../../app/router';
import WorkspacePage from './WorkspacePage';

afterEach(() => vi.unstubAllGlobals());

async function change(element: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement, value: string) {
  const prototype = element instanceof HTMLTextAreaElement ? window.HTMLTextAreaElement.prototype
    : element instanceof HTMLSelectElement ? window.HTMLSelectElement.prototype : window.HTMLInputElement.prototype;
  await act(async () => {
    Object.getOwnPropertyDescriptor(prototype, 'value')!.set!.call(element, value);
    element.dispatchEvent(new Event(element instanceof HTMLSelectElement ? 'change' : 'input', { bubbles: true }));
  });
}

it('browses pages, searches tags, creates named notes and retains conflicting drafts', async () => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  const initialUrl = window.location.href;
  window.history.replaceState({}, '', '/library/notes?path=knowledge%2Fexisting.md');
  vi.stubGlobal('scrollTo', vi.fn());
  const confirm = vi.fn(() => false);
  vi.stubGlobal('confirm', confirm);
  const existing = {
    path: 'knowledge/existing.md', name: 'existing.md', note_name: 'Existing insight',
    content: 'Original content', sha256: 'a'.repeat(64), media_type: 'text/markdown',
    size_bytes: 16, modified_at: '2026-01-01T00:00:00Z', tags: ['physics'], kind: 'note',
  };
  const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost');
    if (init?.method === 'PUT') return new Response(JSON.stringify({ detail: 'File changed' }), { status: 409 });
    if (init?.method === 'POST' && url.pathname.endsWith('/files/notes')) {
      const payload = JSON.parse(String(init.body));
      return Response.json({ ...existing, path: 'knowledge/new--id.md', note_name: payload.name, content: payload.content, tags: payload.tags });
    }
    if (url.pathname.endsWith('/search')) return Response.json(
      Array.from({ length: url.searchParams.get('offset') === '25' ? 1 : 25 }, (_, index) => ({
        ...existing, path: `knowledge/${index}.md`, excerpt: 'Relevant evidence',
      })),
    );
    if (url.pathname.endsWith('/files')) return Response.json([existing]);
    return Response.json(existing);
  });
  vi.stubGlobal('fetch', fetch);
  const container = document.createElement('div');
  const root = createRoot(container);
  const button = (text: string) => Array.from(container.querySelectorAll('button')).find((item) => item.textContent === text)!;
  try {
    await act(async () => root.render(<RouterProvider><WorkspacePage /></RouterProvider>));
    expect(container.textContent).toContain('Original content');
    expect(container.textContent).toContain('Standalone note');
    expect(container.textContent).toContain('physics');
    expect(container.querySelectorAll('.file-row')).toHaveLength(25);
    await act(async () => button('Next').click());
    expect(container.textContent).toContain('Page 2');
    expect(container.querySelectorAll('.file-row')).toHaveLength(1);
    await change(container.querySelector('[aria-label="Search notes"]')!, 'quantum');
    await change(container.querySelector('[aria-label="Filter by tags"]')!, 'physics');
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 250)); });
    const searchUrl = new URL(String(fetch.mock.calls[fetch.mock.calls.length - 1][0]), 'http://localhost');
    expect(searchUrl.searchParams.getAll('kinds')).toEqual(['note', 'paper_notes']);
    expect(searchUrl.searchParams.get('query')).toBe('quantum');
    expect(searchUrl.searchParams.get('tags')).toBe('physics');
    expect(searchUrl.searchParams.get('offset')).toBe('0');
    await act(async () => button('Edit').click());
    await change(container.querySelector('textarea')!, 'My protected draft');
    await act(async () => container.querySelector<HTMLButtonElement>('.file-row')!.click());
    expect(confirm).toHaveBeenCalled();
    expect(container.querySelector('textarea')!.value).toBe('My protected draft');
    await act(async () => button('Save').click());
    const write = fetch.mock.calls.find(([, init]) => init?.method === 'PUT')![1]!;
    expect(JSON.parse(String(write.body)).expected_sha256).toBe(existing.sha256);
    expect(container.textContent).toContain('Conflict:');
    expect(container.querySelector('textarea')!.value).toBe('My protected draft');
    expect(container.querySelector('textarea')!.disabled).toBe(false);
    await change(container.querySelector('[aria-label="New note name"]')!, 'Supporting idea');
    confirm.mockReturnValue(true);
    await act(async () => button('New').click());
    await change(container.querySelector('textarea')!, 'Supporting evidence');
    await act(async () => button('Save').click());
    const create = fetch.mock.calls.find(([input, init]) => String(input).endsWith('/files/notes') && init?.method === 'POST')![1]!;
    expect(JSON.parse(String(create.body))).toEqual({ name: 'Supporting idea', content: 'Supporting evidence', tags: [] });
    expect(container.textContent).toContain('Saved locally');
    expect(container.querySelector('a[href*="new--id.md"]')).not.toBeNull();
    expect(new URLSearchParams(window.location.search).get('path')).toBe('knowledge/new--id.md');
    await change(container.querySelector('[aria-label="Workspace view"]')!, 'files');
    expect(container.querySelector('.file-tree-folder')).not.toBeNull();
  } finally {
    await act(async () => root.unmount());
    window.history.replaceState({}, '', initialUrl);
  }
});

it('keeps the editor and route stable while a conditional save is in flight', async () => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  const initialUrl = window.location.href;
  window.history.replaceState({}, '', '/library/notes?path=knowledge%2Fone.md');
  vi.stubGlobal('scrollTo', vi.fn());
  const confirm = vi.fn(() => true);
  vi.stubGlobal('confirm', confirm);
  const file = {
    path: 'knowledge/one.md', name: 'one.md', content: 'Original', sha256: 'a'.repeat(64),
    media_type: 'text/markdown', size_bytes: 8, modified_at: '2026-01-01T00:00:00Z', tags: [], kind: 'note',
  };
  let finishSave: (response: Response) => void = () => { throw new Error('No save pending'); };
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === 'PUT') return new Promise<Response>((resolve) => { finishSave = resolve; });
    return Response.json(String(input).includes('/search?') ? [file] : file);
  }));
  const container = document.createElement('div');
  const root = createRoot(container);
  const button = (text: string) => Array.from(container.querySelectorAll('button')).find((item) => item.textContent === text)!;
  try {
    await act(async () => root.render(<RouterProvider><Link to="/papers">Leave editor</Link><WorkspacePage /></RouterProvider>));
    await act(async () => button('Edit').click());
    await change(container.querySelector('textarea')!, 'Revised');
    await act(async () => button('Save').click());
    expect(container.querySelector('textarea')!.disabled).toBe(true);
    await act(async () => container.querySelector<HTMLAnchorElement>('a[href="/papers"]')!.click());
    expect(window.location.pathname).toBe('/library/notes');
    expect(confirm).not.toHaveBeenCalled();
    await act(async () => finishSave(Response.json({ ...file, content: 'Revised', sha256: 'b'.repeat(64) })));
    expect(container.querySelector('textarea')!.value).toBe('Revised');
    expect(container.querySelector('textarea')!.disabled).toBe(false);
    expect(container.textContent).toContain('Saved locally');
    await act(async () => container.querySelector<HTMLAnchorElement>('a[href="/papers"]')!.click());
    expect(window.location.pathname).toBe('/papers');
  } finally {
    await act(async () => root.unmount());
    window.history.replaceState({}, '', initialUrl);
  }
});

it('hands saved notes to an editable research draft without leaking content or discarding edits', async () => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  const initialUrl = window.location.href;
  vi.stubGlobal('scrollTo', vi.fn());
  let file = {
    path: 'notes/Ideas & questions/note.md', name: 'note.md', content: 'Private saved content.',
    media_type: 'text/markdown', size_bytes: 22, modified_at: '2026-01-01T00:00:00Z',
    tags: [], kind: 'note', sha256: 'a'.repeat(64),
  };
  const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (init?.method === 'PUT') file = { ...file, ...JSON.parse(String(init.body)) };
    return new Response(JSON.stringify(url.includes('/workspace/search?') ? [file] : file), {
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
