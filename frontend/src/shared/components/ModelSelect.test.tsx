// @vitest-environment jsdom
import { act, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ModelSelect, type ModelOption, type ModelSelectValue } from './ModelSelect';

const options: ModelOption[] = ['Alpha', 'Beta', 'Gamma'].map((model) => ({
  providerId: 'local', providerName: 'Local', model, capabilities: ['chat'],
}));

describe('model picker selection and keyboard continuity', () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;
  const onChange = vi.fn();

  beforeEach(() => {
    Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    onChange.mockReset();
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  async function render(value: ModelSelectValue = {}, models = options) {
    await act(async () => root.render(
      <ModelSelect options={models} value={value} onChange={onChange} emptyOptionLabel="Not configured" />,
    ));
  }
  const trigger = () => container.querySelector<HTMLButtonElement>('.model-select-trigger')!;
  const search = () => container.querySelector<HTMLInputElement>('[role="combobox"]')!;
  async function key(element: HTMLElement, name: string) {
    await act(async () => element.dispatchEvent(new KeyboardEvent('keydown', { key: name, bubbles: true })));
  }
  async function query(value: string) {
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(search(), value);
      search().dispatchEvent(new Event('input', { bubbles: true }));
    });
  }

  it('reopens a filtered selection on its identity instead of clearing the default with Enter', async () => {
    function Picker() {
      const [value, setValue] = useState<ModelSelectValue>({});
      return <ModelSelect options={options} value={value} emptyOptionLabel="Not configured" onChange={(next) => {
        setValue(next);
        onChange(next);
      }} />;
    }
    await act(async () => root.render(<Picker />));
    await act(async () => trigger().click());
    await query('Beta');
    await key(search(), 'Enter');
    expect(onChange).toHaveBeenLastCalledWith({ provider_profile_id: 'local', model: 'Beta' });
    expect(document.activeElement).toBe(trigger());
    await act(async () => trigger().click());
    expect(search().value).toBe('');
    expect(container.querySelector('[data-active="true"]')?.textContent).toContain('Beta');
    expect(search().getAttribute('aria-activedescendant')).toBe(container.querySelector('[data-active="true"]')?.id);
    await key(search(), 'Enter');
    expect(onChange).toHaveBeenLastCalledWith({ provider_profile_id: 'local', model: 'Beta' });
  });

  it('restores focus on Escape and crosses model rows with arrows', async () => {
    await render({ provider_profile_id: 'local', model: 'Beta' });
    await key(trigger(), 'ArrowDown');
    expect(document.activeElement).toBe(search());
    await key(search(), 'ArrowUp');
    expect(container.querySelector('[data-active="true"]')?.textContent).toContain('Alpha');
    await key(search(), 'Escape');
    expect(document.activeElement).toBe(trigger());
    expect(container.querySelector('[role="listbox"]')).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
  });

  it('never commits an absent row or silently clears an unavailable selection', async () => {
    await render({ provider_profile_id: 'local', model: 'Unavailable' });
    await act(async () => trigger().click());
    await key(search(), 'Enter');
    expect(onChange).not.toHaveBeenCalled();
    await query('not found');
    await key(search(), 'Enter');
    expect(onChange).not.toHaveBeenCalled();
    await query('');
    await key(search(), 'ArrowDown');
    await key(search(), 'Enter');
    expect(onChange).toHaveBeenLastCalledWith({ provider_profile_id: 'local', model: 'Alpha' });
  });

  it('retains the active model when option objects are refreshed or reordered', async () => {
    const value = { provider_profile_id: 'local', model: 'Beta' };
    await render(value);
    await act(async () => trigger().click());
    await render(value, [...options].reverse().map((option) => ({ ...option })));
    await key(search(), 'Enter');
    expect(onChange).toHaveBeenLastCalledWith(value);
  });

  it('closes on Tab without changing the selection', async () => {
    await render();
    await act(async () => trigger().click());
    await key(search(), 'Tab');
    expect(container.querySelector('[role="listbox"]')).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
  });

  it('moves through models in displayed provider-group order', async () => {
    await render({ provider_profile_id: 'local', model: 'Alpha' }, [
      options[0],
      { ...options[2], providerId: 'other', providerName: 'Other' },
      options[1],
    ]);
    await act(async () => trigger().click());
    await key(search(), 'ArrowDown');
    expect(container.querySelector('[data-active="true"]')?.textContent).toContain('Beta');
    await key(search(), 'ArrowDown');
    expect(container.querySelector('[data-active="true"]')?.textContent).toContain('Gamma');
  });
});
