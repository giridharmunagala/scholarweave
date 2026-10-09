// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ProviderProfilesPanel } from './ProviderProfilesPanel';
import { providersApi, type Provider } from './api';

const localProvider: Provider = {
  id: 'local',
  name: 'Local models',
  kind: 'openai_compatible',
  base_url: 'http://127.0.0.1:8080/v1',
  api_key_set: false,
  api_mode: 'chat_completions',
  models: [],
  state: 'active',
  serialize_model_switches: true,
  created_at: '2026-09-06T00:00:00Z',
  updated_at: '2026-09-06T00:00:00Z',
};

describe('application-wide inference scheduling', () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;
  const onRefresh = vi.fn(async () => {});
  const onError = vi.fn();

  beforeEach(() => {
    (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement('div');
    root = createRoot(container);
    vi.stubGlobal('fetch', vi.fn());
    onRefresh.mockClear();
    onError.mockClear();
  });

  afterEach(async () => {
    await act(async () => { root.unmount(); });
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  async function render(providers: Provider[] = []) {
    await act(async () => {
      root.render(
        <ProviderProfilesPanel providers={providers} onRefresh={onRefresh} onError={onError} />,
      );
    });
  }

  async function clickButton(text: string) {
    const button = Array.from(container.querySelectorAll('button'))
      .find((entry) => entry.textContent?.includes(text))!;
    await act(async () => { button.click(); });
  }

  it('explains the global lane regardless of legacy provider settings', async () => {
    await render([
      localProvider,
      { ...localProvider, id: 'remote', name: 'Remote', serialize_model_switches: false },
    ]);
    expect(container.querySelector('input[type="checkbox"]')).toBeNull();
    expect(container.textContent).toContain('One LLM call at a time across all providers');
    expect(container.textContent).toContain('Queued chat calls get priority between responses');
    expect(container.textContent).toContain('regular turns for background work');
    expect(container.textContent).toContain('Your model server manages hot swaps');
    expect(container.textContent).not.toContain('same-model calls may run concurrently');
    expect(fetch).not.toHaveBeenCalled();
  });

  it('does not offer a concurrency override when creating providers', async () => {
    const create = vi.spyOn(providersApi, 'create').mockResolvedValue(localProvider);
    await render();
    await clickButton('Add provider');
    expect(container.textContent).toContain('One LLM call at a time across all providers');
    expect(container.textContent).not.toContain('Allow different models concurrently');
    await clickButton('Create profile');
    expect(create).toHaveBeenCalledOnce();
    expect(create.mock.calls[0][0]).not.toHaveProperty('serialize_model_switches');
    expect(onRefresh).toHaveBeenCalledOnce();
  });

  it('saves and clears provider reasoning overrides without changing models', async () => {
    const update = vi.spyOn(providersApi, 'update').mockResolvedValue(localProvider);
    await render([{ ...localProvider, default_reasoning_effort: 'high', tool_reasoning_effort: 'none' }]);
    const selects = container.querySelectorAll('details select');
    expect((selects[0] as HTMLSelectElement).value).toBe('high');
    expect((selects[1] as HTMLSelectElement).value).toBe('none');
    await act(async () => {
      (selects[1] as HTMLSelectElement).value = '';
      selects[1].dispatchEvent(new Event('change', { bubbles: true }));
    });
    expect(update).toHaveBeenLastCalledWith('local', {
      serialize_model_switches: true, tool_reasoning_effort: null,
    });
    await act(async () => {
      (selects[0] as HTMLSelectElement).value = 'medium';
      selects[0].dispatchEvent(new Event('change', { bubbles: true }));
    });
    expect(update).toHaveBeenLastCalledWith('local', {
      serialize_model_switches: true, default_reasoning_effort: 'medium',
    });
    expect(onRefresh).toHaveBeenCalledTimes(2);
    expect(container.textContent).toContain('it does not enable unsupported reasoning plus tools');
  });

  it('switches the generation endpoint explicitly without changing reasoning or models', async () => {
    const update = vi.spyOn(providersApi, 'update').mockResolvedValue(localProvider);
    await render([localProvider]);
    const select = container.querySelector('[aria-label="Generation API for Local models"]') as HTMLSelectElement;
    expect(select.value).toBe('chat_completions');
    await act(async () => {
      select.value = 'responses';
      select.dispatchEvent(new Event('change', { bubbles: true }));
    });
    expect(update).toHaveBeenLastCalledWith('local', {
      api_mode: 'responses', serialize_model_switches: true,
    });
    expect(onRefresh).toHaveBeenCalledOnce();
    expect(container.textContent).toContain('not hosted search or code execution');
  });

  it('does not offer Responses for Ollama', async () => {
    await render([{ ...localProvider, kind: 'ollama' }]);
    const select = container.querySelector('[aria-label="Generation API for Local models"]') as HTMLSelectElement;
    expect(select.disabled).toBe(true);
    expect(select.value).toBe('chat_completions');
  });

  it('surfaces errors saving reasoning overrides', async () => {
    const error = new Error('Could not save');
    vi.spyOn(providersApi, 'update').mockRejectedValue(error);
    await render([localProvider]);
    const select = container.querySelector('details select') as HTMLSelectElement;
    await act(async () => {
      select.value = 'high';
      select.dispatchEvent(new Event('change', { bubbles: true }));
    });
    expect(onError).toHaveBeenCalledWith(error);
    expect(onRefresh).not.toHaveBeenCalled();
    expect(select.disabled).toBe(false);
  });
});
