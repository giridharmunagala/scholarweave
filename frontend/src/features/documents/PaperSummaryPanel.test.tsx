// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { expect, it, vi } from 'vitest';
import { PaperSummaryPanel } from './PaperSummaryPanel';
import { subscribeToRun } from '../../api/events';

const version = vi.hoisted(() => ({
  id: 'version', created_at: '2026-01-01T00:00:00Z', citation_count: 1,
  prompt_revision: 'prompt', status: 'overview', review_summary: 'Brief overview.',
  coverage_complete: true, review_complete: false, canonical_updated: false,
  source_version: 'source-revision', model: { model: '/models/local-model.gguf' },
}));
vi.mock('./summaryApi', () => ({
  summaryApi: {
    versions: vi.fn().mockResolvedValue([version]),
    get: vi.fn().mockResolvedValue({ version, content: 'Overview content.' }),
    start: vi.fn().mockResolvedValue({ run: { id: 'run', status: 'pending' }, prompt_revision: 'prompt' }),
  },
}));
vi.mock('../../api/events', () => ({ subscribeToRun: vi.fn(() => () => undefined) }));

it('distinguishes full source coverage from review and exposes saved provenance', async () => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  const element = document.createElement('div');
  const root = createRoot(element);
  await act(async () => root.render(<PaperSummaryPanel documentId="paper" ready />));
  expect(element.textContent).toContain('Source coverage: complete');
  expect(element.textContent).toContain('not fully reviewed');
  const open = Array.from(element.querySelectorAll('button'))
    .find((button) => button.textContent?.includes('1 citations'))!;
  await act(async () => open.click());
  expect(element.textContent).toContain('Source revision: source-revis');
  expect(element.textContent).toContain('Model: local-model.gguf');
  expect(element.textContent).toContain('the canonical paper summary was not replaced');
  await act(async () => root.unmount());
});

it.each([
  'Model response exhausted its output budget.',
  { type: 'ModelBehaviorError', message: 'Model response exhausted its output budget.' },
])('surfaces failed summary diagnostics rather than silently dropping the running state: %j', async (error) => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  const element = document.createElement('div');
  const root = createRoot(element);
  await act(async () => root.render(<PaperSummaryPanel documentId="paper" ready />));
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
