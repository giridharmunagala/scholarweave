// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import SettingsPage from './SettingsPage';
import { providersApi, type Provider, type Settings } from './api';

const settings = {
  decision_parallel_requests: 8, default_model_references: {},
  agent_context_window_tokens: 32768, agent_context_response_reserve_tokens: 2048,
  agent_context_model_summary_enabled: true, agent_context_high_water_ratio: 0.85,
  agent_epoch_max_turns: 10, tool_read_retry_attempts: 2, retrieval_max_context_chars: 10000,
  ocr_llm_enhancement_enabled: false, ocr_llm_model: null, ocr_llm_triage_model: null,
} as unknown as Settings;
const provider: Provider = {
  id: 'local', name: 'Local', kind: 'ollama', state: 'active', models: [],
  base_url: 'http://localhost:11434', api_mode: 'chat_completions', api_key_set: false, serialize_model_switches: true,
  created_at: '', updated_at: '',
};

describe('settings load recovery and draft isolation', () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;
  beforeEach(() => {
    Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    vi.spyOn(providersApi, 'settings').mockResolvedValue(settings);
    vi.spyOn(providersApi, 'list').mockResolvedValue([]);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.restoreAllMocks();
  });
  const button = (name: string) => [...container.querySelectorAll<HTMLButtonElement>('button')]
    .find((entry) => entry.textContent?.trim() === name)!;
  async function click(name: string) {
    await act(async () => button(name).click());
  }
  const allowance = () => [...container.querySelectorAll('label')]
    .find((label) => label.textContent?.includes('Initial response allowance'))!.querySelector('input')!;
  async function editAllowance(value: string) {
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(allowance(), value);
      allowance().dispatchEvent(new Event('input', { bubbles: true }));
    });
  }

  it('shows the initial settings failure with a working retry instead of indefinite loading', async () => {
    vi.mocked(providersApi.settings).mockRejectedValueOnce(new Error('Settings unavailable'));
    await act(async () => root.render(<SettingsPage />));
    expect(container.textContent).toContain('Settings unavailable');
    expect(container.textContent).not.toContain('Loading settings');
    await click('Retry settings');
    expect(container.textContent).toContain('Default models');
    expect(container.textContent).not.toContain('Settings unavailable');
  });

  it('keeps settings editable when provider listing fails and recovers independently', async () => {
    vi.mocked(providersApi.list).mockRejectedValueOnce(new Error('Provider list unavailable'));
    await act(async () => root.render(<SettingsPage />));
    expect(container.textContent).toContain('Default models');
    expect(container.textContent).toContain('Provider list unavailable');
    await click('Runtime');
    await editAllowance('3000');
    await click('Retry providers and discovery');
    expect(container.textContent).not.toContain('Provider list unavailable');
    expect(container.textContent).toContain('Unsaved changes');
    expect(providersApi.settings).toHaveBeenCalledOnce();
  });

  it('does not await Ollama discovery before showing saved settings and exposes discovery retry', async () => {
    vi.mocked(providersApi.list).mockResolvedValue([provider]);
    let rejectDiscovery!: (error: Error) => void;
    vi.spyOn(providersApi, 'discover').mockImplementationOnce(() => new Promise((_, reject) => {
      rejectDiscovery = reject;
    })).mockResolvedValue({ models: [] });
    await act(async () => root.render(<SettingsPage />));
    expect(container.textContent).toContain('Default models');
    expect(container.textContent).toContain('Discovering Ollama models');
    await click('Runtime');
    await editAllowance('3000');
    await act(async () => rejectDiscovery(new Error('Ollama offline')));
    expect(container.textContent).toContain('Ollama offline');
    expect(container.textContent).toContain('Unsaved changes');
    await click('Retry providers and discovery');
    expect(container.textContent).not.toContain('Ollama offline');
    expect(providersApi.settings).toHaveBeenCalledOnce();
  });

  it('preserves the settings draft and visible save bar across provider mutations and failed saves', async () => {
    vi.mocked(providersApi.list).mockResolvedValue([{ ...provider, kind: 'openai_compatible' }]);
    vi.spyOn(providersApi, 'update').mockResolvedValue(provider);
    const save = vi.spyOn(providersApi, 'updateSettings')
      .mockRejectedValueOnce(new Error('Save failed'))
      .mockImplementation(async (payload) => ({ ...settings, ...payload } as Settings));
    await act(async () => root.render(<SettingsPage />));
    await click('Runtime');
    await editAllowance('3000');
    await click('Providers');
    expect(container.textContent).toContain('Unsaved changes');
    const select = container.querySelector<HTMLSelectElement>('[aria-label="Generation API for Local"]')!;
    await act(async () => {
      select.value = 'responses';
      select.dispatchEvent(new Event('change', { bubbles: true }));
    });
    expect(providersApi.list).toHaveBeenCalledTimes(2);
    expect(providersApi.settings).toHaveBeenCalledOnce();
    await click('Save changes');
    expect(container.textContent).toContain('Save failed');
    expect(container.textContent).toContain('Unsaved changes');
    await click('Runtime');
    expect(allowance().value).toBe('3000');
    await click('Save changes');
    expect(save).toHaveBeenLastCalledWith(expect.objectContaining({ agent_context_response_reserve_tokens: 3000 }));
    expect(container.textContent).not.toContain('Unsaved changes');
    expect(button('Save changes').disabled).toBe(true);
  });

  it('does not discard edits made while an earlier settings save is pending', async () => {
    let finish!: (value: Settings) => void;
    vi.spyOn(providersApi, 'updateSettings').mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    await act(async () => root.render(<SettingsPage />));
    await click('Runtime');
    await editAllowance('3000');
    await click('Save changes');
    await editAllowance('4000');
    await act(async () => finish({ ...settings, agent_context_response_reserve_tokens: 3000 }));
    expect(container.textContent).toContain('Unsaved changes');
    expect(button('Save changes').disabled).toBe(false);
    expect(allowance().value).toBe('4000');
  });

  it('describes the actual restricted OCR fallback precedence rather than promising overrides', async () => {
    vi.mocked(providersApi.settings).mockResolvedValue({
      ...settings,
      ocr_llm_enhancement_enabled: true,
      default_model_references: { vision: { provider_profile_id: 'remote', model: 'vision' } },
    });
    await act(async () => root.render(<SettingsPage />));
    await click('Documents');
    expect(container.textContent).toContain('Legacy enhancement fallback');
    expect(container.textContent).toContain('fallback will not be used');
    expect(container.textContent).toContain('Ollama triage model (same provider)');
    expect(container.textContent).toContain('Other providers use the enhancement model');
    expect(container.textContent).not.toContain('model override');
  });
});
