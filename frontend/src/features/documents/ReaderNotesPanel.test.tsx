// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, expect, it, vi } from 'vitest';
import { RouterProvider } from '../../app/router';
import { ReaderNotesPanel } from './ReaderNotesPanel';

afterEach(() => vi.unstubAllGlobals());

it('connects highlights across papers with a revision guard and keeps failed selections for retry', async () => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  const first = { id: 'first', paper_id: 'p1', paper_name: 'First paper', text: 'Quasar evidence', page: 2,
    path: 'notes1.md', sha256: 'a'.repeat(64), connection_ids: [], match_type: 'direct', connected_via: [] };
  const second = { ...first, id: 'second', paper_id: 'p2', paper_name: 'Second paper', text: 'Stellar evidence', page: 5 };
  let conflict = true;
  const writes: unknown[] = [];
  const reload = vi.fn(async () => undefined);
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost');
    if (init?.method === 'POST') {
      writes.push(JSON.parse(String(init.body)));
      return conflict ? Response.json({ message: 'The workspace file changed; reload before saving.' }, { status: 409 }) : Response.json({});
    }
    const items = url.searchParams.has('paper_id') ? [first] : [first, second];
    return Response.json({ items, total: items.length, next_offset: null });
  }));
  const element = document.createElement('div');
  const root = createRoot(element);
  const click = async (text: string) => { await act(async () => {
    [...element.querySelectorAll('button')].find((button) => button.textContent === text)!.click();
  }); };
  try {
    await act(async () => root.render(<RouterProvider><ReaderNotesPanel paperId="p1" onChanged={reload} /></RouterProvider>));
    expect(element.textContent).toContain('Quasar evidence');
    expect(element.textContent).not.toContain('Stellar evidence');
    await click('Connect highlight');
    expect(element.textContent).toContain('Second paper');
    await click('Connect this highlight');
    expect(writes[0]).toEqual({ source_id: 'first', target_id: 'second', expected_sha256: first.sha256 });
    expect(element.textContent).toContain('The workspace file changed');
    expect(reload).not.toHaveBeenCalled();
    conflict = false;
    await click('Connect this highlight');
    expect(reload).toHaveBeenCalledOnce();
    expect(element.textContent).toContain('Highlights connected');
    expect(element.textContent).not.toContain('Connect a highlight');
  } finally { await act(async () => root.unmount()); }
});

it('opens an exact highlight and distinguishes connected search results from direct matches', async () => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  const urls: URL[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), 'http://localhost');
    urls.push(url);
    return Response.json({ items: [{
      id: 'target', paper_id: 'p2', paper_name: 'Target paper', text: 'Related observation', page: 3,
      path: 'notes.md', sha256: 'b'.repeat(64), connection_ids: ['source'], match_type: 'connected', connected_via: ['source'],
    }], total: 1, next_offset: null });
  }));
  const element = document.createElement('div');
  const root = createRoot(element);
  try {
    await act(async () => root.render(<RouterProvider><ReaderNotesPanel paperId="p2" highlightId="target" /></RouterProvider>));
    expect(urls[0].searchParams.get('note_id')).toBe('target');
    expect(element.textContent).toContain('Via a connected highlight');
    expect(element.querySelector('a')?.getAttribute('href')).toContain('page=3&highlight=target');
    await act(async () => [...element.querySelectorAll('button')].find((button) => button.textContent === '1 connections')!.click());
    expect(urls.at(-1)?.searchParams.get('connected_to')).toBe('target');
    expect(urls.at(-1)?.searchParams.has('paper_id')).toBe(false);
  } finally { await act(async () => root.unmount()); }
});
