// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SessionOverview, SessionStatusStrip } from './SessionMonitor';
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
    act(() => root.render(<SessionStatusStrip summary={buildSessionObservability([])} />));
    expect(container.textContent).toContain('Session tokens');
    expect(container.textContent).not.toContain('Input');
    expect(container.textContent).not.toContain('Output');
    expect(container.textContent).toContain('Prefill');
    expect(container.textContent).toContain('Generation');
    // Observe in the chat header is the only control that opens observability.
    expect(container.querySelector('button')).toBeNull();
  });

  it('keeps active workers and blocked task progress visible as plain read-outs', () => {
    const summary = buildSessionObservability([]);
    summary.activeWorkers = 2;
    summary.activeRuns = 1;
    summary.workPlan = [
      { id: 'a', runId: 'r', title: 'Read evidence', status: 'completed', notes: null },
      { id: 'b', runId: 'r', title: 'Retrieve PDF', status: 'blocked', notes: 'Source unavailable' },
    ];
    summary.workPlanCounts = { completed: 1, blocked: 1, pending: 0, in_progress: 0 };
    act(() => root.render(<SessionStatusStrip summary={summary} />));
    expect(container.textContent).toContain('2 workers active');
    expect(container.textContent).toContain('1/2 tasks');
    expect(container.textContent).toContain('1 blocked');
    expect(container.querySelector('button')).toBeNull();
  });

  it('labels incomplete and estimated usage and explains server-only coverage', () => {
    const summary = buildSessionObservability([]);
    summary.totals = { inputTokens: 30, outputTokens: null, totalTokens: null, complete: false, estimated: true };
    summary.performance.prefill = { tokens: 30, seconds: 3, tokensPerSecond: 10, timedCalls: 1, complete: false };
    summary.performance.modelCalls = 2;
    act(() => root.render(<><SessionStatusStrip summary={summary} /><SessionOverview summary={summary} /></>));
    expect(container.textContent).toContain('Estimated, incomplete usage');
    expect(container.textContent).toContain('Partial server timing · 1/2 calls');
    expect(container.textContent).toContain('Server timing unavailable');
    expect(container.textContent).toContain('30 tokens / 3s active time');
    expect(container.textContent).not.toContain('NaN');
    expect(container.textContent).not.toContain('Infinity');
  });

  it('shows live worker assignment and elapsed time separately from task completion', () => {
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
    expect(container.textContent).toContain('Read the experimental setup');
    expect(container.textContent).toContain('Assigned request (truncated)');
    expect(container.textContent).toContain('Tool: read_page · local-model');
    expect(container.textContent).toContain('5s elapsed · 2 tools completed');
    act(() => vi.advanceTimersByTime(2000));
    expect(container.textContent).toContain('7s elapsed');
    expect(container.textContent).toContain('0 worker tasks completed');
    expect(container.querySelector('[role="progressbar"]')).toBeNull();
    expect(container.textContent).not.toContain('%');
  });

  it('throttles displayed rates to five seconds while updating tokens immediately', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-09-06T12:00:00Z'));
    const summary = buildSessionObservability([]);
    summary.totals = { inputTokens: 100, outputTokens: 20, totalTokens: 120, complete: true, estimated: false };
    summary.performance.prefill = { tokens: 100, seconds: 10, tokensPerSecond: 10, timedCalls: 1, complete: true };
    act(() => root.render(<SessionStatusStrip summary={summary} />));
    expect(container.textContent).toContain('10 tok/s');
    act(() => vi.advanceTimersByTime(1000));
    const updated = {
      ...summary, totals: { ...summary.totals, inputTokens: 200, totalTokens: 220 },
      performance: { ...summary.performance, prefill: { ...summary.performance.prefill, tokensPerSecond: 20 } },
    };
    act(() => root.render(<SessionStatusStrip summary={updated} />));
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
    expect(container.querySelector('details.session-run-row > summary')?.textContent).toContain('1 errors');
    expect(container.querySelector('[aria-label="Run errors"]')?.textContent).toBe('Provider failed');
    expect(container.textContent).toContain('Read source');
    expect(container.textContent).toContain('<script>bad()</script> unavailable');
    expect(container.querySelector('script')).toBeNull();
  });
});
