// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { RouterProvider } from '../../app/router';
import { WritingPanel } from './WritingPanel';
import { SECTIONS, type Track } from './api';

const hash = 'a'.repeat(64);
const stamp = '2026-10-21T00:00:00Z';
const track: Track = {
  id: 'track-1', title: 'Research', folder_path: 'projects/tracks/research',
  created_at: stamp, updated_at: stamp, paper_ids: ['paper-1'], conversation_ids: [],
  sections: SECTIONS.map((section) => ({ section, path: `${section}.md`, sha256: hash, modified_at: stamp })),
  exploration: [], experiments: [], status_counts: {}, warnings: [],
};
let element: HTMLDivElement;
let root: Root;
let writes: { path: string; body: Record<string, unknown> }[];
let conflict = false;
const claim = {
  id: 'claim-1', statement: 'A sourced result', interpretation: 'Limited to this dataset.',
  created_at: stamp, updated_at: stamp,
  evidence: [{ document_id: 'paper-1', paper_title: 'Paper one', page: 2, quote: 'Measured 85 percent.',
    relation: 'supports', source_version: hash, page_sha256: hash, source_status: 'current' }],
};
beforeEach(() => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  writes = []; conflict = false;
  vi.stubGlobal('confirm', vi.fn(() => true));
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost');
    const body = init?.body ? JSON.parse(String(init.body)) : {};
    if (init?.method) writes.push({ path: url.pathname, body });
    if (url.pathname === '/api/references') return Response.json({ revision: 1, references: [{ citation_key: 'paper2026', title: 'Paper one', document_id: 'paper-1' }] });
    if (url.pathname.endsWith('/claims')) return Response.json({ sha256: hash, claims: [claim] });
    if (url.pathname.endsWith('/page')) return Response.json({ document_id: 'paper-1', title: 'Paper one', page: 2, text: 'Measured 85 percent.', source_version: hash });
    if (url.pathname.endsWith('/audit')) return Response.json({ citation_keys: ['missing'], claim_ids: [], warnings: ['Unresolved reference: missing.'] });
    if (url.pathname.endsWith('/manuscript')) {
      if (init?.method === 'PUT' && conflict) return Response.json({ message: 'The track section changed; reload and compare before saving.' }, { status: 409 });
      return Response.json({ content: init?.method === 'PUT' ? body.content : '# Draft', sha256: hash, path: 'projects/tracks/research/exploration/manuscript.md' });
    }
    throw new Error(`Unexpected request: ${url.pathname}`);
  }));
  element = document.createElement('div');
  document.body.append(element);
  root = createRoot(element);
});
afterEach(async () => {
  await act(async () => root.unmount());
  element.remove(); vi.unstubAllGlobals();
});
async function render() {
  await act(async () => root.render(<RouterProvider><WritingPanel track={track} papers={[{ id: 'paper-1', title: 'Paper one' }]} /></RouterProvider>));
}
async function click(text: string) {
  const button = Array.from(element.querySelectorAll('button')).find((item) => item.textContent === text);
  expect(button).toBeDefined();
  await act(async () => button!.click());
}
async function change(label: string, value: string) {
  const input = element.querySelector<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>(`[aria-label="${label}"]`)!;
  const prototype = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
    : input instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
  await act(async () => {
    Object.getOwnPropertyDescriptor(prototype, 'value')!.set!.call(input, value);
    input.dispatchEvent(new Event(input instanceof HTMLSelectElement ? 'change' : 'input', { bubbles: true }));
  });
}

it('inserts source-backed claims and citations into an explicit-save manuscript', async () => {
  await render();
  await click('Insert into manuscript');
  const editor = element.querySelector<HTMLTextAreaElement>('[aria-label="Manuscript Markdown"]')!;
  expect(editor.value).toContain('A sourced result [claim:claim-1] [@paper2026]');
  expect(writes).toHaveLength(0);
  expect(Array.from(element.querySelectorAll('button')).find((item) => item.textContent === 'Export DOCX')!.disabled).toBe(true);
  await click('Save manuscript');
  expect(writes[0].body.expected_sha256).toBe(hash);
  expect(writes[0].body.content).toContain('[claim:claim-1]');
  expect(element.textContent).toContain('Manuscript saved with revision history.');
});

it('retains the manuscript draft on a conflict and offers explicit comparison', async () => {
  await render();
  await change('Manuscript Markdown', '# My unsaved changes');
  conflict = true;
  await click('Save manuscript');
  expect(element.textContent).toContain('section changed');
  expect(element.querySelector<HTMLTextAreaElement>('[aria-label="Manuscript Markdown"]')!.value).toBe('# My unsaved changes');
  await click('Compare saved manuscript');
  expect(element.textContent).toContain('Latest saved manuscript');
  await click('Use compared revision as save base');
  expect(element.querySelector<HTMLTextAreaElement>('[aria-label="Manuscript Markdown"]')!.value).toBe('# My unsaved changes');
});

it('reports a saved manuscript even if inventory refresh fails and retries discovery without replacing drafts', async () => {
  const refresh = vi.fn().mockRejectedValueOnce(new Error('Inventory temporarily unavailable')).mockResolvedValue(undefined);
  await act(async () => root.render(<RouterProvider><WritingPanel track={track} papers={[]} onManuscriptSaved={refresh} /></RouterProvider>));
  await change('Manuscript Markdown', '# Saved manuscript');
  await click('Save manuscript');
  expect(element.textContent).toContain('Manuscript saved with revision history, but the Notebook inventory could not be refreshed');
  expect(element.textContent).toContain('Inventory temporarily unavailable');
  expect(refresh).toHaveBeenCalledOnce();
  await change('Manuscript Markdown', '# Unsaved manuscript continuation');
  await change('Claim statement', 'Unsaved claim statement');
  await click('Refresh Notebook inventory');
  expect(refresh).toHaveBeenCalledTimes(2);
  expect(element.querySelector<HTMLTextAreaElement>('[aria-label="Manuscript Markdown"]')!.value).toBe('# Unsaved manuscript continuation');
  expect(element.querySelector<HTMLTextAreaElement>('[aria-label="Claim statement"]')!.value).toBe('Unsaved claim statement');
  expect(writes.filter((item) => item.path.endsWith('/manuscript'))).toHaveLength(1);
});

it('attaches selected source evidence and exposes audit warnings without saving the draft', async () => {
  await render();
  await change('Claim statement', 'Another observed result');
  await change('Claim source paper', 'paper-1');
  await change('Source page number', '2');
  await click('Read source page');
  await change('Evidence quotation', 'Measured 85 percent.');
  await click('Attach passage to draft');
  await click('Save claim');
  expect(writes[0].body.evidence).toEqual([{ document_id: 'paper-1', page: 2, quote: 'Measured 85 percent.', relation: 'supports' }]);
  expect(writes[0].body.expected_sha256).toBe(hash);
  await click('Check citations and evidence');
  expect(element.textContent).toContain('Unresolved reference: missing.');
  expect(writes.some((item) => item.path.endsWith('/manuscript'))).toBe(false);
});
