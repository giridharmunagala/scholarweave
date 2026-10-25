// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { PdfReader } from './PdfReader';

vi.mock('pdfjs-dist/build/pdf.worker.min.mjs?url', () => ({ default: 'worker.js' }));
vi.mock('pdfjs-dist', () => ({
  GlobalWorkerOptions: {},
  getDocument: () => ({ promise: Promise.resolve({
    numPages: 1, destroy: vi.fn(),
    getPage: async () => ({
      getViewport: ({ scale }: { scale: number }) => ({ width: 612 * scale, height: 792 * scale }),
      render: () => ({ promise: Promise.resolve(), cancel: vi.fn() }),
      streamTextContent: () => [],
    }),
  }) }),
  TextLayer: class { render = async () => undefined; cancel = () => undefined; },
}));

it('retains explicit page widths at high zoom with overflow-safe automatic centering', async () => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  const style = document.createElement('style');
  style.textContent = readFileSync(join(process.cwd(), 'src', 'features', 'library.css'), 'utf8');
  document.head.append(style);
  const element = document.createElement('div');
  document.body.append(element);
  const root = createRoot(element);
  const context = vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({} as never);
  try {
    await act(async () => root.render(<PdfReader url="/fixture.pdf" title="Paper" onQuote={vi.fn()} onNotice={vi.fn()} />));
    await vi.waitFor(async () => {
      await act(async () => { await Promise.resolve(); });
      expect(element.querySelector('.pdf-page'), element.textContent ?? '').not.toBeNull();
    });
    const zoom = element.querySelector<HTMLButtonElement>('[aria-label="Zoom in"]')!;
    for (let index = 0; index < 4; index++) await act(async () => zoom.click());
    const page = element.querySelector<HTMLElement>('.pdf-page')!;
    expect(page.style.width).toBe('1530px');
    expect(getComputedStyle(element.querySelector('.pdf-scroll')!).alignItems).toBe('flex-start');
    expect(getComputedStyle(page).marginInline).toBe('auto');
    await act(async () => element.querySelector<HTMLButtonElement>('.pdf-zoom-label')!.click());
    expect(page.style.width).toBe('612px');
  } finally {
    await act(async () => root.unmount());
    context.mockRestore();
    element.remove();
    style.remove();
  }
});
