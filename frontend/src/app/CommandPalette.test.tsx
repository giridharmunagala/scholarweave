// @vitest-environment jsdom
import { act, useState } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { CommandPalette } from './CommandPalette';
import { RouterProvider, useLocation, useNavigationGuard } from './router';

let container: HTMLDivElement;
let root: Root;
let responses: Record<string, unknown>;
let failures: Set<string>;
let guard = false;
const onAsk = vi.fn();
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });

function Host() {
  const [open, setOpen] = useState(false);
  const { pathname, search } = useLocation();
  useNavigationGuard(guard, 'Discard draft?');
  return <div>
    <button onClick={() => setOpen(true)}>Open search</button>
    <main><button>Background action</button><p>{pathname}{search}</p></main>
    {open ? <CommandPalette onClose={() => setOpen(false)} onAsk={onAsk} /> : null}
  </div>;
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  vi.useFakeTimers();
  window.history.replaceState({}, '', '/');
  vi.stubGlobal('scrollTo', vi.fn());
  vi.stubGlobal('confirm', vi.fn(() => false));
  onAsk.mockReset();
  responses = {};
  failures = new Set();
  guard = false;
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    const path = new URL(input, 'http://localhost').pathname;
    return failures.has(path) ? response({ message: 'Service unavailable' }, 503) : response(responses[path] ?? []);
  }));
  container = document.createElement('div');
  document.body.append(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

async function mount() {
  await act(async () => root.render(<RouterProvider><Host /></RouterProvider>));
  const trigger = container.querySelector<HTMLButtonElement>('button')!;
  trigger.focus();
  await act(async () => trigger.click());
  return trigger;
}

async function search(value: string) {
  await act(async () => {
    const input = container.querySelector<HTMLInputElement>('[role="combobox"]')!;
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await act(async () => vi.advanceTimersByTime(200));
}

async function key(element: Element, value: string, shiftKey = false) {
  await act(async () => element.dispatchEvent(new KeyboardEvent('keydown', { key: value, shiftKey, bubbles: true, cancelable: true })));
}

it('searches paper text, notes, reviews and conversation history through existing APIs', async () => {
  responses = {
    '/api/literature-reviews': [{ id: 'r1', title: 'Calibration review', objective: 'Compare calibration' }],
    '/api/agent/conversations': [{ id: 'c1', kind: 'autonomous', title: 'Calibration discussion', last_message_preview: 'Results' }],
    '/api/deep-work/conversations': [{ id: 'c2', kind: 'deep_work', title: 'Calibration investigation', last_message_preview: '' }],
    '/api/documents/search': [{ id: 'p1', title: 'Different title', excerpt: 'Calibration in the source text', page_start: 4 }],
    '/api/workspace/search': [{ path: 'knowledge/experiment.md', name: 'Experiment', excerpt: 'Calibration notes' }],
  };
  await mount();
  await search('calibration');
  expect(container.querySelector('[role="listbox"]')?.textContent).toContain('Different title');
  expect(container.textContent).toContain('Calibration review');
  expect(container.textContent).toContain('Calibration discussion');
  expect(container.textContent).toContain('Calibration investigation');
  expect(container.textContent).toContain('Calibration notes');
  const paper = [...container.querySelectorAll<HTMLButtonElement>('[role="option"]')].find((item) => item.textContent?.includes('Different title'))!;
  await act(async () => paper.click());
  expect(window.location.search).toBe('?paper=p1&page=4');
  expect(container.querySelector('[role="dialog"]')).toBeNull();
});

it('shows source failures, retains successful results and retries without changing the query', async () => {
  failures.add('/api/documents');
  failures.add('/api/workspace/search');
  responses['/api/tracks'] = [{ id: 't1', title: 'Calibration track' }];
  await mount();
  await search('calibration');
  expect(container.querySelector('[role="alert"]')?.textContent).toContain('Paper titles unavailable');
  expect(container.querySelector('[role="alert"]')?.textContent).toContain('Notes search unavailable');
  expect(container.textContent).toContain('Calibration track');
  expect(container.querySelector('[role="status"]')?.textContent).toContain('incomplete');
  failures.clear();
  await act(async () => [...container.querySelectorAll<HTMLButtonElement>('button')].find((button) => button.textContent === 'Retry search')!.click());
  await act(async () => vi.advanceTimersByTime(200));
  expect(container.querySelector('[role="alert"]')).toBeNull();
  expect(container.querySelector<HTMLInputElement>('[role="combobox"]')!.value).toBe('calibration');
  expect(onAsk).not.toHaveBeenCalled();
});

it('opens batch Runs from search without starting a job', async () => {
  await mount();
  await search('batch runs');
  const runs = [...container.querySelectorAll<HTMLButtonElement>('[role="option"]')]
    .find((item) => item.textContent?.includes('Batch runs'))!;
  await act(async () => runs.click());
  expect(window.location.pathname).toBe('/runs');
  expect(container.querySelector('[role="dialog"]')).toBeNull();
  expect(vi.mocked(fetch).mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true);
});

it('isolates modal focus, exposes the active option, closes from a result and restores focus', async () => {
  const trigger = await mount();
  const input = container.querySelector<HTMLInputElement>('[role="combobox"]')!;
  expect(document.activeElement).toBe(input);
  expect(container.querySelector('main')?.hasAttribute('inert')).toBe(true);
  await key(input, 'ArrowDown');
  const active = document.getElementById(input.getAttribute('aria-activedescendant')!)!;
  expect(active.getAttribute('aria-selected')).toBe('true');
  const close = container.querySelector<HTMLButtonElement>('[aria-label="Close search"]')!;
  await key(input, 'Tab', true);
  expect(document.activeElement).toBe(close);
  await key(close, 'Tab');
  expect(document.activeElement).toBe(input);
  active.focus();
  await key(active, 'Escape');
  expect(container.querySelector('[role="dialog"]')).toBeNull();
  expect(document.activeElement).toBe(trigger);
  expect(container.querySelector('main')?.hasAttribute('inert')).toBe(false);
});

it('does not dismiss search when navigation is declined', async () => {
  guard = true;
  await mount();
  const settings = [...container.querySelectorAll<HTMLButtonElement>('[role="option"]')].find((item) => item.textContent === 'Go to Settings')!;
  await act(async () => settings.click());
  expect(window.location.pathname).toBe('/');
  expect(container.querySelector('[role="dialog"]')).not.toBeNull();
});

it('distinguishes empty search from unavailable sources without starting the assistant', async () => {
  await mount();
  await search('no-matching-evidence');
  expect(container.querySelector('[role="status"]')?.textContent).toContain('No matching local items');
  expect(container.querySelector('[role="alert"]')).toBeNull();
  expect(onAsk).not.toHaveBeenCalled();
});

it('ignores late results from an earlier query', async () => {
  let resolveOld: ((value: Response) => void) | undefined;
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    const url = new URL(input, 'http://localhost');
    if (url.pathname === '/api/workspace/search' && url.searchParams.get('query') === 'old') {
      return new Promise<Response>((resolve) => { resolveOld = resolve; });
    }
    return response([]);
  }));
  await mount();
  await search('old');
  expect(container.querySelector('[role="status"]')?.textContent).toContain('Searching');
  await search('new');
  await act(async () => resolveOld!(response([{ path: 'knowledge/old.md', name: 'Stale old result' }])));
  expect(container.textContent).not.toContain('Stale old result');
});

it('preserves the keyboard-selected action when full-text results arrive above it', async () => {
  let finishSearch: ((value: Response) => void) | undefined;
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    if (new URL(input, 'http://localhost').pathname === '/api/documents/search') {
      return new Promise<Response>((resolve) => { finishSearch = resolve; });
    }
    return response([]);
  }));
  await mount();
  await search('settings');
  const input = container.querySelector<HTMLInputElement>('[role="combobox"]')!;
  await key(input, 'ArrowDown');
  await key(input, 'ArrowUp');
  await act(async () => finishSearch!(response([{ id: 'p1', title: 'Experimental settings', excerpt: 'Setup details', page_start: 1 }])));
  expect(document.getElementById(input.getAttribute('aria-activedescendant')!)?.textContent).toBe('Go to Settings');
  await key(input, 'Enter');
  expect(window.location.pathname).toBe('/settings');
});
