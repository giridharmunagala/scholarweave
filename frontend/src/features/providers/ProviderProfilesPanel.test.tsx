// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ProviderProfilesPanel } from './ProviderProfilesPanel';
import { providersApi, type Provider, type ProviderVerification } from './api';

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
const chatModel: Provider['models'][number] = {
  name: 'chat-1', capabilities: ['chat', 'tools'], enabled: true,
  context_window_tokens: 32768, preserve_thinking: false, reasoning_efforts: null,
};

describe('application-wide inference scheduling', () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;
  const onRefresh = vi.fn(async () => {});
  const onError = vi.fn();

  beforeEach(() => {
    (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    vi.stubGlobal('fetch', vi.fn());
    onRefresh.mockClear();
    onError.mockClear();
  });

  afterEach(async () => {
    await act(async () => { root.unmount(); });
    container.remove();
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
  async function clickDialogButton(text: string) {
    const button = [...document.querySelectorAll<HTMLButtonElement>('[role="dialog"] button')]
      .find((entry) => entry.textContent?.trim() === text)!;
    await act(async () => button.click());
  }
  async function inputValue(input: HTMLInputElement, value: string) {
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value);
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
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

  async function changeEndpoint(value: string) {
    const input = container.querySelector('[aria-label="Base URL for Local models"]') as HTMLInputElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value);
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
  }

  it.each(['openai_compatible', 'ollama'] as const)('edits an existing %s endpoint without replacing saved configuration', async (kind) => {
    const update = vi.spyOn(providersApi, 'update').mockResolvedValue(localProvider);
    await render([{ ...localProvider, kind }]);
    await clickButton('Edit URL');
    expect((container.querySelector('input[type="url"]') as HTMLInputElement).value).toBe(localProvider.base_url);
    await changeEndpoint('http://127.0.0.1:9090/v1');
    await clickButton('Save URL');
    expect(update).toHaveBeenCalledExactlyOnceWith('local', {
      base_url: 'http://127.0.0.1:9090/v1', serialize_model_switches: true,
    });
    expect(onRefresh).toHaveBeenCalledOnce();
    expect(container.querySelector('input[type="url"]')).toBeNull();
  });

  it('cancels endpoint changes without saving', async () => {
    const update = vi.spyOn(providersApi, 'update');
    await render([localProvider]);
    await clickButton('Edit URL');
    await changeEndpoint('http://127.0.0.1:9090/v1');
    await clickButton('Cancel');
    expect(update).not.toHaveBeenCalled();
    await clickButton('Edit URL');
    expect((container.querySelector('input[type="url"]') as HTMLInputElement).value).toBe(localProvider.base_url);
  });

  it('keeps the endpoint draft and surfaces save failures', async () => {
    const error = new Error('Could not save URL');
    vi.spyOn(providersApi, 'update').mockRejectedValue(error);
    await render([localProvider]);
    await clickButton('Edit URL');
    await changeEndpoint('http://127.0.0.1:9090/v1');
    await clickButton('Save URL');
    expect(onError).toHaveBeenCalledWith(error);
    expect(onRefresh).not.toHaveBeenCalled();
    const input = container.querySelector('input[type="url"]') as HTMLInputElement;
    expect(input.value).toBe('http://127.0.0.1:9090/v1');
    expect(input.disabled).toBe(false);
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

  it('requires archive confirmation, warns about references, and offers identity-preserving restoration', async () => {
    const archive = vi.spyOn(providersApi, 'archive').mockResolvedValue({ ...localProvider, state: 'archived' });
    const restore = vi.spyOn(providersApi, 'restore').mockResolvedValue(localProvider);
    await act(async () => root.render(
      <ProviderProfilesPanel
        providers={[localProvider]}
        defaultReferences={[{ chat: { provider_profile_id: 'local', model: 'chat-1' } }]}
        onRefresh={onRefresh}
        onError={onError}
      />,
    ));
    await clickButton('Archive');
    expect(archive).not.toHaveBeenCalled();
    expect(container.querySelector('[role="alertdialog"]')?.textContent).toContain('defaults: chat');
    expect(container.textContent).toContain('Defaults and history will not be cleared');
    await clickButton('Cancel archive');
    expect(container.querySelector('[role="alertdialog"]')).toBeNull();
    await clickButton('Archive');
    await clickButton('Confirm archive');
    expect(archive).toHaveBeenCalledExactlyOnceWith('local');
    await render([{ ...localProvider, state: 'archived' }]);
    expect(container.textContent).toContain('Archived providers');
    expect(container.textContent).not.toContain('Manage models');
    await clickButton('Restore Local models');
    expect(restore).toHaveBeenCalledExactlyOnceWith('local');
  });

  it('allows manual registration without discovery, preserving configured models', async () => {
    const discover = vi.spyOn(providersApi, 'discover').mockResolvedValue({ models: [], discovery_error: 'Model listing is not supported' });
    const update = vi.spyOn(providersApi, 'update').mockResolvedValue(localProvider);
    await render([localProvider]);
    await clickButton('Discover models');
    expect(discover).toHaveBeenCalledOnce();
    expect(onError).toHaveBeenCalledWith(expect.objectContaining({ message: 'Model listing is not supported' }));
    await clickButton('Manage models');
    const input = document.querySelector<HTMLInputElement>('.provider-model-manual input:not([type="checkbox"])')!;
    await inputValue(input, 'deployment-custom');
    await clickDialogButton('Register model');
    expect(update).toHaveBeenCalledExactlyOnceWith('local', {
      serialize_model_switches: true,
      models: [{ name: 'deployment-custom', capabilities: ['chat'], enabled: true, preserve_thinking: false }],
    });
    expect(onRefresh).toHaveBeenCalledOnce();
    expect(input.value).toBe('');
    expect(document.querySelector('[role="dialog"]')?.textContent).toContain('Capability choices declare intended use');
  });

  it('retains manual model input on failure and rejects duplicate registrations', async () => {
    const update = vi.spyOn(providersApi, 'update').mockRejectedValue(new Error('Registration failed'));
    await render([{ ...localProvider, models: [chatModel] }]);
    await clickButton('Manage models');
    const input = document.querySelector<HTMLInputElement>('.provider-model-manual input:not([type="checkbox"])')!;
    await inputValue(input, 'chat-1');
    await clickDialogButton('Register model');
    expect(update).not.toHaveBeenCalled();
    expect(document.querySelector('.provider-model-manual [role="alert"]')?.textContent).toContain('already configured');
    await inputValue(input, 'new-chat');
    await clickDialogButton('Register model');
    expect(input.value).toBe('new-chat');
    expect(document.querySelector('.provider-model-manual [role="alert"]')?.textContent).toContain('Registration failed');
    expect(update.mock.calls[0][1].models?.[0]).toEqual(chatModel);
  });

  it.each(['100', '4096.5', 'invalid', '2000001'])('validates invalid context %s without showing it as saved', async (value) => {
    const update = vi.spyOn(providersApi, 'update');
    await render([{ ...localProvider, models: [chatModel] }]);
    await clickButton('Manage models');
    const input = document.querySelector<HTMLInputElement>('[aria-label="chat-1 context window tokens"]')!;
    await inputValue(input, value);
    await act(async () => input.dispatchEvent(new FocusEvent('focusout', { bubbles: true })));
    expect(input.getAttribute('aria-invalid')).toBe('true');
    expect(document.getElementById(input.getAttribute('aria-describedby')!)?.textContent).toContain('4096 to 2000000');
    expect(update).not.toHaveBeenCalled();
    await clickDialogButton('Revert context');
    expect(input.value).toBe('32768');
    expect(input.getAttribute('aria-invalid')).toBe('false');
    expect(document.getElementById(input.getAttribute('aria-describedby')!)?.textContent).toBe('Saved context');
  });

  it('retains a failed context draft, supports retry, and accepts clearing to fallback', async () => {
    let current = { ...localProvider, models: [chatModel] };
    const update = vi.spyOn(providersApi, 'update')
      .mockRejectedValueOnce(new Error('Could not save context'))
      .mockImplementation(async (_, payload) => {
        current = { ...current, models: payload.models! as Provider['models'] };
        return current;
      });
    onRefresh.mockImplementationOnce(async () => {
      root.render(<ProviderProfilesPanel providers={[current]} onRefresh={onRefresh} onError={onError} />);
    });
    await render([current]);
    await clickButton('Manage models');
    const input = document.querySelector<HTMLInputElement>('[aria-label="chat-1 context window tokens"]')!;
    await inputValue(input, '65536');
    expect(document.getElementById(input.getAttribute('aria-describedby')!)?.textContent).toContain('Unsaved context');
    await act(async () => input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })));
    expect(input.value).toBe('65536');
    expect(document.getElementById(input.getAttribute('aria-describedby')!)?.textContent).toContain('could not be confirmed');
    await inputValue(input, '');
    await act(async () => input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })));
    expect(update).toHaveBeenLastCalledWith('local', expect.objectContaining({
      models: [expect.objectContaining({ context_window_tokens: null })],
    }));
    expect(document.getElementById(input.getAttribute('aria-describedby')!)?.textContent).toBe('Saved context');
  });

  it('permits the smaller decision-only context minimum', async () => {
    const model = { ...chatModel, capabilities: ['decision'] as const };
    vi.spyOn(providersApi, 'update').mockResolvedValue(localProvider);
    await render([{ ...localProvider, models: [{ ...model, capabilities: ['decision'] }] }]);
    await clickButton('Manage models');
    const input = document.querySelector<HTMLInputElement>('[aria-label="chat-1 context window tokens"]')!;
    await inputValue(input, '512');
    expect(input.getAttribute('aria-invalid')).toBe('false');
    await act(async () => input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })));
    expect(providersApi.update).toHaveBeenCalledWith('local', expect.objectContaining({
      models: [expect.objectContaining({ context_window_tokens: 512 })],
    }));
  });

  it('shows verification pending state, capability failures, and detail instead of reachable implying tools', async () => {
    let finish!: (result: ProviderVerification) => void;
    vi.spyOn(providersApi, 'verify').mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    await render([{ ...localProvider, models: [chatModel] }]);
    await clickButton('Manage models');
    await clickDialogButton('Verify');
    expect(document.querySelector('[role="dialog"]')?.textContent).toContain('Verifying…');
    expect(document.querySelector('[role="dialog"]')?.textContent).toContain('Checking chat-1 capabilities');
    await act(async () => finish({
      provider: 'openai_compatible', base_url: localProvider.base_url, model: 'chat-1',
      reachable: true, tool_calling: false, detail: 'Provider answered but ignored the tool: done',
    }));
    expect(document.querySelector('.provider-verification')?.textContent).toContain('tool calling not verified');
    expect(document.querySelector('.provider-verification')?.textContent).toContain('ignored the tool');
    expect(document.querySelector('.provider-verification')?.textContent).not.toContain('reachable');
  });

  it('shows verification transport errors inline and clears stale results before retry', async () => {
    vi.spyOn(providersApi, 'verify').mockResolvedValueOnce({
      provider: 'openai_compatible', base_url: localProvider.base_url, model: 'chat-1',
      reachable: true, tool_calling: true, detail: 'Provider answered and called the test tool.',
    }).mockRejectedValueOnce(new Error('Endpoint offline'));
    await render([{ ...localProvider, models: [chatModel] }]);
    await clickButton('Manage models');
    await clickDialogButton('Verify');
    expect(document.querySelector('.provider-verification')?.textContent).toContain('Response and tool calling verified');
    await clickDialogButton('Verify');
    expect(document.querySelector('.provider-verification')?.textContent).toContain('Endpoint offline');
    expect(document.querySelector('[role="dialog"]')?.textContent).not.toContain('Response and tool calling verified');
  });

  it.each(['embedding', 'decision'] as const)('does not present a verified %s model as a tool calling test', async (capability) => {
    vi.spyOn(providersApi, 'verify').mockResolvedValue({
      provider: 'openai_compatible', base_url: localProvider.base_url, model: 'chat-1',
      reachable: true, tool_calling: false, detail: `${capability} response received`,
    });
    await render([{ ...localProvider, models: [{ ...chatModel, capabilities: [capability] }] }]);
    await clickButton('Manage models');
    await clickDialogButton('Verify');
    expect(document.querySelector('.provider-verification')?.textContent).toContain('tools not tested');
    expect(document.querySelector('.provider-verification')?.textContent).toContain(`${capability} response received`);
  });
});
