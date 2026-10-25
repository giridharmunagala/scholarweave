// @vitest-environment jsdom
import { act, StrictMode, useState } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it } from 'vitest';
import { SettingsDrawer } from './SettingsDrawer';

let container: HTMLDivElement;
let root: Root;

function Example() {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState('');
  return <>
    <button aria-haspopup="dialog" aria-controls="example-settings" aria-expanded={open} onClick={() => setOpen(true)}>Settings</button>
    <SettingsDrawer id="example-settings" title="Example settings" open={open} onClose={() => setOpen(false)}>
      <label>Draft<input value={draft} onChange={(event) => setDraft(event.target.value)} /></label>
    </SettingsDrawer>
  </>;
}

beforeEach(async () => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  document.body.style.overflow = 'auto';
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => root.render(<StrictMode><Example /></StrictMode>));
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  document.body.style.overflow = '';
});

it.each(['close', 'Escape', 'backdrop'])('preserves drafts and restores focus and scrolling after %s', async (method) => {
  const trigger = container.querySelector('button')!;
  const dialog = container.querySelector('dialog')!;
  expect(dialog.open).toBe(false);
  trigger.focus();
  await act(async () => trigger.click());
  expect(dialog.open).toBe(true);
  expect(trigger.getAttribute('aria-expanded')).toBe('true');
  expect(document.body.style.overflow).toBe('hidden');
  const field = dialog.querySelector('input')!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(field, 'Keep this draft');
    field.dispatchEvent(new Event('input', { bubbles: true }));
    dialog.querySelector('h2')!.click();
  });
  expect(dialog.open).toBe(true);
  await act(async () => {
    if (method === 'close') dialog.querySelector('button')!.click();
    else if (method === 'Escape') dialog.dispatchEvent(new Event('cancel', { cancelable: true }));
    else dialog.click();
  });
  expect(dialog.open).toBe(false);
  expect(document.activeElement).toBe(trigger);
  expect(document.body.style.overflow).toBe('auto');
  await act(async () => trigger.click());
  expect(dialog.querySelector('input')).toBe(field);
  expect(field.value).toBe('Keep this draft');
});

it('releases the scroll lock when an open drawer unmounts', async () => {
  await act(async () => container.querySelector('button')!.click());
  expect(document.body.style.overflow).toBe('hidden');
  await act(async () => root.render(<div>Next page</div>));
  expect(document.body.style.overflow).toBe('auto');
});
