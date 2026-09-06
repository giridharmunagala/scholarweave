// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { resolveModelReference } from './ChatModelPicker';
import type { ModelReference } from './api';
import {
  readStoredReasoningEffort,
  ReasoningEffortSelect,
  storeReasoningEffort,
  useModelReasoningEffort,
  type ReasoningEffort,
} from './ReasoningEffortSelect';

const first = { provider_profile_id: 'local', model: 'first' };
const second = { provider_profile_id: 'local', model: 'second' };
const otherProvider = { provider_profile_id: 'remote', model: 'first' };

describe('model-scoped reasoning selection', () => {
  let container: HTMLDivElement;
  let root: Root;
  let submitted: ReasoningEffort | null;

  function Composer({
    model,
    supported,
  }: {
    model: ModelReference;
    supported: ReasoningEffort[] | null;
  }) {
    const [effort, select] = useModelReasoningEffort(model, supported);
    return <>
      <ReasoningEffortSelect value={effort} supportedEfforts={supported} onChange={select} />
      <button onClick={() => { submitted = effort; }}>Send</button>
    </>;
  }

  const render = async (model: ModelReference, supported: ReasoningEffort[] | null) => {
    await act(async () => root.render(<Composer model={model} supported={supported} />));
  };
  const select = async (effort: ReasoningEffort | '') => {
    await act(async () => {
      const control = container.querySelector('select')!;
      control.value = effort;
      control.dispatchEvent(new Event('change', { bubbles: true }));
    });
  };
  const send = async () => {
    await act(async () => container.querySelector('button')!.click());
    return submitted;
  };

  beforeEach(() => {
    (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
    localStorage.clear();
    container = document.createElement('div');
    root = createRoot(container);
    submitted = null;
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it('does not transfer effort between models even when both support it', async () => {
    await render(first, ['none', 'high']);
    await select('high');
    expect(await send()).toBe('high');
    await render(second, ['none', 'high']);
    expect(container.querySelector('select')!.value).toBe('');
    expect(await send()).toBeNull();
    await select('none');
    await render(first, ['none', 'high']);
    expect(await send()).toBe('high');
    await render(second, ['none', 'high']);
    expect(await send()).toBe('none');
    expect(readStoredReasoningEffort(first)).toBe('high');
    expect(readStoredReasoningEffort(second)).toBe('none');
  });

  it('never submits stale effort for unsupported or unconfigured models', async () => {
    storeReasoningEffort('high', first);
    await render(first, ['none', 'high']);
    expect(await send()).toBe('high');
    await render(second, ['low', 'medium']);
    expect(await send()).toBeNull();
    await render(second, null);
    expect(container.querySelector('select')!.disabled).toBe(true);
    expect(await send()).toBeNull();
    await render(first, []);
    expect(await send()).toBeNull();
    await render(first, ['none', 'high']);
    expect(await send()).toBe('high');
  });

  it('keeps provider identities separate and clears only the selected model', async () => {
    storeReasoningEffort('high', first);
    storeReasoningEffort('low', otherProvider);
    await render(otherProvider, ['low', 'high']);
    expect(await send()).toBe('low');
    await select('');
    expect(readStoredReasoningEffort(otherProvider)).toBeNull();
    expect(readStoredReasoningEffort(first)).toBe('high');
  });

  it('restores persisted effort after model capabilities load, including workspace defaults', async () => {
    storeReasoningEffort('high', first);
    await render({}, null);
    expect(await send()).toBeNull();
    const resolved = resolveModelReference({}, { default_model_references: { chat: first } });
    await render(resolved, null);
    expect(await send()).toBeNull();
    await render(resolved, ['high']);
    expect(await send()).toBe('high');
    await act(async () => root.unmount());
    root = createRoot(container);
    await render(resolved, ['high']);
    expect(await send()).toBe('high');
  });

  it('ignores unscoped legacy storage and corrupt or unsupported saved efforts', async () => {
    localStorage.setItem('scholarweave-reasoning-effort', 'high');
    await render(first, ['none', 'high']);
    expect(await send()).toBeNull();
    const key = `scholarweave-reasoning-effort:${JSON.stringify(['local', 'first'])}`;
    localStorage.setItem(key, 'off');
    await render(first, ['none', 'high']);
    expect(await send()).toBeNull();
    localStorage.setItem(key, 'xhigh');
    await render(first, ['none', 'high']);
    expect(await send()).toBeNull();
  });

  it('retains per-model choices in memory when browser storage is unavailable', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('Unavailable'); });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('Unavailable'); });
    await render(first, ['none', 'high']);
    await select('high');
    expect(await send()).toBe('high');
    await render(second, ['none', 'high']);
    expect(await send()).toBeNull();
    await render(first, ['none', 'high']);
    expect(await send()).toBe('high');
  });
});
