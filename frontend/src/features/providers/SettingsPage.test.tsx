// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { describe, expect, it, vi } from 'vitest';
import SettingsPage from './SettingsPage';

const runtimeSettings = {
  default_model_references: {}, agent_context_window_tokens: 32768,
  agent_context_use_model_window: true,
  agent_working_context_tokens: 12000, agent_context_response_reserve_tokens: 2048,
  agent_context_model_summary_enabled: true,
  agent_context_high_water_ratio: 0.85, agent_context_compaction_target_tokens: 8000,
  tool_result_max_tokens: 2000, agent_epoch_max_turns: 10, agent_max_epochs: 5,
  agent_run_timeout_seconds: 600, tool_call_timeout_seconds: 60,
  tool_read_retry_attempts: 2, retrieval_max_context_chars: 10000,
  ocr_llm_enhancement_enabled: false, ocr_llm_model: null, ocr_llm_triage_model: null,
};

describe('runtime context settings', () => {
  it('saves the initial response allowance without obsolete fixed caps', async () => {
    (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
    const settings = { ...runtimeSettings, agent_context_use_model_window: false };
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
      const reserve = inputFor('Initial response allowance');
      expect(reserve.min).toBe('256');
      expect(container.textContent).toContain('Automatically grows on truncation');
      const summarize = container.querySelector(
        '[aria-label="Summarize older conversation with the model"]',
      ) as HTMLButtonElement;
      expect(summarize.getAttribute('aria-checked')).toBe('true');
      await act(async () => { summarize.click(); });
      await act(async () => {
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(reserve, '3000');
        reserve.dispatchEvent(new Event('input', { bubbles: true }));
      });
      const save = Array.from(container.querySelectorAll('button'))
        .find((button) => button.textContent?.includes('Save changes'))!;
      await act(async () => { save.click(); });
      expect(saved).toMatchObject({
        agent_context_response_reserve_tokens: 3000,
        agent_context_model_summary_enabled: false,
      });
      for (const key of [
        'agent_context_use_model_window', 'agent_working_context_tokens',
        'agent_context_compaction_target_tokens', 'tool_result_max_tokens',
        'agent_max_epochs', 'agent_run_timeout_seconds', 'tool_call_timeout_seconds',
      ]) expect(saved).not.toHaveProperty(key);
    } finally {
      await act(async () => { root.unmount(); });
      vi.unstubAllGlobals();
    }
  });

  it.each([true, false])('uses model context regardless of legacy automatic flag %s', async (automatic) => {
    (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
    const settings = { ...runtimeSettings, agent_context_use_model_window: automatic };
    let saved: Record<string, unknown> | null = null;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === 'PUT') saved = JSON.parse(String(init.body));
      return new Response(JSON.stringify(
        String(input).endsWith('/providers') ? [] : { ...settings, ...(saved ?? {}) },
      ), { status: 200, headers: { 'content-type': 'application/json' } });
    }));
    const container = document.createElement('div');
    const root = createRoot(container);
    try {
      await act(async () => { root.render(<SettingsPage />); });
      const buttonFor = (name: string) => Array.from(container.querySelectorAll('button'))
        .find((button) => button.textContent?.includes(name))!;
      await act(async () => { buttonFor('Runtime').click(); });
      const inputFor = (name: string) => Array.from(container.querySelectorAll('label'))
        .find((label) => label.textContent?.includes(name))!.querySelector('input')!;
      expect(container.querySelector('[aria-label="Use model context window"]')).toBeNull();
      for (const label of [
        'Working context budget', 'Compaction target', 'Tool result limit',
        'Maximum epochs', 'Run deadline', 'Tool deadline',
      ]) expect(container.textContent).not.toContain(label);
      expect(inputFor('Initial response allowance').disabled).toBe(false);
      expect(inputFor('Context window fallback').disabled).toBe(false);
      expect(inputFor('Compaction high-water ratio').value).toBe('0.85');
      expect(inputFor('Compaction high-water ratio').max).toBe('0.95');
      expect(container.textContent).toContain('Tool outputs remain intact');
      expect(container.textContent).toContain('Exact archived content stays readable');
      expect(container.textContent).toContain('Checkpoint interval (model turns)');
      expect(container.textContent).toContain('no automatic turn, epoch, or elapsed-time ceiling');
      const fallback = inputFor('Context window fallback');
      await act(async () => {
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(fallback, '80000');
        fallback.dispatchEvent(new Event('input', { bubbles: true }));
      });
      await act(async () => { buttonFor('Save changes').click(); });
      expect(saved).toMatchObject({
        agent_context_window_tokens: 80000,
        agent_context_response_reserve_tokens: 2048,
      });
      expect(buttonFor('Save changes').disabled).toBe(true);
    } finally {
      await act(async () => { root.unmount(); });
      vi.unstubAllGlobals();
    }
  });
});
