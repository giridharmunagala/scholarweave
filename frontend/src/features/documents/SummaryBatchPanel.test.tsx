// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { RouterProvider } from '../../app/router';
import { SummaryBatchPanel } from './SummaryBatchPanel';
import { summaryApi } from './summaryApi';
import { providersApi } from '../providers/api';

vi.mock('../providers/api', async (importOriginal) => ({
  ...await importOriginal<typeof import('../providers/api')>(),
  providersApi: { list: vi.fn().mockResolvedValue([{
    id: 'local', models: [{ name: 'small-model', reasoning_efforts: ['none', 'high'] }],
  }]), settings: vi.fn().mockResolvedValue({ default_model_references: {} }) },
}));
vi.mock('../providers/ModelDefaultsPanel', () => ({
  capabilityOptions: () => [{
    providerId: 'local', providerName: 'Local llama', model: 'small-model', capabilities: ['chat'],
  }],
}));
vi.mock('../../api/events', () => ({ subscribeToRun: vi.fn(() => () => undefined) }));
vi.mock('./summaryApi', () => ({
  summaryApi: { activeRuns: vi.fn().mockResolvedValue([]), cancel: vi.fn(), startBatch: vi.fn().mockResolvedValue({
    runs: [{ document_id: 'p1', prompt_revision: 'revision', run: { id: 'r1', status: 'pending', agent_name: 'Summary' } }],
  }) },
}));

afterEach(() => vi.clearAllMocks());
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(summaryApi.activeRuns).mockResolvedValue([]);
});

describe('SummaryBatchPanel', () => {
  it('updates visual support when the batch model changes', async () => {
    Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
    HTMLElement.prototype.scrollIntoView = vi.fn();
    const providers = await providersApi.list();
    vi.mocked(providersApi.list).mockResolvedValueOnce([{
      ...providers[0], models: [{
        ...providers[0].models[0], capabilities: ['chat', 'tools', 'vision'],
      }],
    }]);
    const element = document.createElement('div');
    const root = createRoot(element);
    try {
      await act(async () => root.render(<RouterProvider><SummaryBatchPanel papers={[]} /></RouterProvider>));
      expect(element.textContent).not.toContain('Vision available');
      await act(async () => element.querySelector<HTMLButtonElement>('[aria-label="Summary batch model"]')!.click());
      await act(async () => element.querySelector<HTMLElement>('[role="option"]')!.click());
      expect(element.textContent).toContain('Vision available: summaries include PDF page images');
      expect(summaryApi.startBatch).not.toHaveBeenCalled();
    } finally {
      await act(async () => root.unmount());
    }
  });
  it('recovers active jobs after closing and reopening the batch panel', async () => {
    (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
    const run = { id: 'r1', status: 'running', agent_name: 'Summary' };
    vi.mocked(summaryApi.activeRuns).mockResolvedValue([{ document_id: 'p1', prompt_revision: 'revision', run }] as never);
    vi.mocked(summaryApi.cancel).mockResolvedValue({ ...run, status: 'cancelled' } as never);
    const element = document.createElement('div');
    let root = createRoot(element);
    try {
      await act(async () => root.render(<RouterProvider><SummaryBatchPanel papers={[]} /></RouterProvider>));
      await act(async () => root.unmount());
      root = createRoot(element);
      await act(async () => root.render(<RouterProvider><SummaryBatchPanel papers={[]} /></RouterProvider>));
      expect(summaryApi.activeRuns).toHaveBeenCalledTimes(2);
      expect(summaryApi.startBatch).not.toHaveBeenCalled();
      expect(element.textContent).toContain('running');
      expect([...element.querySelectorAll('button')].find((button) => button.textContent?.startsWith('Queue'))!.disabled).toBe(true);
      await act(async () => [...element.querySelectorAll('button')].find((button) => button.textContent === 'Cancel')!.click());
      expect(summaryApi.cancel).toHaveBeenCalledWith('r1');
      expect(element.textContent).toContain('cancelled');
    } finally {
      await act(async () => root.unmount());
    }
  });
  it.each([null, 'high'] as const)('queues sequential summaries with default or explicit models and reasoning: %s', async (effort) => {
    (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
    HTMLElement.prototype.scrollIntoView = vi.fn();
    if (effort === null) {
      vi.mocked(providersApi.settings).mockResolvedValueOnce({
        ...await providersApi.settings(),
        default_model_references: {
          chat: { provider_profile_id: 'local', model: 'large-model' },
          small_tasks: { provider_profile_id: 'local', model: 'small-model' },
        },
      });
    }
    const element = document.createElement('div');
    const root = createRoot(element);
    await act(async () => {
      root.render(<RouterProvider><SummaryBatchPanel papers={[{
        id: 'p1', title: 'Attention paper', status: 'ready', usefulness: 'unrated', source_filename: 'p.pdf',
        content_type: 'application/pdf', page_count: 12, metadata: {},
        created_at: '2026-09-06T00:00:00Z', updated_at: '2026-09-06T00:00:00Z',
      }]} /></RouterProvider>);
    });
    const queueButton = () => Array.from(element.querySelectorAll('button'))
      .find((button) => button.textContent?.startsWith('Queue'))!;
    expect(queueButton().disabled).toBe(true);
    expect(summaryApi.startBatch).not.toHaveBeenCalled();
    expect(element.textContent).toContain('Queue papers sequentially');
    expect(element.textContent).toContain('Only one LLM call runs at a time');
    await act(async () => element.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click());
    expect(queueButton().disabled).toBe(effort !== null);
    if (effort !== null) {
      await act(async () => element.querySelector<HTMLButtonElement>('[aria-label="Summary batch model"]')!.click());
      const option = Array.from(element.querySelectorAll<HTMLElement>('[role="option"]'))
        .find((item) => item.textContent?.includes('small-model'))!;
      await act(async () => option.click());
    }
    const reasoning = element.querySelector<HTMLSelectElement>('[aria-label="Summary batch reasoning"]')!;
    expect(reasoning.value).toBe('');
    if (effort) {
      await act(async () => {
        reasoning.value = effort;
        reasoning.dispatchEvent(new Event('change', { bubbles: true }));
      });
    }
    await act(async () => {
      const select = element.querySelector<HTMLSelectElement>('[aria-label="Summary batch depth"]')!;
      select.value = 'overview';
      select.dispatchEvent(new Event('change', { bubbles: true }));
    });
    expect(queueButton().disabled).toBe(false);
    await act(async () => queueButton().click());
    expect(summaryApi.startBatch).toHaveBeenCalledWith(['p1'], {
      model_reference: { provider_profile_id: 'local', model: 'small-model' }, mode: 'overview',
      ...(effort ? { reasoning_effort: effort } : {}),
    });
    expect(element.textContent).toContain('Attention paper');
    expect(queueButton().disabled).toBe(true);
    await act(async () => root.unmount());
  });
});
