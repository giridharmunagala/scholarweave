// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { expect, it, vi } from 'vitest';
import { RouterProvider } from '../../app/router';
import { PaperSummaryPanel } from './PaperSummaryPanel';
import { subscribeToRun } from '../../api/events';
import { summaryApi } from './summaryApi';

const version = vi.hoisted(() => ({
  id: 'version', created_at: '2026-01-01T00:00:00Z', citation_count: 1,
  prompt_revision: 'prompt', status: 'overview', review_summary: 'Brief overview.',
  coverage_complete: true, review_complete: false, canonical_updated: false,
  source_version: 'source-revision', model: { model: '/models/local-model.gguf' },
  path: 'papers/paper/summaries/version & review.md',
}));
vi.mock('./summaryApi', () => ({
  summaryApi: {
    versions: vi.fn().mockResolvedValue([version]),
    get: vi.fn().mockResolvedValue({ version, content: 'Overview content.' }),
    start: vi.fn().mockResolvedValue({ run: { id: 'run', status: 'pending' }, prompt_revision: 'prompt' }),
  },
}));
vi.mock('../../api/events', () => ({ subscribeToRun: vi.fn(() => () => undefined) }));
vi.mock('../providers/api', () => ({
  providersApi: {
    list: vi.fn().mockResolvedValue([{
      id: 'local', models: [{ name: 'qwen-27b', reasoning_efforts: ['none', 'high'] }],
    }]),
    settings: vi.fn().mockResolvedValue({
      default_model_references: { chat: { provider_profile_id: 'local', model: 'qwen-27b' } },
    }),
  },
}));

it.each([null, 'high'] as const)('only enables summary reasoning on explicit selection: %s', async (effort) => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  const element = document.createElement('div');
  const root = createRoot(element);
  await act(async () => root.render(<RouterProvider><PaperSummaryPanel documentId="paper" ready /></RouterProvider>));
  const reasoning = element.querySelector<HTMLSelectElement>('[aria-label="Paper summary reasoning"]')!;
  expect(reasoning.value).toBe('');
  expect(reasoning.textContent).toContain('Off by default');
  if (effort) {
    await act(async () => element.querySelector<HTMLElement>('details > summary')!.click());
    await act(async () => {
      reasoning.value = effort;
      reasoning.dispatchEvent(new Event('change', { bubbles: true }));
    });
  }
  const start = Array.from(element.querySelectorAll('button'))
    .find((button) => button.textContent?.includes('Rerun summary'))!;
  await act(async () => start.click());
  expect(summaryApi.start).toHaveBeenLastCalledWith('paper', {
    mode: 'reviewed', ...(effort ? { reasoning_effort: effort } : {}),
  });
  expect(reasoning.disabled).toBe(true);
  await act(async () => root.unmount());
});

it('distinguishes full source coverage from review and exposes saved provenance', async () => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  const element = document.createElement('div');
  const root = createRoot(element);
  await act(async () => root.render(<RouterProvider><PaperSummaryPanel documentId="paper" ready /></RouterProvider>));
  const options = element.querySelector<HTMLDetailsElement>('.library-details')!;
  expect(options.open).toBe(false);
  expect(element.textContent).toContain('Source coverage: complete');
  expect(element.textContent).toContain('not fully reviewed');
  const open = Array.from(element.querySelectorAll('button'))
    .find((button) => button.textContent?.includes('1 citations'))!;
  await act(async () => open.click());
  expect(element.textContent).toContain('Source revision: source-revis');
  expect(element.textContent).toContain('Model: local-model.gguf');
  expect(element.textContent).toContain('the canonical paper summary was not replaced');
  const provenance = Array.from(element.querySelectorAll('details'))
    .find((details) => details.querySelector('summary')?.textContent === 'Summary provenance')!;
  expect(provenance.open).toBe(false);
  expect(element.querySelector('.summary-preview')?.hasAttribute('open')).toBe(true);
  const link = element.querySelector<HTMLAnchorElement>('a[href^="/?research="]')!;
  expect(link.textContent).toContain('Analyze summary');
  const prompt = new URL(link.href).searchParams.get('research')!;
  expect(prompt).toContain(version.path);
  expect(prompt).toContain('paper ID: paper, summary version: version');
  expect(prompt).toContain('Do not generate another saved summary');
  expect(prompt).not.toContain('Overview content.');
  await act(async () => provenance.querySelector('summary')!.click());
  expect(provenance.open).toBe(true);
  await act(async () => provenance.querySelector('summary')!.click());
  expect(provenance.open).toBe(false);
  await act(async () => root.unmount());
});

it('keeps the selected summary mode visible when options are collapsed', async () => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  const element = document.createElement('div');
  const root = createRoot(element);
  try {
    await act(async () => root.render(<RouterProvider><PaperSummaryPanel documentId="paper" ready /></RouterProvider>));
    const options = element.querySelector<HTMLDetailsElement>('.library-details')!;
    const disclosure = options.querySelector('summary')!;
    expect(options.open).toBe(false);
    expect(disclosure.textContent).toBe('Summary options · Reviewed');
    await act(async () => disclosure.click());
    const depth = element.querySelector<HTMLSelectElement>('[aria-label="Paper summary depth"]')!;
    await act(async () => {
      depth.value = 'overview';
      depth.dispatchEvent(new Event('change', { bubbles: true }));
    });
    await act(async () => disclosure.click());
    expect(options.open).toBe(false);
    expect(disclosure.textContent).toBe('Summary options · Quick overview');
    const start = Array.from(element.querySelectorAll('button'))
      .find((button) => button.textContent?.includes('Rerun summary'))!;
    await act(async () => start.click());
    expect(summaryApi.start).toHaveBeenLastCalledWith('paper', { mode: 'overview' });
  } finally {
    await act(async () => root.unmount());
  }
});

it.each([
  'Model response exhausted its output budget.',
  { type: 'ModelBehaviorError', message: 'Model response exhausted its output budget.' },
])('surfaces failed summary diagnostics rather than silently dropping the running state: %j', async (error) => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  const element = document.createElement('div');
  const root = createRoot(element);
  await act(async () => root.render(<RouterProvider><PaperSummaryPanel documentId="paper" ready /></RouterProvider>));
  const start = Array.from(element.querySelectorAll('button'))
    .find((button) => button.textContent?.includes('Rerun summary'))!;
  await act(async () => start.click());
  const calls = vi.mocked(subscribeToRun).mock.calls;
  const onEvent = calls[calls.length - 1][2];
  await act(async () => onEvent({
    sequence: 1, event_type: 'run.failed', payload: { error },
  }));
  expect(element.textContent).toContain('Model response exhausted its output budget.');
  expect(element.textContent).not.toContain('Summarizing…');
  await act(async () => root.unmount());
});
