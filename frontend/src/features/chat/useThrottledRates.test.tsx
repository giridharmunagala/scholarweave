// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useThrottledRates } from './useThrottledRates';

describe('visible telemetry rates', () => {
  let root: Root;
  let container: HTMLDivElement;

  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
    container = document.createElement('div');
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  function Probe({ rate, tokens, status = 'running', id = 'run-1' }: {
    rate: number | null; tokens: number; status?: string; id?: string;
  }) {
    const metrics = useThrottledRates(new Map([[id, {
      promptRate: rate, generationRate: rate, tokens,
    }]])).get(id)!;
    return <div key={status}>{JSON.stringify(metrics)}</div>;
  }

  const visible = () => JSON.parse(container.textContent!);
  const advance = (ms: number) => act(() => vi.advanceTimersByTime(ms));

  it('throttles, not debounces, both speeds while every token update remains immediate', () => {
    act(() => root.render(<Probe rate={10} tokens={100} />));
    advance(1000);
    act(() => root.render(<Probe rate={20} tokens={200} />));
    expect(visible()).toEqual({ promptRate: 10, generationRate: 10, tokens: 200 });
    advance(3000);
    act(() => root.render(<Probe rate={30} tokens={300} />));
    advance(999);
    expect(visible().promptRate).toBe(10);
    advance(1);
    expect(visible()).toEqual({ promptRate: 30, generationRate: 30, tokens: 300 });
    advance(1000);
    act(() => root.render(<Probe rate={40} tokens={400} status="completed" />));
    expect(visible().promptRate).toBe(30);
    advance(4000);
    expect(visible().promptRate).toBe(40);
  });

  it('throttles unavailable transitions, keeps runs isolated, and cleans up timers', () => {
    act(() => root.render(<Probe rate={null} tokens={0} />));
    advance(1000);
    act(() => root.render(<Probe rate={20} tokens={200} />));
    expect(visible().promptRate).toBeNull();
    advance(4000);
    expect(visible().promptRate).toBe(20);
    advance(1000);
    act(() => root.render(<Probe rate={null} tokens={300} />));
    expect(visible().promptRate).toBe(20);
    advance(4000);
    expect(visible().promptRate).toBeNull();
    act(() => root.render(<Probe rate={80} tokens={10} id="run-2" />));
    expect(visible().promptRate).toBe(80);
    act(() => root.render(<Probe rate={90} tokens={20} id="run-2" />));
    expect(vi.getTimerCount()).toBeGreaterThan(0);
    act(() => root.render(null));
    expect(vi.getTimerCount()).toBe(0);
  });
});
