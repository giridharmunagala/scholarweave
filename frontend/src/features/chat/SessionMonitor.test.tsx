// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SessionOverview } from './SessionMonitor';
import { buildSessionObservability } from './sessionObservability';
import type { Run } from './api';

describe('Session observability views', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.useRealTimers();
  });

  it('reports totals and speeds without offering its own way to open the panel', () => {
    act(() => root.render(<SessionOverview summary={buildSessionObservability([])} />));
    expect(container.querySelector('summary')?.textContent).toBe('Usage · 0 tokens · 0 calls');
    expect(container.querySelector('details')?.open).toBe(false);
    expect(container.querySelector('[aria-label="Work plan"]')).toBeNull();
    expect(container.querySelector('[aria-label="Prompt cache"]')).toBeNull();
    expect(container.textContent).toContain('Input tokens');
    expect(container.textContent).toContain('Output tokens');
    expect(container.textContent).toContain('Average prefill');
    expect(container.textContent).toContain('Average generation');
    // Observe in the chat header is the only control that opens observability.
    expect(container.querySelector('button')).toBeNull();
  });

  it('shows reported cache reuse without treating missing reports as zero hits', () => {
    const summary = buildSessionObservability([]);
    summary.performance.modelCalls = 3;
    summary.performance.cache = { tokens: 1024, reportedCalls: 2 };
    act(() => root.render(<SessionOverview summary={summary} />));
    expect(container.querySelector('[aria-label="Prompt cache"]')?.textContent)
      .toBe('Prompt cache: 1,024 input tokens reused · 2/3 calls reported.');
    summary.performance.cache = { tokens: 0, reportedCalls: 1 };
    act(() => root.render(<SessionOverview summary={{ ...summary }} />));
    expect(container.querySelector('[aria-label="Prompt cache"]')?.textContent).toContain('0 input tokens reused');
  });

  it('collapses task detail and does not duplicate workers from the trace', () => {
    const summary = buildSessionObservability([]);
    summary.activeWorkers = 2;
    summary.activeRuns = 1;
    summary.workPlan = [
      { id: 'a', runId: 'r', title: 'Read evidence', status: 'completed', notes: null },
      { id: 'b', runId: 'r', title: 'Retrieve PDF', status: 'blocked', notes: 'Source unavailable' },
    ];
    summary.workPlanCounts = { completed: 1, blocked: 1, pending: 0, in_progress: 0 };
    act(() => root.render(<SessionOverview summary={summary} />));
    expect(container.textContent).not.toContain('active workers');
    expect(container.textContent).toContain('1/2 completed');
    expect(container.textContent).toContain('1 blocked');
    expect(container.querySelector('button')).toBeNull();
    expect(container.querySelector<HTMLDetailsElement>('[aria-label="Work plan"]')?.open).toBe(false);
  });

  it('labels incomplete and estimated usage and explains server-only coverage', () => {
    const summary = buildSessionObservability([]);
    summary.totals = { inputTokens: 30, outputTokens: null, totalTokens: null, complete: false, estimated: true };
    summary.performance.prefill = { tokens: 30, seconds: 3, tokensPerSecond: 10, timedCalls: 1, complete: false };
    summary.performance.modelCalls = 2;
    act(() => root.render(<SessionOverview summary={summary} />));
    expect(container.textContent).toContain('Estimated, incomplete usage');
    expect(container.textContent).toContain('server active time where available');
    expect(container.textContent).toContain('tokens unavailable');
    expect(container.textContent).toContain('10 tok/s');
    expect(container.textContent).not.toContain('NaN');
    expect(container.textContent).not.toContain('Infinity');
  });

  it('leaves worker assignments and live progress to the hierarchical trace', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-09-06T12:00:05Z'));
    const summary = buildSessionObservability([]);
    summary.workers = [{
      id: 'r:w', runId: 'r', invocationId: 'w', name: 'Reader', delegated: true,
      request: 'Read the experimental setup', requestTruncated: true, status: 'running', phase: 'Tool: read_page',
      model: 'local-model', startedAt: Date.parse('2026-09-06T12:00:00Z'), finishedAt: null,
      elapsedSeconds: null, completedTools: 2, completedModels: 1, error: null,
    }];
    summary.activeWorkers = 1;
    act(() => root.render(<SessionOverview summary={summary} />));
    expect(container.textContent).not.toContain('Read the experimental setup');
    expect(container.textContent).not.toContain('Assigned request');
    act(() => vi.advanceTimersByTime(2000));
    expect(container.textContent).not.toContain('elapsed');
    expect(container.querySelector('[role="progressbar"]')).toBeNull();
    expect(container.textContent).not.toContain('%');
  });

  it('throttles displayed rates to five seconds while updating tokens immediately', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-09-06T12:00:00Z'));
    const summary = buildSessionObservability([]);
    summary.totals = { inputTokens: 100, outputTokens: 20, totalTokens: 120, complete: true, estimated: false };
    summary.performance.prefill = { tokens: 100, seconds: 10, tokensPerSecond: 10, timedCalls: 1, complete: true };
    act(() => root.render(<SessionOverview summary={summary} />));
    expect(container.textContent).toContain('10 tok/s');
    act(() => vi.advanceTimersByTime(1000));
    const updated = {
      ...summary, totals: { ...summary.totals, inputTokens: 200, totalTokens: 220 },
      performance: { ...summary.performance, prefill: { ...summary.performance.prefill, tokensPerSecond: 20 } },
    };
    act(() => root.render(<SessionOverview summary={updated} />));
    expect(container.textContent).toContain('220');
    expect(container.textContent).toContain('10 tok/s');
    expect(container.textContent).not.toContain('20 tok/s');
    act(() => vi.advanceTimersByTime(3999));
    expect(container.textContent).toContain('10 tok/s');
    act(() => vi.advanceTimersByTime(1));
    expect(container.textContent).toContain('20 tok/s');
  });

  it('exposes errors in collapsible run rows and renders work-plan notes as text', () => {
    const record: Run = {
      id: 'r', agent_name: 'Main', status: 'failed', input: '', conversation_id: null,
      created_at: '2026-09-06T12:00:00Z', started_at: null, finished_at: null,
      cancel_requested: false, error: 'Provider failed', events: [], epochs: [], items: [],
      tool_attempts: [], final_output: null, last_agent_name: null, usage: {},
      goal_state: { items: [{ id: 'a', title: 'Read source', status: 'blocked', summary: '<script>bad()</script> unavailable' }] },
    };
    act(() => root.render(<SessionOverview summary={buildSessionObservability([record])} />));
    expect(container.querySelector('details.session-observability-error > summary')?.textContent).toBe('Errors · 1');
    expect(container.querySelector('[aria-label="Run errors"]')?.textContent).toContain('Provider failed');
    expect(container.textContent).toContain('Read source');
    expect(container.textContent).toContain('<script>bad()</script> unavailable');
    expect(container.querySelector('script')).toBeNull();
  });
});
