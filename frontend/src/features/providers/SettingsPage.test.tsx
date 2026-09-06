// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { describe, expect, it, vi } from 'vitest';
import SettingsPage from './SettingsPage';

describe('runtime context settings', () => {
  it('edits and saves the working budget and response reserve independently', async () => {
    (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
    const settings = {
      default_model_references: {}, agent_context_window_tokens: 32768,
      agent_working_context_tokens: 12000, agent_context_response_reserve_tokens: 2048,
      agent_context_model_summary_enabled: true,
      agent_context_high_water_ratio: 0.8, agent_context_compaction_target_tokens: 8000,
      tool_result_max_tokens: 2000, agent_epoch_max_turns: 10, agent_max_epochs: 5,
      agent_run_timeout_seconds: 600, tool_call_timeout_seconds: 60,
      tool_read_retry_attempts: 2, retrieval_max_context_chars: 10000,
      ocr_llm_enhancement_enabled: false, ocr_llm_model: null, ocr_llm_triage_model: null,
    };
    let saved: Record<string, unknown> | null = null;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === 'PUT') saved = JSON.parse(String(init.body));
      const body = String(input).endsWith('/providers') ? [] : { ...settings, ...(saved ?? {}) };
      return new Response(JSON.stringify(body), {
        status: 200, headers: { 'content-type': 'application/json' },
      });
    }));
    const container = document.createElement('div');
    const root = createRoot(container);
    try {
      await act(async () => { root.render(<SettingsPage />); });
      const runtime = Array.from(container.querySelectorAll('button'))
        .find((button) => button.textContent?.includes('Runtime'))!;
      await act(async () => { runtime.click(); });
      const inputFor = (name: string) => Array.from(container.querySelectorAll('label'))
        .find((label) => label.textContent?.includes(name))!.querySelector('input')!;
      const budget = inputFor('Working context budget');
      const reserve = inputFor('Response reserve');
      expect(budget.min).toBe('2048');
      expect(reserve.min).toBe('256');
      expect(container.textContent).toContain('Desired input size after compaction');
      expect(container.textContent).not.toContain('Compaction floor');
      const summarize = container.querySelector(
        '[aria-label="Summarize older conversation with the model"]',
      ) as HTMLButtonElement;
      expect(summarize.getAttribute('aria-checked')).toBe('true');
      await act(async () => { summarize.click(); });
      for (const [input, value] of [[budget, '10000'], [reserve, '3000']] as const) {
        await act(async () => {
          Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value);
          input.dispatchEvent(new Event('input', { bubbles: true }));
        });
      }
      const save = Array.from(container.querySelectorAll('button'))
        .find((button) => button.textContent?.includes('Save changes'))!;
      await act(async () => { save.click(); });
      expect(saved).toMatchObject({
        agent_working_context_tokens: 10000,
        agent_context_response_reserve_tokens: 3000,
        agent_context_compaction_target_tokens: 8000,
        agent_context_model_summary_enabled: false,
      });
    } finally {
      await act(async () => { root.unmount(); });
      vi.unstubAllGlobals();
    }
  });
});
