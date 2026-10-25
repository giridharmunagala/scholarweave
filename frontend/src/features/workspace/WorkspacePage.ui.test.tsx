// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { Link, RouterProvider } from '../../app/router';
import WorkspacePage from './WorkspacePage';

afterEach(() => vi.unstubAllGlobals());
beforeEach(() => {
  vi.stubGlobal('scrollTo', vi.fn());
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); };
});

it.each(['cancel', 'success', 'preview-error', 'execute-error', 'empty', 'inspection-failed'])(
  'cleans up only confirmed notes and preserves the open selection and draft: %s',
  async (outcome) => {
    (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
    const oldUrl = window.location.href;
    window.history.replaceState({}, '', '/library/notes?path=knowledge%2Fopen.md');
    const file = {
      path: 'knowledge/open.md', name: 'open.md', note_name: 'Open note', content: 'Saved stub',
      sha256: 'a'.repeat(64), media_type: 'text/markdown', size_bytes: 10,
      modified_at: '2026-10-09T09:00:00Z', tags: [], kind: 'note',
    };
    const candidates = [file.path, 'knowledge/remove.md', 'knowledge/changed.md', 'knowledge/locked.md']
      .map((path) => ({ path, size_bytes: 10, sha256: 'a'.repeat(64) }));
    const confirm = vi.fn((_message?: string) => outcome !== 'cancel');
    vi.stubGlobal('confirm', confirm);
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith('/cleanup/preview')) {
        if (outcome === 'preview-error') return Response.json({ message: 'Cannot inspect notes' }, { status: 500 });
        return Response.json({
          threshold_bytes: 1024,
          candidates: ['empty', 'inspection-failed'].includes(outcome) ? [candidates[0]] : candidates,
          failed: outcome === 'inspection-failed' ? [{ path: 'knowledge/unreadable.md', reason: 'Cannot read note' }] : [],
        });
      }
      if (path.endsWith('/notes/cleanup')) {
        if (outcome === 'execute-error') return Response.json({ message: 'Cleanup unavailable' }, { status: 500 });
        return Response.json({
          deleted_paths: ['knowledge/remove.md'],
          skipped: [{ path: 'knowledge/changed.md', reason: 'Note changed since preview' }],
          failed: [{ path: 'knowledge/locked.md', reason: 'File is locked' }],
        });
      }
      return Response.json(path.includes('/content?') ? file : [file]);
    });
    vi.stubGlobal('fetch', fetch);
    const container = document.createElement('div');
    document.body.append(container);
    const root = createRoot(container);
    const button = (text: string) => [...container.querySelectorAll('button')].find((item) => item.textContent === text)!;
    try {
      await act(async () => root.render(<RouterProvider><WorkspacePage /></RouterProvider>));
      expect(container.querySelector('dialog')?.hasAttribute('open')).toBe(false);
      expect(container.querySelector('[aria-label="Search notes"]')?.closest('dialog')).toBeNull();
      expect(container.querySelector('[aria-label="New note name"]')?.closest('dialog')).toBeNull();
      await act(async () => button('Edit').click());
      await change(container.querySelector('[aria-label="Note content"]')!, 'Keep this unsaved evidence');
      await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Notes settings"]')!.click());
      expect(container.querySelector('dialog')?.hasAttribute('open')).toBe(true);
      const trigger = container.querySelector('[aria-label="Notes settings"]')!;
      expect(trigger.getAttribute('aria-haspopup')).toBe('dialog');
      expect(trigger.getAttribute('aria-expanded')).toBe('true');
      expect(trigger.getAttribute('aria-controls')).toBe('notes-settings');
      await act(async () => button('Clean up notes under 1 KB').click());
      const execute = fetch.mock.calls.find(([input]) => String(input).endsWith('/notes/cleanup'));
      if (outcome === 'success' || outcome === 'execute-error') {
        expect(JSON.parse(String(execute![1]!.body))).toEqual({ candidates: candidates.slice(1) });
      } else expect(execute).toBeUndefined();
      if (outcome === 'success') {
        expect(container.textContent).toContain('Removed 1 note. 1 skipped; 1 failed.');
        expect(container.textContent).toContain('Note changed since preview');
        expect(container.textContent).toContain('File is locked');
      } else if (outcome.endsWith('error')) {
        expect(container.textContent).toContain('Cleanup could not be completed.');
        expect(container.textContent).toContain(outcome === 'preview-error' ? 'Cannot inspect notes' : 'Cleanup unavailable');
      } else if (outcome === 'cancel') {
        expect(container.textContent).toContain('Cleanup canceled. No notes were removed.');
      } else if (outcome === 'inspection-failed') {
        expect(container.textContent).toContain('1 could not be inspected and were kept.');
        expect(container.textContent).toContain('Cannot read note');
      } else {
        expect(container.textContent).toContain('No notes smaller than 1024 bytes to remove.');
      }
      if (['cancel', 'success', 'execute-error'].includes(outcome)) {
        expect(confirm).toHaveBeenCalledTimes(1);
        expect(confirm.mock.calls[0][0]).toContain('Permanently delete 3 notes');
        expect(confirm.mock.calls[0][0]).toContain('open note are kept');
      } else expect(confirm).not.toHaveBeenCalled();
      await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Close notes settings"]')!.click());
      expect(container.querySelector('dialog')?.hasAttribute('open')).toBe(false);
      expect(container.querySelector<HTMLTextAreaElement>('[aria-label="Note content"]')!.value).toBe('Keep this unsaved evidence');
      expect(container.querySelector('.file-row')?.getAttribute('aria-current')).toBe('true');
      expect(container.querySelector('.workspace-reader h2')?.textContent).toBe('Open note');
      expect(window.location.search).toContain('knowledge%2Fopen.md');
      expect(fetch.mock.calls.some(([, init]) => init?.method === 'DELETE' || init?.method === 'PUT')).toBe(false);
    } finally {
      await act(async () => root.unmount());
      container.remove();
      window.history.replaceState({}, '', oldUrl);
    }
  },
);

it('keeps an unsaved new note and settings filters when closing and reopening Notes settings', async () => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  const oldUrl = window.location.href;
  window.history.replaceState({}, '', '/library/notes');
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => Response.json(
    String(input).endsWith('/cleanup/preview') ? { candidates: [], failed: [], threshold_bytes: 1024 } : [],
  )));
  const container = document.createElement('div');
  const root = createRoot(container);
  const button = (text: string) => [...container.querySelectorAll('button')].find((item) => item.textContent?.trim() === text)!;
  try {
    await act(async () => root.render(<RouterProvider><WorkspacePage /></RouterProvider>));
    await change(container.querySelector('[aria-label="New note name"]')!, 'New evidence');
    await act(async () => button('New').click());
    await change(container.querySelector('[aria-label="Note content"]')!, 'Unsaved new note');
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Notes settings"]')!.click());
    await change(container.querySelector('[aria-label="Filter by tags"]')!, 'keep');
    await act(async () => button('Clean up notes under 1 KB').click());
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Close notes settings"]')!.click());
    expect(container.querySelector('[aria-label="Active note filters"]')?.textContent).toContain('Tags: keep');
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Notes settings"]')!.click());
    expect(container.querySelector<HTMLInputElement>('[aria-label="Filter by tags"]')!.value).toBe('keep');
    expect(container.querySelector<HTMLTextAreaElement>('[aria-label="Note content"]')!.value).toBe('Unsaved new note');
    expect(container.querySelector('.workspace-reader h2')?.textContent).toBe('New evidence');
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Close notes settings"]')!.click());
    await act(async () => button('Reset filters').click());
    expect(container.querySelector('[aria-label="Active note filters"]')).toBeNull();
    expect(container.querySelector<HTMLTextAreaElement>('[aria-label="Note content"]')!.value).toBe('Unsaved new note');
  } finally {
    await act(async () => root.unmount());
    window.history.replaceState({}, '', oldUrl);
  }
});

it('groups artifacts by storage location and searches nested folders without moving files', async () => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  const oldUrl = window.location.href;
  window.history.replaceState({}, '', '/library/notes');
  const files = [
    'knowledge/idea.md',
    'research/galaxies/review-1/report.md',
    'research/biology/review-2/report.md',
    'custom/archive/receipt.json',
  ].map((path) => ({
    path, name: path.split('/').pop(), size_bytes: 100,
    modified_at: '2026-10-09T09:00:00Z', tags: [], kind: 'file', media_type: 'text/markdown',
  }));
  const fetch = vi.fn(async () => Response.json(files));
  vi.stubGlobal('fetch', fetch);
  const container = document.createElement('div');
  const root = createRoot(container);
  const area = (label: string) => [...container.querySelectorAll<HTMLButtonElement>('.artifact-area')]
    .find((button) => button.querySelector('strong')?.textContent === label)!;
  try {
    await act(async () => root.render(<RouterProvider><WorkspacePage /></RouterProvider>));
    const picker = container.querySelector<HTMLSelectElement>('[aria-label="Workspace view"]')!;
    expect(picker).not.toBeNull();
    expect(picker.closest('dialog')).toBeNull();
    expect(picker.closest('.workspace-browser')).not.toBeNull();
    expect(container.querySelector('dialog')?.hasAttribute('open')).toBe(false);
    expect([...picker.options].map((option) => option.value)).toEqual(['notes', 'standalone', 'paper-notes', 'summaries', 'files']);
    await change(container.querySelector('[aria-label="Workspace view"]')!, 'files');
    expect(container.querySelectorAll('.file-row')).toHaveLength(4);
    expect(area('Research artifacts').textContent).toContain('2 files');
    await act(async () => area('Research artifacts').click());
    expect(container.querySelectorAll('.file-row')).toHaveLength(2);
    await change(container.querySelector('[aria-label="Find artifact"]')!, 'GALAXIES');
    expect(container.querySelectorAll('.file-row')).toHaveLength(1);
    expect(container.querySelector('.file-row')?.getAttribute('title')).toBe(files[1].path);
    expect([...container.querySelectorAll<HTMLDetailsElement>('.file-tree-folder')].every((folder) => folder.open)).toBe(true);
    expect(container.querySelector('.folder-delete-button')).toBeNull();
    await change(container.querySelector('[aria-label="Find artifact"]')!, 'missing');
    expect(container.textContent).toContain('No artifacts match this location or name.');
    await change(container.querySelector('[aria-label="Find artifact"]')!, '');
    await act(async () => area('All locations').click());
    expect(container.querySelector('[title="custom/archive/receipt.json"]')).not.toBeNull();
    expect(fetch.mock.calls).toHaveLength(2);
  } finally {
    await act(async () => root.unmount());
    window.history.replaceState({}, '', oldUrl);
  }
});

it('preserves the editor draft and browsing filters when focusing the reader', async () => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  const oldUrl = window.location.href;
  window.history.replaceState({}, '', '/library/notes?path=knowledge%2Fidea.md');
  const file = {
    path: 'knowledge/idea.md', name: 'idea.md', note_name: 'A readable research title',
    content: 'Saved evidence', sha256: 'a'.repeat(64), media_type: 'text/markdown',
    size_bytes: 100, modified_at: '2026-10-09T09:00:00Z', tags: [], kind: 'note',
  };
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) =>
    Response.json(String(input).includes('/content?') ? file : [file])));
  const container = document.createElement('div');
  const root = createRoot(container);
  const button = (text: string) => [...container.querySelectorAll('button')].find((item) => item.textContent === text)!;
  try {
    await act(async () => root.render(<RouterProvider><WorkspacePage /></RouterProvider>));
    expect(container.querySelector('.workspace-reader h2')?.textContent).toBe(file.note_name);
    expect(container.querySelector('.file-row')?.getAttribute('aria-current')).toBe('true');
    await act(async () => button('Edit').click());
    await change(container.querySelector('[aria-label="Note content"]')!, 'Unsaved evidence');
    await change(container.querySelector('[aria-label="Workspace view"]')!, 'summaries');
    await act(async () => button('Focus reader').click());
    expect(container.querySelector('.workspace-layout')?.classList.contains('workspace-focused')).toBe(true);
    expect(button('Show artifact browser').getAttribute('aria-pressed')).toBe('true');
    await act(async () => button('Show artifact browser').click());
    expect(container.querySelector<HTMLTextAreaElement>('[aria-label="Note content"]')?.value).toBe('Unsaved evidence');
    expect(container.querySelector<HTMLSelectElement>('[aria-label="Workspace view"]')?.value).toBe('summaries');
  } finally {
    await act(async () => root.unmount());
    window.history.replaceState({}, '', oldUrl);
  }
});

async function change(element: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement, value: string) {
  const prototype = element instanceof HTMLTextAreaElement ? window.HTMLTextAreaElement.prototype
    : element instanceof HTMLSelectElement ? window.HTMLSelectElement.prototype : window.HTMLInputElement.prototype;
  await act(async () => {
    Object.getOwnPropertyDescriptor(prototype, 'value')!.set!.call(element, value);
    element.dispatchEvent(new Event(element instanceof HTMLSelectElement ? 'change' : 'input', { bubbles: true }));
  });
}

it.each([{ value: [1, true, null] }, ['a', 2], 'a JSON string', 5, false, null])('saves JSON as typed content rather than double-encoded text: %j', async (content) => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  const oldUrl = window.location.href;
  window.history.replaceState({}, '', '/library/notes?path=data%2Fresult.json');
  const file = {
    path: 'data/result.json', name: 'result.json', content: { old: true },
    sha256: 'a'.repeat(64), media_type: 'application/json', tags: [], kind: 'file',
    size_bytes: 100, modified_at: '2026-10-09T09:00:00Z',
  };
  const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === 'PUT') return Response.json({ ...file, content: JSON.parse(String(init.body)).content });
    return Response.json(String(input).includes('/content?') ? file : []);
  });
  vi.stubGlobal('fetch', fetch);
  const container = document.createElement('div');
  const root = createRoot(container);
  try {
    await act(async () => root.render(<RouterProvider><WorkspacePage /></RouterProvider>));
    await change(container.querySelector<HTMLTextAreaElement>('[aria-label="Note content"]')!, JSON.stringify(content));
    const save = [...container.querySelectorAll('button')].find((button) => button.textContent === 'Save')!;
    await act(async () => save.click());
    const write = fetch.mock.calls.find(([, init]) => init?.method === 'PUT')![1]!;
    expect(JSON.parse(String(write.body)).content).toEqual(content);
    expect(container.querySelector<HTMLTextAreaElement>('textarea')!.value).toBe(JSON.stringify(content, null, 2));
    expect(container.textContent).toContain('Saved locally');
  } finally {
    await act(async () => root.unmount());
    window.history.replaceState({}, '', oldUrl);
  }
});

it('retains invalid JSON without sending a corrupt-success save', async () => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  const oldUrl = window.location.href;
  window.history.replaceState({}, '', '/library/notes?path=data%2Fresult.json');
  const file = {
    path: 'data/result.json', name: 'result.json', content: { old: true },
    sha256: 'a'.repeat(64), media_type: 'application/json', tags: [], kind: 'file',
    size_bytes: 100, modified_at: '2026-10-09T09:00:00Z',
  };
  const fetch = vi.fn(async (input: RequestInfo | URL) => Response.json(String(input).includes('/content?') ? file : []));
  vi.stubGlobal('fetch', fetch);
  const container = document.createElement('div');
  const root = createRoot(container);
  try {
    await act(async () => root.render(<RouterProvider><WorkspacePage /></RouterProvider>));
    await change(container.querySelector('textarea')!, '{ invalid JSON');
    await act(async () => [...container.querySelectorAll('button')].find((button) => button.textContent === 'Save')!.click());
    expect(fetch.mock.calls).toHaveLength(2);
    expect(container.textContent).toContain('Invalid JSON');
    expect(container.querySelector('textarea')!.value).toBe('{ invalid JSON');
    expect(container.textContent).toContain('Save failed. Your draft is retained.');
  } finally {
    await act(async () => root.unmount());
    window.history.replaceState({}, '', oldUrl);
  }
});

it.each([
  ['projects/tracks/my-track--t1/problem.md', 'track_section', '/tracks?id=t1'],
  ['library/papers/paper--p1/summaries/run.json', 'paper_file', '/library?paper=p1'],
])('opens managed artifacts read-only with an owner link: %s', async (path, kind, owner) => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  const oldUrl = window.location.href;
  window.history.replaceState({}, '', `/library/notes?path=${encodeURIComponent(path)}`);
  const file = {
    path, kind, paper_id: kind === 'paper_file' ? 'p1' : null, name: path.split('/').pop(),
    content: path.endsWith('.json') ? { receipt: true } : 'Managed problem',
    sha256: 'a'.repeat(64), media_type: path.endsWith('.json') ? 'application/json' : 'text/markdown',
    tags: [], size_bytes: 100, modified_at: '2026-10-09T09:00:00Z',
  };
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => Response.json(String(input).includes('/content?') ? file : [])));
  const container = document.createElement('div');
  const root = createRoot(container);
  try {
    await act(async () => root.render(<RouterProvider><WorkspacePage /></RouterProvider>));
    expect(container.textContent).toContain('Read-only artifact');
    expect([...container.querySelectorAll('button')].find((button) => button.textContent === 'Save')).toBeUndefined();
    expect(container.querySelector(`a[href="${owner}"]`)).not.toBeNull();
    const source = [...container.querySelectorAll('button')].find((button) => button.textContent === 'View source');
    if (source) await act(async () => source.click());
    expect(container.querySelector('textarea')!.readOnly).toBe(true);
    expect(container.querySelector<HTMLInputElement>('[aria-label="Tags"]')!.disabled).toBe(true);
  } finally {
    await act(async () => root.unmount());
    window.history.replaceState({}, '', oldUrl);
  }
});

it('organizes notes by kind and tags, labels canonical files by paper, and collapses individual folders', async () => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  const oldUrl = window.location.href;
  window.history.replaceState({}, '', '/library/notes');
  const files = ['paper_notes', 'paper_summary'].map((kind, index) => ({
    path: `library/papers/discovery--paper-${index}/${index ? 'summary' : 'notes'}.md`,
    name: `${index ? 'summary' : 'notes'}.md`, paper_name: `Discovery paper ${index}`,
    paper_id: `paper-${index}`,
    size_bytes: 100, modified_at: '2026-10-09T09:00:00Z',
    tags: ['paper', index ? 'summary' : 'notes', `paper:paper-${index}`, 'astronomy'],
    kind, media_type: 'text/markdown',
  }));
  const searches: URL[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), 'http://localhost');
    if (url.pathname.endsWith('/search')) searches.push(url);
    return Response.json(files);
  }));
  const container = document.createElement('div');
  const root = createRoot(container);
  try {
    await act(async () => root.render(<RouterProvider><WorkspacePage /></RouterProvider>));
    expect(container.querySelector('.file-row strong')?.textContent).toBe('Discovery paper 0');
    expect([...container.querySelectorAll('.library-tags button')].map((button) => button.textContent)).toEqual(['astronomy', 'astronomy']);
    await change(container.querySelector('[aria-label="Workspace view"]')!, 'standalone');
    expect(searches[searches.length - 1].searchParams.getAll('kinds')).toEqual(['note']);
    await change(container.querySelector('[aria-label="Workspace view"]')!, 'paper-notes');
    expect(searches[searches.length - 1].searchParams.getAll('kinds')).toEqual(['paper_notes']);
    await act(async () => container.querySelector<HTMLButtonElement>('.library-tags button')!.click());
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 250)); });
    expect(searches[searches.length - 1].searchParams.getAll('tags')).toEqual(['astronomy']);
    expect(container.querySelector<HTMLInputElement>('[aria-label="Filter by tags"]')!.value).toBe('astronomy');
    expect(container.querySelector('.library-tags button')?.getAttribute('aria-pressed')).toBe('true');
    await act(async () => container.querySelector<HTMLButtonElement>('.library-tags button')!.click());
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 250)); });
    expect(searches[searches.length - 1].searchParams.getAll('tags')).toEqual([]);
    expect(container.querySelector<HTMLInputElement>('[aria-label="Filter by tags"]')!.value).toBe('');
    expect(container.querySelector('.library-tags button')?.getAttribute('aria-pressed')).toBe('false');
    await change(container.querySelector('[aria-label="Workspace view"]')!, 'files');
    expect(container.querySelector('.file-row strong')?.textContent).toBe('notes.md');
    const folders = [...container.querySelectorAll<HTMLDetailsElement>('.file-tree-folder')];
    expect(folders.filter((folder) => folder.querySelector(':scope > summary')?.textContent?.includes('Discovery paper'))).toHaveLength(2);
    expect(folders.slice(-2).every((folder) => !folder.open)).toBe(true);
    await act(async () => folders[folders.length - 1].querySelector('summary')!.click());
    expect(folders[folders.length - 1].open).toBe(true);
  } finally {
    await act(async () => root.unmount());
    window.history.replaceState({}, '', oldUrl);
  }
});

it('omits tag controls entirely for automatic paper/note labels without removing stored metadata', async () => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  const oldUrl = window.location.href;
  window.history.replaceState({}, '', '/library/notes');
  const files = [
    { kind: 'paper_notes', tags: ['paper', 'notes', 'paper:paper-1'] },
    { kind: 'paper_summary', tags: ['paper', 'summary', 'paper:paper-2'] },
    { kind: 'note', tags: ['note', 'brief-source:source-id'] },
  ].map((file, index) => ({
    ...file, path: `knowledge/${index}.md`, name: `${index}.md`,
    size_bytes: 100, modified_at: '2026-10-09T09:00:00Z', media_type: 'text/markdown',
  }));
  const fetch = vi.fn(async () => Response.json(files));
  vi.stubGlobal('fetch', fetch);
  const container = document.createElement('div');
  const root = createRoot(container);
  try {
    await act(async () => root.render(<RouterProvider><WorkspacePage /></RouterProvider>));
    expect(container.querySelectorAll('.file-row')).toHaveLength(3);
    expect(container.querySelector('.library-tags')).toBeNull();
    expect(files[0].tags).toEqual(['paper', 'notes', 'paper:paper-1']);
    expect(fetch).toHaveBeenCalledTimes(1);
  } finally {
    await act(async () => root.unmount());
    window.history.replaceState({}, '', oldUrl);
  }
});

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
    expect(container.querySelector('a[href^="/chat?research="]')).toBeNull();
    await act(async () => container.querySelector<HTMLButtonElement>('.file-row')!.click());
    const link = container.querySelector<HTMLAnchorElement>('a[href^="/chat?research="]')!;
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
    expect(container.querySelector('a[href^="/chat?research="]')).toBeNull();
    expect(Array.from(container.querySelectorAll('button'))
      .find((button) => button.textContent === 'Analyze saved work')?.disabled).toBe(true);
    expect(container.textContent).toContain('Save your changes before discussing them in chat.');
    await act(async () => {
      Array.from(container.querySelectorAll('button')).find((button) => button.textContent === 'Save')!.click();
    });
    expect(container.querySelector('a[href^="/chat?research="]')).not.toBeNull();
    expect(file.content).toBe('Changed private content.');
    const requestCount = fetch.mock.calls.length;
    await act(async () => container.querySelector<HTMLAnchorElement>('a[href^="/chat?research="]')!.click());
    expect(window.location.pathname).toBe('/chat');
    expect(new URLSearchParams(window.location.search).get('research')).toBe(prompt);
    expect(fetch.mock.calls).toHaveLength(requestCount);
  } finally {
    await act(async () => root.unmount());
    window.history.replaceState({}, '', initialUrl);
  }
});
