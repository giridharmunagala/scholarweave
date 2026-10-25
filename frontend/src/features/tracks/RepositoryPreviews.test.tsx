// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { RepositoryTextPreview, parseCsvPreview } from './RepositoryTextPreview';
import { RepositoryPdfPreview } from './RepositoryPdfPreview';

const pdf = vi.hoisted(() => {
  const render = vi.fn(() => ({ promise: Promise.resolve(), cancel: vi.fn() }));
  const page = { getViewport: vi.fn(() => ({ width: 612, height: 792 })), render,
    getTextContent: vi.fn(async () => ({ items: [{ str: 'Accessible report text', hasEOL: true }] })) };
  const doc = { numPages: 2, getPage: vi.fn(async () => page) };
  return { doc, page, render, getDocument: vi.fn(() => ({ promise: Promise.resolve(doc), destroy: vi.fn() })) };
});
vi.mock('pdfjs-dist', () => ({ getDocument: pdf.getDocument, GlobalWorkerOptions: {} }));
vi.mock('pdfjs-dist/build/pdf.worker.min.mjs?url', () => ({ default: '/local-worker.js' }));
let element: HTMLDivElement;
let root: Root;
beforeEach(() => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  pdf.page.getTextContent.mockResolvedValue({ items: [{ str: 'Accessible report text', hasEOL: true }] });
  element = document.createElement('div'); document.body.appendChild(element); root = createRoot(element);
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({} as CanvasRenderingContext2D);
});
afterEach(async () => { await act(async () => root.unmount()); element.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it('renders quoted CSV cells, embedded newlines and bounded rows', async () => {
  expect(parseCsvPreview('name,value\r\n"a,b","line1\nline2"\r\n"quoted ""name""",2').rows)
    .toEqual([['name', 'value'], ['a,b', 'line1\nline2'], ['quoted "name"', '2']]);
  expect(parseCsvPreview('a\nb\nc', 2).truncated).toBe(true);
  await act(async () => root.render(<RepositoryTextPreview path="data.csv" content={'name,value\nmetric,0.8'} onOpenRelative={vi.fn()} />));
  expect(element.querySelectorAll('td')).toHaveLength(4);
});

it('reports malformed CSV rather than presenting a misleading parsed table', async () => {
  expect(() => parseCsvPreview('name,value\n"unfinished')).toThrow('Unclosed quoted');
  await act(async () => root.render(<RepositoryTextPreview path="broken.csv" content={'a,"value"trailing'} onOpenRelative={vi.fn()} />));
  expect(element.textContent).toContain('Invalid CSV');
  expect(element.querySelector('table')).toBeNull();
});

it('previews saved notebook cells and text outputs without executing active content', async () => {
  const content = JSON.stringify({ nbformat: 4, cells: [
    { cell_type: 'markdown', source: ['# Analysis\n', 'Saved description.'] },
    { cell_type: 'code', source: ['print(score)'], outputs: [{ output_type: 'stream', text: ['0.85\n'] },
      { output_type: 'display_data', data: { 'text/html': '<script>alert(1)</script>' } }] },
  ] });
  await act(async () => root.render(<RepositoryTextPreview path="analysis.ipynb" content={content} onOpenRelative={vi.fn()} />));
  expect(element.querySelector('h1')?.textContent).toBe('Analysis');
  expect(element.textContent).toContain('print(score)');
  expect(element.textContent).toContain('0.85');
  expect(element.textContent).toContain('Some saved output formats are not rendered');
  expect(element.querySelector('script,iframe,object')).toBeNull();
});

it('formats JSON and renders Markdown with repository-local links and no remote images', async () => {
  await act(async () => root.render(<RepositoryTextPreview path="data.json" content='{"score":0.8}' onOpenRelative={vi.fn()} />));
  expect(element.querySelector('pre')?.textContent).toBe('{\n  "score": 0.8\n}');
  const open = vi.fn();
  await act(async () => root.render(<RepositoryTextPreview path="reports/readme.md" onOpenRelative={open}
    content={'# Report\n\n[metrics](../results.csv)\n\n![remote](https://example.invalid/a.png)\n\n<img src="https://example.invalid/track">\n\n![reference][img]\n\n[img]: https://example.invalid/b.png'} />));
  expect(element.querySelector('h1')?.textContent).toBe('Report');
  expect(element.querySelector('img')).toBeNull();
  await act(async () => element.querySelector<HTMLAnchorElement>('a')!.click());
  expect(open).toHaveBeenCalledWith('results.csv');
});

it('renders PDFs locally with selectable accessible page text and controls, without native PDF embedding', async () => {
  await act(async () => root.render(<RepositoryPdfPreview url="/api/report/media" title="report.pdf" />));
  expect(pdf.getDocument).toHaveBeenCalledWith({ url: '/api/report/media', isEvalSupported: false, enableXfa: false });
  expect(pdf.render).toHaveBeenCalled();
  expect(element.querySelector('canvas')).toBeTruthy();
  expect(element.querySelector('canvas')?.getAttribute('aria-hidden')).toBe('true');
  expect(element.querySelector('[aria-label="Selectable PDF text, page 1"] pre')?.textContent).toBe('Accessible report text');
  expect(element.querySelector('[aria-label="Selectable PDF text, page 1"] pre')?.getAttribute('tabindex')).toBe('0');
  expect(element.querySelector('iframe,object,embed')).toBeNull();
  const next = Array.from(element.querySelectorAll('button')).find((button) => button.textContent === 'Next PDF page')!;
  await act(async () => next.click());
  expect(pdf.doc.getPage).toHaveBeenCalledWith(2);
  expect(element.textContent).toContain('Page 2 of 2');
  expect(element.querySelector('[aria-label="Selectable PDF text, page 2"] pre')?.textContent).toBe('Accessible report text');
});

it('retains the text fallback when canvas rendering fails', async () => {
  vi.mocked(HTMLCanvasElement.prototype.getContext).mockReturnValue(null);
  await act(async () => root.render(<RepositoryPdfPreview url="/api/report/media" title="report.pdf" />));
  expect(element.textContent).toContain('Canvas rendering is unavailable');
  expect(element.querySelector('[aria-label="Selectable PDF text, page 1"] pre')?.textContent).toBe('Accessible report text');
});

it('offers the original PDF for scanned pages and reports extraction failures', async () => {
  pdf.page.getTextContent.mockResolvedValueOnce({ items: [] });
  await act(async () => root.render(<RepositoryPdfPreview url="/api/report/media" title="report.pdf" />));
  expect(element.textContent).toContain('No selectable text was found');
  expect(element.textContent).toContain('Download original PDF');
  pdf.page.getTextContent.mockRejectedValueOnce(new Error('Text stream unavailable'));
  await act(async () => Array.from(element.querySelectorAll('button')).find((button) => button.textContent === 'Next PDF page')!.click());
  expect(element.querySelector('[role="alert"]')?.textContent).toContain('Text stream unavailable');
});

it('downloads only the revision-checked authorized PDF and reports revoked or changed files', async () => {
  const createObjectURL = vi.fn(() => 'blob:authorized-report');
  const revokeObjectURL = vi.fn();
  vi.stubGlobal('URL', Object.assign(class extends URL {}, { createObjectURL, revokeObjectURL }));
  const download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(Response.json({ message: 'Media changed' }, { status: 409 }))
    .mockResolvedValueOnce(new Response('%PDF-1.7\nreport', { headers: { 'Content-Type': 'application/pdf' } })));
  const url = '/api/tracks/track/repositories/repo/media?path=report.pdf&expected_sha256=abc';
  await act(async () => root.render(<RepositoryPdfPreview url={url} title="reports/report.pdf" />));
  const original = Array.from(element.querySelectorAll('button')).find((button) => button.textContent === 'Download original PDF')!;
  await act(async () => original.click());
  expect(fetch).toHaveBeenCalledWith(url);
  expect(createObjectURL).not.toHaveBeenCalled();
  expect(element.querySelector('[role="alert"]')?.textContent).toContain('Original PDF unavailable (409)');
  await act(async () => original.click());
  expect(createObjectURL).toHaveBeenCalledOnce();
  expect(download).toHaveBeenCalledOnce();
  expect(download.mock.contexts[0]).toHaveProperty('download', 'report.pdf');
  expect(revokeObjectURL).toHaveBeenCalledWith('blob:authorized-report');
  expect(element.querySelector('[role="alert"]')).toBeNull();
});
