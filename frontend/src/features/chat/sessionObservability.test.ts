import { describe, expect, it } from 'vitest';
import type { Run } from './api';
import { buildSessionObservability } from './sessionObservability';

const at = (seconds: number) => new Date(Date.UTC(2026, 8, 6, 12, 0, seconds)).toISOString();

function event(sequence: number, event_type: string, payload: Record<string, unknown> = {}): Run['events'][number] {
  return { sequence, event_type, payload, created_at: at(sequence) };
}

function run(overrides: Partial<Run> = {}): Run {
  return {
    id: 'run-1', agent_name: 'Main', status: 'completed', input: 'Research',
    conversation_id: 'conversation-1', created_at: at(0), started_at: at(0), finished_at: at(20),
    cancel_requested: false, error: null, events: [], epochs: [], items: [], tool_attempts: [],
    final_output: null, last_agent_name: null, goal_state: null, usage: {}, ...overrides,
  };
}

function performance(overrides: Record<string, unknown> = {}) {
  return {
    model_calls: 1, main_model_calls: 1, delegated_model_calls: 0,
    input_tokens: 100, output_tokens: 20, usage_complete: true,
    timing_source: 'server', timed_prompt_tokens: 100, prompt_seconds: 1, prompt_timed_calls: 1,
    timed_output_tokens: 20, generation_seconds: 2, generation_timed_calls: 1, ...overrides,
  };
}

function telemetry(id: string, overrides: Record<string, unknown> = {}) {
  return {
    model_call_id: id, agent_name: 'Main', context_scope: 'main',
    usage: { input_tokens: 100, output_tokens: 20 }, usage_complete: true,
    timings: { prompt_n: 100, prompt_ms: 1000, predicted_n: 20, predicted_ms: 2000 },
    ...overrides,
  };
}

describe('buildSessionObservability', () => {
  it('identifies the coordinator separately from the blueprint name and early delegated events', () => {
    const summary = buildSessionObservability([run({
      agent_name: 'Research blueprint', status: 'running', finished_at: null,
      events: [
        event(1, 'agent.started', { agent_name: 'Reader', invocation_id: 'worker', delegated: true }),
        event(2, 'agent.started', { agent_name: 'Coordinator', invocation_id: 'root' }),
        event(3, 'agent.completed', { agent_name: 'Reader', invocation_id: 'worker' }),
      ],
    })]);
    expect(summary.workers.find((worker) => worker.name === 'Coordinator')).toMatchObject({
      delegated: false, status: 'running', request: 'Research',
    });
    expect(summary.activeWorkers).toBe(0);
    expect(summary.completedWorkers).toBe(1);
    expect(summary.activeRuns).toBe(1);
  });

  it('distinguishes an empty session, missing usage, and reported zero usage', () => {
    expect(buildSessionObservability([]).totals).toEqual({
      inputTokens: 0, outputTokens: 0, totalTokens: 0, complete: true, estimated: false,
    });
    const missing = buildSessionObservability([run()]);
    expect(missing.totals.totalTokens).toBeNull();
    expect(missing.totals.complete).toBe(false);
    expect(missing.performance.prefill.tokensPerSecond).toBeNull();
    const zero = buildSessionObservability([run({ usage: { performance: performance({
      input_tokens: 0, output_tokens: 0, timed_prompt_tokens: 0, timed_output_tokens: 0,
    }) } })]);
    expect(zero.totals.totalTokens).toBe(0);
    expect(zero.totals.complete).toBe(true);
    expect(zero.performance.prefill.tokensPerSecond).toBe(0);
  });

  it('adds runs once and never sums cumulative updates, top-level usage, or individual calls twice', () => {
    const usage = { input_tokens: 9999, output_tokens: 9999, performance: performance({
      model_calls: 2, input_tokens: 200, output_tokens: 40,
    }) };
    const events = [
      event(1, 'model.telemetry', telemetry('call-1')),
      event(2, 'usage.updated', { performance: performance() }),
      event(3, 'model.telemetry', telemetry('call-2')),
      event(4, 'usage.updated', { performance: usage.performance }),
      event(5, 'run.completed', { usage }),
    ];
    const original = run({ usage, events });
    const summary = buildSessionObservability([original, { ...original, events: [...events, events[1]] }]);
    expect(summary.runs).toHaveLength(1);
    expect(summary.totals.totalTokens).toBe(240);
    expect(summary.performance.modelCalls).toBe(2);
    expect(summary.runs[0].calls).toHaveLength(2);
  });

  it('replaces a persisted snapshot with a live cumulative update and ignores older replay', () => {
    const summary = buildSessionObservability([run({
      status: 'running', finished_at: null,
      usage: { performance: performance() },
      events: [
        event(4, 'usage.updated', { performance: performance({ model_calls: 2, input_tokens: 500, output_tokens: 70 }) }),
        event(2, 'usage.updated', { performance: performance() }),
      ],
    })]);
    expect(summary.totals.totalTokens).toBe(570);
    const persisted = buildSessionObservability([run({
      usage: { performance: performance({ model_calls: 3, input_tokens: 600 }) },
      events: [event(2, 'usage.updated', { performance: performance() })],
    })]);
    expect(persisted.totals.inputTokens).toBe(600);
  });

  it('uses server-active-time weighting rather than a mean of per-run rates', () => {
    const summary = buildSessionObservability([
      run({ usage: { performance: performance({ prompt_tokens_per_second: 999 }) } }),
      run({ id: 'run-2', usage: { performance: performance({
        input_tokens: 900, output_tokens: 180, timed_prompt_tokens: 900, prompt_seconds: 3,
        timed_output_tokens: 180, generation_seconds: 3, prompt_tokens_per_second: 999,
      }) } }),
    ]);
    expect(summary.performance.prefill.tokensPerSecond).toBe(250);
    expect(summary.performance.generation.tokensPerSecond).toBe(40);
    expect(summary.performance.prefill.tokensPerSecond).not.toBe(200);
    expect(summary.performance.prefill.complete).toBe(true);
    expect(summary.performance.prefill.timedCalls).toBe(2);
  });

  it('never treats legacy wall-clock speeds or untrusted/missing denominators as server timing', () => {
    for (const invalid of [
      { timing_source: 'wall_clock' },
      { timing_source: undefined },
      { prompt_seconds: 0, generation_seconds: 0 },
      { prompt_seconds: NaN, generation_seconds: Infinity },
      { timed_prompt_tokens: -1, timed_output_tokens: '20' },
    ]) {
      const summary = buildSessionObservability([run({ usage: { performance: performance(invalid) } })]);
      expect(summary.performance.prefill.tokensPerSecond).toBeNull();
      expect(summary.performance.generation.tokensPerSecond).toBeNull();
    }
    const legacy = buildSessionObservability([run({ usage: {
      input_tokens: 100, output_tokens: 20, requests: 1,
      performance: { input_tokens: 100, output_tokens: 20, prompt_tokens_per_second: 20 },
    } })]);
    expect(legacy.totals.totalTokens).toBe(120);
    expect(legacy.totals.complete).toBe(false);
    expect(legacy.performance.prefill.tokensPerSecond).toBeNull();
  });

  it('marks missing/partial/estimated totals and coverage without discarding known values', () => {
    const summary = buildSessionObservability([
      run({ usage: { performance: performance({
        model_calls: 2, usage_complete: false, input_tokens_estimated: true,
      }) } }),
      run({ id: 'run-2' }),
    ]);
    expect(summary.totals).toEqual({
      inputTokens: 100, outputTokens: 20, totalTokens: 120, complete: false, estimated: true,
    });
    expect(summary.performance.prefill.tokensPerSecond).toBe(100);
    expect(summary.performance.prefill.timedCalls).toBe(1);
    expect(summary.performance.prefill.complete).toBe(false);
  });

  it('retains old server measurements but marks absent timing-call counts as unknown', () => {
    const summary = buildSessionObservability([run({ usage: { performance: performance({
      prompt_timed_calls: undefined, generation_timed_calls: undefined,
    }) } })]);
    expect(summary.performance.prefill.tokensPerSecond).toBe(100);
    expect(summary.performance.prefill.timedCalls).toBeNull();
    expect(summary.performance.prefill.complete).toBe(false);
  });

  it('reconstructs all native call scopes once when no cumulative snapshot is available', () => {
    const main = telemetry('main');
    const delegate = telemetry('delegate', { delegated: true, context_scope: 'delegate' });
    const compaction = telemetry('compact', { context_scope: 'compaction' });
    const summaryCall = telemetry('summary', { context_scope: 'summary' });
    const summary = buildSessionObservability([run({ events: [
      event(1, 'model.telemetry', main), event(2, 'model.telemetry', delegate),
      event(3, 'model.telemetry', compaction), event(4, 'model.telemetry', summaryCall),
      event(5, 'model.telemetry', main),
      event(6, 'model.completed', { usage: main.usage }),
    ] })]);
    expect(summary.totals.totalTokens).toBe(480);
    expect(summary.performance.modelCalls).toBe(4);
    expect(summary.performance.mainModelCalls).toBe(1);
    expect(summary.performance.delegatedModelCalls).toBe(1);
    expect(summary.performance.prefill.timedCalls).toBe(4);
  });

  it('uses terminal usage even without lifecycle telemetry', () => {
    const summary = buildSessionObservability([run({
      status: 'running',
      events: [event(1, 'run.completed', { usage: {
        input_tokens: 5, output_tokens: 2, total_tokens: 7, usage_complete: true,
      } })],
    })]);
    expect(summary.totals.totalTokens).toBe(7);
    expect(summary.activeRuns).toBe(0);
  });

  it('does not replace legacy or cumulative spend with a partial native replay', () => {
    const events = [
      event(1, 'model.telemetry', telemetry('native-1')),
      event(2, 'model.telemetry', telemetry('native-2')),
    ];
    const legacy = buildSessionObservability([run({
      usage: { requests: 3, input_tokens: 800, output_tokens: 150 }, events,
    })]);
    expect(legacy.totals.totalTokens).toBe(950);
    expect(legacy.performance.modelCalls).toBe(3);
    expect(legacy.performance.prefill.timedCalls).toBe(2);
    expect(legacy.performance.prefill.complete).toBe(false);
    const cumulative = buildSessionObservability([run({
      usage: { performance: performance({ input_tokens: 800, output_tokens: 150 }) }, events,
    })]);
    expect(cumulative.totals.totalTokens).toBe(950);
  });

  it('preserves legacy terminal usage when a stale duplicate run has no usage', () => {
    const finished = run({ usage: { input_tokens: 100, output_tokens: 20, usage_complete: true } });
    const stale = run({ status: 'running', usage: {}, finished_at: null });
    const summary = buildSessionObservability([finished, stale]);
    expect(summary.totals.totalTokens).toBe(120);
    expect(summary.activeRuns).toBe(0);
  });

  it('attributes concurrent repeated worker invocations and tools by their IDs', () => {
    const summary = buildSessionObservability([run({
      status: 'running', finished_at: null,
      events: [
        event(1, 'agent.started', { agent_name: 'Worker', invocation_id: 'first', delegated: true, assignment: 'Read paper A', assignment_truncated: true }),
        event(2, 'agent.started', { agent_name: 'Worker', invocation_id: 'second', delegated: true, assignment: 'Read paper B' }),
        event(3, 'model.started', { agent_name: 'Worker', invocation_id: 'second', input_item_count: 2 }),
        event(4, 'tool.started', { agent_name: 'Worker', invocation_id: 'first', tool_call_id: 'read-a', tool_name: 'read_page' }),
        event(5, 'tool.completed', { agent_name: 'Worker', invocation_id: 'first', tool_call_id: 'read-a' }),
        event(5, 'tool.completed', { agent_name: 'Worker', invocation_id: 'first', tool_call_id: 'read-a' }),
        event(6, 'agent.completed', { agent_name: 'Worker', invocation_id: 'first' }),
        event(7, 'agent.started', { agent_name: 'Worker', invocation_id: 'third', delegated: true, assignment: 'Compare findings' }),
      ],
    })]);
    expect(summary.workers).toHaveLength(3);
    expect(summary.workers[0]).toMatchObject({
      request: 'Read paper A', requestTruncated: true, status: 'completed', completedTools: 1, elapsedSeconds: 5,
    });
    expect(summary.workers[1]).toMatchObject({
      request: 'Read paper B', status: 'running', model: null, phase: 'Model call',
    });
    expect(summary.activeWorkers).toBe(2);
    expect(summary.completedWorkers).toBe(1);
  });

  it('handles exact model lifecycle payloads without model IDs and avoids telemetry double-counting', () => {
    const summary = buildSessionObservability([run({
      status: 'running', finished_at: null, events: [
        event(1, 'agent.started', { agent_name: 'Worker', invocation_id: 'worker', assignment: 'Read the paper' }),
        event(2, 'model.started', { agent_name: 'Worker', invocation_id: 'worker', input_item_count: 2 }),
        event(3, 'model.telemetry', telemetry('model-call', {
          agent_name: 'Worker', invocation_id: 'worker', model: 'local-model', completed: true, delegated: true,
        })),
        event(4, 'model.completed', { agent_name: 'Worker', invocation_id: 'worker', usage: { input_tokens: 100, output_tokens: 20 } }),
        event(5, 'tool.started', { agent_name: 'Worker', invocation_id: 'worker', tool_call_id: 'read', tool_name: 'read_page' }),
      ],
    })]);
    expect(summary.workers[0]).toMatchObject({
      completedModels: 1, model: 'local-model', phase: 'Tool: read_page', status: 'running',
    });
    expect(summary.performance.modelCalls).toBe(1);
  });

  it('does not guess attribution for concurrent legacy events without invocation IDs', () => {
    const summary = buildSessionObservability([run({
      status: 'running', finished_at: null,
      events: [
        event(1, 'agent.started', { agent_name: 'Worker', invocation_id: 'a' }),
        event(2, 'agent.started', { agent_name: 'Worker', invocation_id: 'b' }),
        event(3, 'tool.started', { agent_name: 'Worker', tool_name: 'search_web' }),
        event(4, 'agent.completed', { agent_name: 'Worker' }),
      ],
    })]);
    expect(summary.workers.every((worker) => worker.status === 'running' && worker.phase === 'Starting')).toBe(true);
  });

  it.each(['completed', 'failed', 'cancelled'] as const)('settles unfinished workers on %s without inventing completion', (status) => {
    const summary = buildSessionObservability([run({
      status, events: [
        event(1, 'agent.started', { agent_name: 'Worker', invocation_id: 'a' }),
        event(2, `run.${status}`, { error: status === 'failed' ? 'Provider disconnected' : undefined }),
      ],
    })]);
    expect(summary.workers[0].status).toBe(status === 'completed' ? 'interrupted' : status);
    expect(summary.workers[0].elapsedSeconds).toBe(1);
    expect(summary.activeWorkers).toBe(0);
    expect(summary.completedWorkers).toBe(0);
  });

  it('settles explicit interruptions and keeps new recovered invocations separate', () => {
    const interruptedEvents = [
      event(1, 'agent.started', { agent_name: 'Worker', invocation_id: 'old', delegated: true }),
      event(2, 'run.interrupted', { reason: 'Server restart' }),
    ];
    const interrupted = buildSessionObservability([run({
      status: 'running', finished_at: null, events: interruptedEvents,
    })]);
    expect(interrupted.workers[0]).toMatchObject({ status: 'interrupted', elapsedSeconds: 1 });
    expect(interrupted.activeWorkers).toBe(0);
    expect(interrupted.activeRuns).toBe(0);
    expect(interrupted.runs[0].status).toBe('interrupted');

    const recovered = buildSessionObservability([run({
      status: 'running', finished_at: null, events: [
        ...interruptedEvents, event(3, 'run.recovered'),
        event(4, 'agent.started', { agent_name: 'Worker', invocation_id: 'new', delegated: true }),
        event(5, 'model.started', { agent_name: 'Worker', invocation_id: 'new' }),
      ],
    })]);
    expect(recovered.workers[0].status).toBe('interrupted');
    expect(recovered.workers[1]).toMatchObject({ status: 'running', phase: 'Model call' });
    expect(recovered.activeWorkers).toBe(1);
    expect(recovered.activeRuns).toBe(1);
  });

  it('projects work-plan titles, notes and all four states without a progress percentage', () => {
    const items = [
      { id: 'a', title: 'Find evidence', status: 'pending', summary: '' },
      { id: 'b', title: 'Read sources', status: 'in_progress', summary: 'Reading PDF' },
      { id: 'c', title: 'Compare', status: 'completed', summary: 'Two sources agree' },
      { id: 'd', title: 'Check appendix', status: 'blocked', summary: 'Appendix unavailable' },
    ];
    const summary = buildSessionObservability([run({
      status: 'running', goal_state: { version: 1, items: [items[0]] },
      events: [event(3, 'tool.completed', { tool_name: 'update_work_item', result: { items } })],
    })]);
    expect(summary.workPlan).toHaveLength(4);
    expect(summary.workPlan[2]).toMatchObject({ title: 'Compare', notes: 'Two sources agree' });
    expect(summary.workPlanCounts).toEqual({ pending: 1, in_progress: 1, completed: 1, blocked: 1 });
    expect(summary).not.toHaveProperty('percentComplete');
  });

  it('prefers the latest small work tracker snapshot over legacy goal state', () => {
    const summary = buildSessionObservability([run({
      goal_state: { version: 5, items: [{ id: 'a', title: 'Read', status: 'completed', summary: 'Done' }] },
      events: [event(2, 'tool.completed', {
        tool_name: 'create_work_plan', result: { items: [{ id: 'a', title: 'Read', status: 'pending' }] },
      })],
    })]);
    expect(summary.workPlanCounts.completed).toBe(0);
    expect(summary.workPlanCounts.pending).toBe(1);
  });

  it('does not replace the work plan after a failed tool call', () => {
    const summary = buildSessionObservability([run({ events: [
      event(1, 'tool.completed', {
        tool_name: 'create_work_plan', result: { items: [{ id: 'a', title: 'Read source', status: 'pending', summary: '' }] },
      }),
      event(2, 'tool.completed', {
        tool_name: 'update_work_item', result: { items: [{ id: 'a', title: 'Read source', status: 'blocked', summary: 'PDF missing' }] },
      }),
      event(3, 'tool.failed', {
        tool_name: 'update_work_item', result: { items: [] }, error: 'Invalid update',
      }),
    ] })]);
    expect(summary.workPlan).toHaveLength(1);
    expect(summary.workPlan[0]).toMatchObject({ status: 'blocked', notes: 'PDF missing' });
  });

  it('uses versioned goal snapshots in order and ignores older live replay', () => {
    const pending = { id: 'a', title: 'Read', status: 'pending' };
    const completed = { ...pending, status: 'completed', summary: 'Evidence saved' };
    const summary = buildSessionObservability([run({
      status: 'running', goal_state: { version: 3, items: [completed] },
      events: [
        event(2, 'goal.plan.updated', { version: 1, items: [pending] }),
        event(3, 'goal.plan.updated', { version: 4, items: [completed, { id: 'b', title: 'Compare', status: 'in_progress' }] }),
        event(4, 'goal.plan.updated', { version: 2, items: [pending] }),
      ],
    })]);
    expect(summary.workPlanCounts.completed).toBe(1);
    expect(summary.workPlanCounts.in_progress).toBe(1);
    expect(summary.workPlanCounts.pending).toBe(0);
  });
});
