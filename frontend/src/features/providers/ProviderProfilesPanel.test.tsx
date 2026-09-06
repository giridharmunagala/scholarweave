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
});
