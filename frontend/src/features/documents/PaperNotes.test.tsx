// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, expect, it, vi } from 'vitest';
import { RouterProvider } from '../../app/router';
import { PaperNotesDrawer, usePaperNotes } from './PaperNotes';

afterEach(() => vi.unstubAllGlobals());

it.each([false, true])('uses tag-only discovery and preserves lookup failures when appending: %s', async (failure) => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  const file = { path: 'library/papers/paper--p1/notes.md', content: 'Saved notes\n', sha256: 'a'.repeat(64) };
  const requests: { url: URL; init?: RequestInit }[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost');
    requests.push({ url, init });
    if (url.pathname.endsWith('/search')) {
      expect(url.searchParams.has('query')).toBe(false);
      expect(url.searchParams.get('tags')).toBe('paper:p1');
      return failure ? Response.json({ message: 'Index unavailable' }, { status: 503 }) : Response.json([file]);
    }
    return Response.json(file);
  }));
  const element = document.createElement('div');
  const root = createRoot(element);
  let notes!: ReturnType<typeof usePaperNotes>;
  function Harness() { notes = usePaperNotes('p1'); return null; }
  try {
    await act(async () => root.render(<Harness />));
    if (failure) {
      expect(notes.error).toBe('Index unavailable');
      await act(async () => { await expect(notes.append('Quote')).rejects.toThrow('Index unavailable'); });
      expect(requests.every(({ init }) => !init?.method)).toBe(true);
    } else {
      await act(async () => { await notes.append('Quote'); });
      const write = requests.find(({ init }) => init?.method === 'PATCH')!;
      expect(JSON.parse(String(write.init!.body))).toMatchObject({
        path: file.path, content: '\nQuote\n', expected_sha256: file.sha256,
      });
    }
  } finally {
    await act(async () => root.unmount());
  }
});

it('creates missing notes only for an explicit append after successful discovery', async () => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  const file = { path: 'library/papers/paper--p1/notes.md', content: 'Existing notes preserved\n', sha256: 'original' };
  const requests: { url: URL; init?: RequestInit }[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost');
    requests.push({ url, init });
    if (url.pathname.endsWith('/search')) return Response.json([]);
    if (url.pathname === '/api/documents/p1/notes') return Response.json(file);
    if (init?.method === 'PATCH') return Response.json({ ...file, content: `${file.content}\nQuote\n`, sha256: 'appended' });
    throw new Error(`Unexpected request: ${url}`);
  }));
  const root = createRoot(document.createElement('div'));
  let notes!: ReturnType<typeof usePaperNotes>;
  function Harness() { notes = usePaperNotes('p1'); return null; }
  try {
    await act(async () => root.render(<Harness />));
    expect(notes.notes).toBeNull();
    expect(notes.error).toBeNull();
    expect(requests.every(({ init }) => !init?.method)).toBe(true);
    await act(async () => { await notes.append('Quote'); });
    const create = requests.find(({ init }) => init?.method === 'POST')!;
    expect(create.url.pathname).toBe('/api/documents/p1/notes');
    expect(create.init?.body).toBeUndefined();
    const append = requests.find(({ init }) => init?.method === 'PATCH')!;
    expect(JSON.parse(String(append.init?.body))).toMatchObject({
      path: file.path, content: '\nQuote\n', expected_sha256: file.sha256,
    });
    expect(notes.notes?.content).toContain('Existing notes preserved');
    expect(notes.notes?.content).toContain('Quote');
  } finally {
    await act(async () => root.unmount());
  }
});

it('does not append when explicit note creation fails for a missing paper', async () => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (!init?.method) return Response.json([]);
    expect(String(input)).toBe('/api/documents/p1/notes');
    expect(init.method).toBe('POST');
    return Response.json({ message: 'Paper not found' }, { status: 404 });
  });
  vi.stubGlobal('fetch', fetch);
  const root = createRoot(document.createElement('div'));
  let notes!: ReturnType<typeof usePaperNotes>;
  function Harness() { notes = usePaperNotes('p1'); return null; }
  try {
    await act(async () => root.render(<Harness />));
    await act(async () => { await expect(notes.append('Quote')).rejects.toThrow('Paper not found'); });
    expect(fetch.mock.calls.some(([, init]) => init?.method === 'PATCH')).toBe(false);
    expect(notes.notes).toBeNull();
  } finally {
    await act(async () => root.unmount());
  }
});

function notesDrawer() {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  const element = document.createElement('div');
  const root = createRoot(element);
  let notes!: ReturnType<typeof usePaperNotes>;
  function Harness({ paperId, open }: { paperId: string; open: boolean }) {
    notes = usePaperNotes(paperId);
    return <PaperNotesDrawer paperNotes={notes} open={open} onClose={() => undefined} />;
  }
  return {
    element,
    get notes() { return notes; },
    render: async (paperId: string, open = true) => {
      await act(async () => root.render(<RouterProvider><Harness paperId={paperId} open={open} /></RouterProvider>));
    },
    click: async (label: string) => {
      const button = [...element.querySelectorAll('button')].find((item) => item.textContent?.trim() === label)!;
      await act(async () => button.click());
    },
    type: async (value: string) => {
      const editor = element.querySelector('textarea')!;
      await act(async () => {
        Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(editor, value);
        editor.dispatchEvent(new Event('input', { bubbles: true }));
      });
    },
    unmount: async () => { await act(async () => root.unmount()); },
  };
}

it('offers explicit creation in an empty drawer and edits the revision returned by ensure', async () => {
  const file = { path: 'library/papers/paper--p1/notes.md', content: 'Recovered notes\n', sha256: 'recovered' };
  const writes: { method: string; body?: BodyInit | null }[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (!init?.method) return Response.json([]);
    writes.push({ method: init.method, body: init.body });
    if (init.method === 'POST') return Response.json(file);
    return Response.json({ ...file, content: 'Manual notes', sha256: 'saved' });
  }));
  const drawer = notesDrawer();
  try {
    await drawer.render('p1');
    expect(writes).toEqual([]);
    expect(drawer.element.textContent).toContain('No notes file exists');
    await drawer.click('Create notes');
    expect(writes).toEqual([{ method: 'POST', body: undefined }]);
    expect(drawer.element.querySelector('textarea')?.value).toBe('Recovered notes\n');
    await drawer.type('Manual notes');
    await drawer.click('Save');
    expect(JSON.parse(String(writes[1].body))).toEqual({
      path: file.path, content: 'Manual notes', expected_sha256: 'recovered',
    });
    expect(drawer.element.querySelector('textarea')).toBeNull();
    expect(drawer.element.textContent).toContain('Manual notes');
  } finally {
    await drawer.unmount();
  }
});

it('keeps failed discovery visible without offering creation', async () => {
  const fetch = vi.fn(async () => Response.json({ message: 'Index unavailable' }, { status: 503 }));
  vi.stubGlobal('fetch', fetch);
  const drawer = notesDrawer();
  try {
    await drawer.render('p1');
    expect(drawer.element.textContent).toContain('Index unavailable');
    expect(drawer.element.textContent).not.toContain('Create notes');
    expect(fetch).toHaveBeenCalledTimes(1);
  } finally {
    await drawer.unmount();
  }
});

it('rejects cross-paper drafts and stale callbacks even after returning to the original paper', async () => {
  const files = {
    p1: { path: 'library/papers/p1/notes.md', content: 'First paper', sha256: 'first' },
    p2: { path: 'library/papers/p2/notes.md', content: 'Second paper', sha256: 'second' },
  };
  const writes: RequestInit[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost');
    if (init?.method) { writes.push(init); return Response.json(files.p2); }
    if (url.pathname.endsWith('/search')) return Response.json([url.searchParams.get('tags') === 'paper:p1' ? files.p1 : files.p2]);
    return Response.json(url.searchParams.get('path') === files.p1.path ? files.p1 : files.p2);
  }));
  const drawer = notesDrawer();
  try {
    await drawer.render('p1');
    const first = drawer.notes.notes!;
    const staleSave = drawer.notes.save;
    await drawer.click('Edit');
    await drawer.type('First paper draft');
    await drawer.render('p2');
    expect(drawer.element.querySelector('textarea')).toBeNull();
    await expect(drawer.notes.save(first, 'First paper draft')).rejects.toThrow('another paper');
    await expect(staleSave(first, 'First paper draft')).rejects.toThrow('open paper has changed');
    await drawer.click('Edit');
    expect(drawer.element.querySelector('textarea')?.value).toBe('Second paper');
    await drawer.render('p1');
    await expect(staleSave(first, 'First paper draft')).rejects.toThrow('open paper has changed');
    expect(writes).toEqual([]);
  } finally {
    await drawer.unmount();
  }
});

it('fences a late save completion from a different paper and disables editing during save', async () => {
  const first = { path: 'library/papers/p1/notes.md', content: 'First paper', sha256: 'first' };
  const second = { path: 'library/papers/p2/notes.md', content: 'Second paper', sha256: 'second' };
  let complete!: (response: Response) => void;
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost');
    if (init?.method === 'PUT') return new Promise<Response>((resolve) => { complete = resolve; });
    if (url.pathname.endsWith('/search')) return Response.json([url.searchParams.get('tags') === 'paper:p1' ? first : second]);
    return Response.json(url.searchParams.get('path') === first.path ? first : second);
  }));
  const drawer = notesDrawer();
  try {
    await drawer.render('p1');
    await drawer.click('Edit');
    await drawer.type('Saved first paper');
    await drawer.click('Save');
    expect(drawer.element.querySelector('textarea')?.disabled).toBe(true);
    await drawer.render('p2');
    await drawer.click('Edit');
    await drawer.type('Second paper draft');
    await act(async () => complete(Response.json({ ...first, content: 'Saved first paper', sha256: 'saved' })));
    expect(drawer.notes.notes).toMatchObject(second);
    expect(drawer.element.querySelector('textarea')?.value).toBe('Second paper draft');
    expect(drawer.element.textContent).not.toContain('Saving…');
  } finally {
    await drawer.unmount();
  }
});
