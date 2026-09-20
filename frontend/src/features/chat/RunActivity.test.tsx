// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { expect, it, vi } from 'vitest';
import { RunActivity } from './RunActivity';
import type { RunStage } from './runStages';

it('ticks live stages, freezes settled durations and retains tasks and failures in long turns', () => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  vi.useFakeTimers();
  vi.setSystemTime(10_000);
  const container = document.createElement('div');
  const root = createRoot(container);
  const open = vi.fn();
  const stages: RunStage[] = Array.from({ length: 12 }, (_, index) => ({
    id: String(index), label: `Stage ${index}`, status: index === 0 ? 'failed' : 'completed',
    startedAt: 0, finishedAt: 2000,
  }));
  stages.push({ id: 'live', label: 'Writing the answer', status: 'running', startedAt: 9000, finishedAt: null });
  const workPlan = [{ id: 'task', runId: 'r', title: 'Read sources', status: 'completed' as const, notes: null }];
  try {
    act(() => root.render(<RunActivity stages={stages} workPlan={workPlan} onOpenActivity={open} />));
    expect(container.textContent).toContain('Tasks 1/1 complete');
    expect(container.textContent).toContain('5 earlier completed stages');
    expect(container.textContent).toContain('Stage 0 failed');
    expect(container.querySelector('.status-running time')?.textContent).toBe('1s');
    act(() => vi.advanceTimersByTime(2000));
    expect(container.querySelector('.status-running time')?.textContent).toBe('3s');
    expect(container.querySelector('.status-failed time')?.textContent).toBe('2s');
    act(() => container.querySelector('button')!.click());
    expect(open).toHaveBeenCalledOnce();
    act(() => root.render(<RunActivity stages={stages.map((stage) => stage.status === 'running'
      ? { ...stage, status: 'completed', finishedAt: 12_000 } : stage)} onOpenActivity={open} />));
    act(() => vi.advanceTimersByTime(3000));
    expect(container.querySelector('li:last-child time')?.textContent).toBe('3s');
  } finally {
    act(() => root.unmount());
    vi.useRealTimers();
  }
});
