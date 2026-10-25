// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { ArxivSearch } from './ArxivSearch';

let container: HTMLDivElement;
let root: Root;
const paper = { arxiv_id: '2610.01234', title: 'Calibration methods', summary: 'A metadata abstract.',
  authors: ['A. Researcher'], published_at: '2026-10-01', updated_at: '2026-10-01',
  url: 'https://arxiv.org/abs/2610.01234', pdf_url: 'https://arxiv.org/pdf/2610.01234', categories: ['cs.LG'] };
const choose = vi.fn();
const button = (label: string) => [...container.querySelectorAll('button')].find((item) => item.textContent === label)!;
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  choose.mockClear();
  container = document.createElement('div'); document.body.append(container); root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove(); vi.unstubAllGlobals();
});
async function search() {
  await act(async () => root.render(<ArxivSearch onChoose={choose} />));
  await act(async () => {
    const input = container.querySelector('input')!;
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, 'calibration');
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await act(async () => button('Search arXiv').click());
}
it('searches explicitly and only adds a result on a separate choice', async () => {
  const fetch = vi.fn(async (_url: string) => new Response(JSON.stringify({ query: 'calibration', provider: 'arxiv', results: [paper], next_start: 10 })));
  vi.stubGlobal('fetch', fetch);
  await search();
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(String(fetch.mock.calls[0]?.[0])).toContain('/research/arxiv?query=calibration&start=0');
  expect(choose).not.toHaveBeenCalled();
  expect(container.textContent).toContain('A metadata abstract.');
  await act(async () => button('Use as input').click());
  expect(choose).toHaveBeenCalledWith(paper);
  await act(async () => button('More arXiv results').click());
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(container.querySelectorAll('article')).toHaveLength(1);
});
it.each([
  [JSON.stringify({ detail: 'arXiv unavailable' }), 'arXiv unavailable'],
  ['Internal Server Error', 'Internal Server Error'],
])('shows search failures without downloading or choosing a paper: %s', async (body, message) => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(body, { status: 502 })));
  await search();
  expect(container.textContent).toContain(message);
  expect(choose).not.toHaveBeenCalled();
});
it('keeps large collaboration author lists intact when choosing a compact search result', async () => {
  const collaboration = { ...paper, authors: Array.from({ length: 1001 }, (_, index) => `Researcher ${index}`) };
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
    query: 'calibration', provider: 'arxiv', results: [collaboration], next_start: null,
  }))));
  await search();
  expect(container.textContent).toContain('1001 authors');
  expect(container.textContent).not.toContain('Researcher 1000');
  await act(async () => button('Use as input').click());
  expect(choose).toHaveBeenCalledWith(collaboration);
});
it('keeps selected PDF glyphs transparent despite the global selection text color', () => {
  const libraryCss = readFileSync(resolve('src', 'features', 'library.css'), 'utf8');
  const baseCss = readFileSync(resolve('src', 'shared', 'styles', 'base.css'), 'utf8');
  expect(baseCss).toContain('color: var(--text-strong)');
  const rule = libraryCss.match(/\.pdf-reader \.textLayer ::selection\s*\{([^}]+)\}/)?.[1];
  expect(rule).toContain('color: transparent');
  expect(rule).toContain('text-shadow: none');
  expect(rule).toContain('background:');
});
