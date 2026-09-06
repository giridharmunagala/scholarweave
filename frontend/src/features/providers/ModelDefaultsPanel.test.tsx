// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { describe, expect, it, vi } from 'vitest';
import { ModelDefaultsPanel, capabilityOptions } from './ModelDefaultsPanel';
import type { Provider, Settings } from './api';

const providers = [{
  id: 'local',
  name: 'Local models',
  kind: 'openai_compatible',
  models: [
    { name: 'gemma4-12b', capabilities: ['chat'], enabled: true },
    { name: 'unknown', capabilities: [], enabled: true },
    { name: 'disabled', capabilities: ['chat'], enabled: false },
    { name: 'embed', capabilities: ['embedding'], enabled: true },
  ],
}] as Provider[];

describe('context maintenance default', () => {
  it('offers enabled chat models without a separate model capability flag', () => {
    expect(capabilityOptions(providers, 'compaction').map((option) => option.model))
      .toEqual(['gemma4-12b', 'unknown']);
  });

  it('is optional and can select and clear a helper without changing the chat default', async () => {
    (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
    const scroll = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollIntoView');
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() });
    const chat = { provider_profile_id: 'local', model: 'main' };
    let settings = { default_model_references: { chat } } as unknown as Settings;
    const container = document.createElement('div');
    const root = createRoot(container);
    const render = () => root.render(<ModelDefaultsPanel
      settings={settings}
      providers={providers}
      onChange={(next) => { settings = next; render(); }}
    />);
    try {
      await act(async () => { render(); });
      const card = () => Array.from(container.querySelectorAll('.default-model-card'))
        .find((entry) => entry.textContent?.includes('Context maintenance'))!;
      expect(card().textContent).toContain('Use main agent model');
      expect(settings.default_model_references.compaction).toBeUndefined();
      await act(async () => { card().querySelector<HTMLButtonElement>('button')!.click(); });
      const option = Array.from(card().querySelectorAll<HTMLElement>('[role="option"]'))
        .find((entry) => entry.textContent?.includes('gemma4-12b'))!;
      await act(async () => { option.click(); });
      expect(settings.default_model_references).toEqual({
        chat, compaction: { provider_profile_id: 'local', model: 'gemma4-12b' },
      });
      await act(async () => { card().querySelector<HTMLButtonElement>('button')!.click(); });
      const clear = Array.from(card().querySelectorAll<HTMLElement>('[role="option"]'))
        .find((entry) => entry.textContent?.includes('Use main agent model'))!;
      await act(async () => { clear.click(); });
      expect(settings.default_model_references).toEqual({ chat });
    } finally {
      await act(async () => { root.unmount(); });
      if (scroll) Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', scroll);
      else Reflect.deleteProperty(HTMLElement.prototype, 'scrollIntoView');
    }
  });
});
