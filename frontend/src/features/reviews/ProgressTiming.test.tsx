// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ProgressTiming } from './ProgressTiming';

let container: HTMLDivElement;
let root: Root;
let time: number;

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  vi.useFakeTimers();
  time = 0;
  vi.spyOn(performance, 'now').mockImplementation(() => time);
  container = document.createElement('div');
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  vi.restoreAllMocks();
  vi.useRealTimers();
});

async function render(count: number, running = true, total = 20) {
  await act(async () => root.render(<ProgressTiming count={count} total={total} running={running} />));
}

async function advance(seconds: number) {
  time += seconds * 1000;
  await act(async () => vi.advanceTimersByTime(seconds * 1000));
}

it('measures new completions, not historical counts, and updates ETA during waits', async () => {
  await render(10);
  expect(container.textContent).toContain('Measuring speed');
  await advance(60);
  await render(12);
  expect(container.textContent).toContain('2.0 papers/min (0.03 papers/s)');
  expect(container.textContent).toContain('~4 min remaining');
  await advance(60);
  expect(container.textContent).toContain('1.0 papers/min');
  expect(container.textContent).toContain('~8 min remaining');
  expect(container.textContent).toContain('not model-request RPM/RPS');
});

it('resets on pause, resume, and counter rollback instead of including inactive time', async () => {
  await render(0);
  await advance(60);
  await render(2);
  await render(2, false);
  expect(container.textContent).toContain('Not running');
  await advance(600);
  await render(2);
  expect(container.textContent).toContain('Measuring speed');
  await advance(60);
  await render(4);
  expect(container.textContent).toContain('2.0 papers/min');
  await render(1);
  expect(container.textContent).toContain('Measuring speed');
});

it('marks completed tasks without forecasting more work and handles growing totals', async () => {
  await render(20);
  expect(container.textContent).toBe('Complete');
  await render(20, true, 25);
  expect(container.textContent).toContain('Measuring speed');
  await advance(30);
  await render(21, true, 25);
  expect(container.textContent).toContain('~2 min remaining');
});
