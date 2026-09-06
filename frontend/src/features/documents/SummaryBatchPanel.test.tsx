// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { RouterProvider } from '../../app/router';
import { SummaryBatchPanel } from './SummaryBatchPanel';
import { summaryApi } from './summaryApi';

vi.mock('../providers/api', () => ({
  providersApi: { list: vi.fn().mockResolvedValue([{
    id: 'local', models: [{ name: 'small-model', reasoning_efforts: ['none', 'high'] }],
  }]) },
}));
vi.mock('../providers/ModelDefaultsPanel', () => ({
  capabilityOptions: () => [{
    providerId: 'local', providerName: 'Local llama', model: 'small-model', capabilities: ['chat'],
  }],
}));
vi.mock('../../api/events', () => ({ subscribeToRun: vi.fn(() => () => undefined) }));
vi.mock('./summaryApi', () => ({
  summaryApi: { startBatch: vi.fn().mockResolvedValue({
    runs: [{ document_id: 'p1', prompt_revision: 'revision', run: { id: 'r1', status: 'pending', agent_name: 'Summary' } }],
  }) },
}));

afterEach(() => vi.clearAllMocks());

describe('SummaryBatchPanel', () => {
  it.each([null, 'high'] as const)('queues sequential summaries with opt-in reasoning: %s', async (effort) => {
    (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
    HTMLElement.prototype.scrollIntoView = vi.fn();
    const element = document.createElement('div');
    const root = createRoot(element);
    await act(async () => {
      root.render(<RouterProvider><SummaryBatchPanel papers={[{
        id: 'p1', title: 'Attention paper', status: 'ready', source_filename: 'p.pdf',
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
    expect(queueButton().disabled).toBe(true);
    await act(async () => element.querySelector<HTMLButtonElement>('[aria-label="Summary batch model"]')!.click());
    const option = Array.from(element.querySelectorAll<HTMLElement>('[role="option"]'))
      .find((item) => item.textContent?.includes('small-model'))!;
    await act(async () => option.click());
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
