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

async function render(count: number, running = true, total = 20, windowSeconds?: number) {
  await act(async () => root.render(<ProgressTiming count={count} total={total} running={running} windowSeconds={windowSeconds} />));
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

it.each([undefined, 120])('resets on pause, resume, and counter rollback with window %s', async (windowSeconds) => {
  const update = (count: number, running = true) => render(count, running, 20, windowSeconds);
  await update(0);
  await advance(60);
  await update(2);
  await update(2, false);
  expect(container.textContent).toContain('Not running');
  await advance(600);
  await update(2);
  expect(container.textContent).toContain('Measuring speed');
  await advance(60);
  await update(4);
  expect(container.textContent).toContain('2.0 papers/min');
  await update(1);
  expect(container.textContent).toContain('Measuring speed');
});

it('uses only completions in the rolling two-minute window and withholds ETA after a stall', async () => {
  await render(10, true, 100, 120);
  await advance(60);
  await render(22, true, 100, 120);
  expect(container.textContent).toContain('12.0 papers/min');
  await advance(60);
  await render(24, true, 100, 120);
  expect(container.textContent).toContain('7.0 papers/min');
  await advance(60);
  await render(28, true, 100, 120);
  expect(container.textContent).toContain('3.0 papers/min');
  expect(container.textContent).toContain('~24 min remaining');
  await advance(120);
  expect(container.textContent).toContain('No papers completed in the last 2 min');
  expect(container.textContent).not.toMatch(/~\d/);
  await render(29, true, 100, 120);
  expect(container.textContent).toContain('0.5 papers/min');
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
