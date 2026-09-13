import { describe, expect, it } from 'vitest';
import type { RunStreamEvent } from '../../api/events';
import { buildTurnTimeline, describeLiveActivity, emptyTurnTimeline, type ToolStep } from './chatTimeline';

function event(
  sequence: number,
  event_type: string,
  payload: Record<string, unknown>,
): RunStreamEvent {
  return {
    sequence,
    event_type,
    payload,
    created_at: new Date(Date.parse('2026-08-17T10:00:00Z') + sequence * 1000).toISOString(),
  };
}

describe('hierarchical worker traces', () => {
  it('isolates concurrent same-named workers and nests a helper under its owning call', () => {
    const start = (sequence: number, id: string, parent: string, call: string) => event(sequence, 'agent.started', {
      agent_name: 'Worker', invocation_id: id, delegated: true,
      parent_invocation_id: parent, parent_tool_call_id: call, assignment: `Assignment ${id}`,
    });
    const tool = (sequence: number, kind: string, invocation: string, call: string, result?: unknown) =>
      event(sequence, kind, { invocation_id: invocation, tool_name: 'read_source', tool_call_id: call, result });
    const events = [
      event(1, 'agent.started', { agent_name: 'Coordinator', invocation_id: 'root' }),
      tool(2, 'tool.started', 'root', 'delegate-a'),
      start(3, 'a', 'root', 'delegate-a'),
      tool(4, 'tool.started', 'root', 'delegate-b'),
      start(5, 'b', 'root', 'delegate-b'),
      event(6, 'agent.stream', {
        invocation_id: 'a', raw_type: 'response.reasoning_text.delta', delta: 'Inspect A',
      }),
      event(7, 'agent.stream', {
        invocation_id: 'b', raw_type: 'response.reasoning_text.delta', delta: 'Inspect B', snapshot: true,
      }),
      tool(8, 'tool.started', 'a', 'same-call-id'),
      tool(9, 'tool.started', 'b', 'same-call-id'),
      tool(10, 'tool.completed', 'b', 'same-call-id', { text: 'B evidence' }),
      event(11, 'agent.completed', { agent_name: 'Worker', invocation_id: 'b', output: 'B handoff' }),
      tool(12, 'tool.completed', 'root', 'delegate-b'),
      tool(13, 'tool.completed', 'a', 'same-call-id', { text: 'A evidence' }),
      tool(14, 'tool.started', 'a', 'helper-call'),
      start(15, 'helper', 'a', 'helper-call'),
      tool(16, 'tool.started', 'helper', 'same-call-id'),
    ];
    const live = buildTurnTimeline(events);
    const rootTools = live.steps as ToolStep[];
    expect(rootTools).toHaveLength(2);
    const a = rootTools[0].children![0];
    const b = rootTools[1].children![0];
    expect(a.request).toBe('Assignment a');
    expect(a.children!.steps[0]).toMatchObject({ kind: 'reasoning', text: 'Inspect A' });
    expect(b.children!.steps[0]).toMatchObject({ kind: 'reasoning', text: 'Inspect B' });
    expect(a.children!.steps[1]).toMatchObject({ result: { text: 'A evidence' } });
    expect(b.children!.steps[1]).toMatchObject({ result: { text: 'B evidence' } });
    expect((a.children!.steps[2] as ToolStep).children![0].id).toBe('helper');
    expect(live.agentCount).toBe(3);
    expect(live.toolCount).toBe(6);
    expect(describeLiveActivity(live)).toMatchObject({ phase: 'tool', label: 'Using Read source' });

    const cancelled = buildTurnTimeline([...events, event(17, 'run.cancelled', {})]);
    const stoppedA = (cancelled.steps[0] as ToolStep).children![0];
    const stoppedHelper = (stoppedA.children!.steps[2] as ToolStep).children![0];
    expect(stoppedA.status).toBe('cancelled');
    expect(stoppedHelper.status).toBe('cancelled');
    expect(stoppedHelper.children!.steps[0]).toMatchObject({ status: 'cancelled' });
    expect(cancelled.running).toBe(false);
  });

  it('rebuilds the same tree from saved snapshots without duplicating live reasoning', () => {
    const events = [
      event(1, 'agent.started', { agent_name: 'Coordinator', invocation_id: 'root' }),
      event(2, 'tool.started', { tool_name: 'delegate', tool_call_id: 'delegate' }),
      event(3, 'agent.started', {
        agent_name: 'Worker', invocation_id: 'worker', delegated: true,
        parent_tool_call_id: 'delegate', parent_agent_name: 'Coordinator',
      }),
      event(4, 'agent.stream', {
        invocation_id: 'worker', raw_type: 'response.reasoning_text.delta', delta: 'Check ',
      }),
      event(5, 'agent.stream', {
        invocation_id: 'worker', raw_type: 'response.reasoning_text.delta', delta: 'Check sources', snapshot: true,
      }),
      event(6, 'agent.completed', { agent_name: 'Worker', invocation_id: 'worker', output: 'Verified' }),
      event(7, 'tool.completed', { tool_name: 'delegate', tool_call_id: 'delegate' }),
      event(8, 'agent.started', { agent_name: 'Coordinator', invocation_id: 'next-epoch' }),
    ];
    const live = buildTurnTimeline(events, { settled: true });
    const saved = buildTurnTimeline(events.filter((item) => item.sequence !== 4), { settled: true });
    const worker = (live.steps[0] as ToolStep).children![0];
    expect(worker.children!.steps).toHaveLength(1);
    expect(worker.children!.steps[0]).toMatchObject({ text: 'Check sources', streaming: false });
    expect(live.agentCount).toBe(1);
    expect(saved).toMatchObject({
      toolCount: live.toolCount, agentCount: live.agentCount,
      steps: [{ callId: 'delegate', children: [{
        id: 'worker', status: 'completed', output: 'Verified',
        children: { steps: [{ text: 'Check sources', streaming: false }] },
      }] }],
    });
  });

  it('shows a worker response as it streams and retracts only its retried text', () => {
    const events = [
      event(1, 'agent.started', { agent_name: 'Worker', invocation_id: 'worker', delegated: true }),
      event(2, 'agent.stream', { invocation_id: 'worker', raw_type: 'response.output_text.delta', delta: 'Partial answer' }),
    ];
    expect(buildTurnTimeline(events).steps[0]).toMatchObject({ output: 'Partial answer', status: 'running' });
    const timeline = buildTurnTimeline([
      ...events,
      event(3, 'model.retry', { invocation_id: 'worker', discarded_text_characters: 6 }),
      event(4, 'agent.stream', { invocation_id: 'worker', raw_type: 'response.output_text.delta', delta: 'Partial ', snapshot: true }),
      event(5, 'agent.stream', { invocation_id: 'worker', raw_type: 'response.output_text.delta', delta: 'retry' }),
    ]);
    expect(timeline.steps[0]).toMatchObject({ output: 'Partial retry' });
    expect(describeLiveActivity(timeline)).toMatchObject({ label: 'Writing the answer', detail: 'Worker' });
  });

  it('nests legacy sequential worker tools without requiring newly added parent IDs', () => {
    const timeline = buildTurnTimeline([
      event(1, 'agent.started', { agent_name: 'Coordinator' }),
      event(2, 'tool.started', { tool_name: 'focused_research_worker', tool_call_id: 'delegate' }),
      event(3, 'agent.started', { agent_name: 'Worker', invocation_id: 'worker', delegated: true, parent_agent_name: 'Coordinator' }),
      event(4, 'run.item', { delegated: true, delegate_agent_name: 'Worker', item: {
        type: 'tool_call_item', raw_item: { name: 'read_source', call_id: 'read', arguments: '{"query":"method"}' },
      } }),
      event(5, 'tool.completed', { invocation_id: 'worker', tool_name: 'read_source', tool_call_id: 'read', result: 'Evidence' }),
      event(6, 'agent.completed', { agent_name: 'Worker', invocation_id: 'worker', output: 'Done' }),
      event(7, 'tool.completed', { tool_name: 'focused_research_worker', tool_call_id: 'delegate' }),
    ], { settled: true });
    expect(timeline.steps).toHaveLength(1);
    const worker = (timeline.steps[0] as ToolStep).children![0];
    expect(worker.children!.steps).toMatchObject([{ kind: 'tool', query: 'method', result: 'Evidence' }]);
  });
});

describe('sub-agent timeline activity', () => {
  it('keeps nested agent outputs and omits the root answer duplicate', () => {
    const timeline = buildTurnTimeline([
      event(1, 'agent.started', { agent_name: 'ScholarWeave Coordinator' }),
      event(2, 'agent.started', { agent_name: 'Extended Work Planner' }),
      event(4, 'agent.completed', {
        agent_name: 'Extended Work Planner',
        output: { tasks: [{ id: 'evidence', title: 'Collect evidence' }] },
      }),
      event(5, 'agent.started', { agent_name: 'Focused Work Specialist' }),
      event(9, 'agent.completed', {
        agent_name: 'Focused Work Specialist',
        output: 'The focused evidence agrees across two sources.',
      }),
      event(10, 'agent.completed', {
        agent_name: 'ScholarWeave Coordinator',
        output: 'Final answer already shown in the transcript.',
      }),
    ], { settled: true });

    const agents = timeline.steps.filter((step) => step.kind === 'agent');
    expect(timeline.agentCount).toBe(2);
    expect(agents.map((agent) => agent.name)).toEqual([
      'Extended Work Planner',
      'Focused Work Specialist',
    ]);
    expect(agents[1]).toMatchObject({
      status: 'completed',
      completedSequence: 9,
      output: 'The focused evidence agrees across two sources.',
      seconds: 4,
    });

  });

  describe('tool timeline activity', () => {
    it('matches concurrent repeated tool calls by call ID', () => {
    const timeline = buildTurnTimeline([
      event(1, 'run.item', {
        item: {
          type: 'tool_call_item',
          raw_item: {
            name: 'search_papers',
            call_id: 'call-first',
            arguments: '{"query":"first"}',
          },
        },
      }),
      event(2, 'run.item', {
        item: {
          type: 'tool_call_item',
          raw_item: {
            name: 'search_papers',
            call_id: 'call-second',
            arguments: '{"query":"second"}',
          },
        },
      }),
      event(3, 'tool.started', {
        tool_name: 'search_papers',
        tool_call_id: 'call-first',
      }),
      event(4, 'tool.started', {
        tool_name: 'search_papers',
        tool_call_id: 'call-second',
      }),
      event(5, 'tool.completed', {
        tool_name: 'search_papers',
        tool_call_id: 'call-second',
        result: { result: 'second result' },
      }),
      event(6, 'tool.completed', {
        tool_name: 'search_papers',
        tool_call_id: 'call-first',
        result: { result: 'first result' },
      }),
    ], { settled: true });

    expect(timeline.steps.filter((step) => step.kind === 'tool')).toMatchObject([
      {
        query: 'first',
        result: { result: 'first result' },
        status: 'completed',
      },
      {
        query: 'second',
        result: { result: 'second result' },
        status: 'completed',
      },
    ]);
    });
  });

  it('tracks repeated focused workers as separate calls', () => {
    const timeline = buildTurnTimeline([
      event(1, 'agent.started', { agent_name: 'Coordinator' }),
      event(2, 'agent.started', { agent_name: 'Worker' }),
      event(3, 'agent.completed', { agent_name: 'Worker', output: 'First' }),
      event(4, 'agent.started', { agent_name: 'Worker' }),
      event(6, 'agent.completed', { agent_name: 'Worker', output: 'Second' }),
    ]);

    expect(timeline.steps.filter((step) => step.kind === 'agent')).toHaveLength(2);
  });

  it('settles the exact invocation as failed or superseded', () => {
    const timeline = buildTurnTimeline([
      event(1, 'agent.started', {
        agent_name: 'Coordinator',
        invocation_id: 'root',
      }),
      event(2, 'agent.started', {
        agent_name: 'Worker',
        invocation_id: 'worker-1',
      }),
      event(3, 'agent.superseded', {
        agent_name: 'Worker',
        invocation_id: 'worker-1',
        reason: 'context_high_water',
      }),
      event(4, 'agent.started', {
        agent_name: 'Worker',
        invocation_id: 'worker-2',
      }),
      event(5, 'agent.failed', {
        agent_name: 'Worker',
        invocation_id: 'worker-2',
        error: 'ContextLengthError: request too large',
      }),
    ], { settled: true });

    const agents = timeline.steps.filter((step) => step.kind === 'agent');
    expect(agents).toMatchObject([
      {
        id: 'worker-1',
        status: 'superseded',
        output: 'context_high_water',
      },
      {
        id: 'worker-2',
        status: 'failed',
        output: 'ContextLengthError: request too large',
      },
    ]);
    expect(timeline.running).toBe(false);
  });
});

describe('live activity status', () => {
  it('keeps repeated tool call IDs separate across model iterations and starts a fresh waiting phase', () => {
    const events: RunStreamEvent[] = [];
    for (let index = 0; index < 12; index += 1) {
      const sequence = index * 4;
      events.push(
        event(sequence, 'run.item', { item: { type: 'tool_call_item', raw_item: {
          name: 'search_web', call_id: 'call-0', arguments: JSON.stringify({ query: `query ${index}` }),
        } } }),
        event(sequence + 1, 'tool.started', { tool_name: 'search_web', tool_call_id: 'call-0' }),
        event(sequence + 2, 'tool.completed', { tool_name: 'search_web', tool_call_id: 'call-0', result: index }),
        event(sequence + 3, 'model.phase', { phase: 'waiting' }),
      );
    }
    const timeline = buildTurnTimeline(events);
    expect(timeline.toolCount).toBe(12);
    expect(timeline.steps[0]).toMatchObject({ query: 'query 0', result: 0 });
    expect(timeline.steps[11]).toMatchObject({ query: 'query 11', result: 11 });
    expect(describeLiveActivity(timeline)).toMatchObject({
      label: 'Waiting for model', phase: 'waiting', startedAt: Date.parse(events[47].created_at!),
    });
    const next = buildTurnTimeline([...events, event(48, 'model.phase', { phase: 'tool' })]);
    expect(describeLiveActivity(next).label).toBe('Preparing tool call');
  });

  it('shows a worker queue or prefill phase rather than a stale parent delegation', () => {
    const events = [
      event(1, 'tool.started', { tool_name: 'delegate', tool_call_id: 'delegate' }),
      event(2, 'agent.started', { agent_name: 'Reader', invocation_id: 'reader', delegated: true, parent_tool_call_id: 'delegate' }),
      event(3, 'model.phase', { invocation_id: 'reader', phase: 'queued' }),
    ];
    expect(describeLiveActivity(buildTurnTimeline(events))).toMatchObject({ label: 'Waiting in queue', detail: 'Reader' });
    events.push(event(4, 'model.phase', { invocation_id: 'reader', phase: 'processing' }));
    expect(describeLiveActivity(buildTurnTimeline(events))).toMatchObject({ label: 'Processing context', detail: 'Reader' });
  });

  it('names the tool the agent is waiting on, with the argument that identifies it', () => {
    const timeline = buildTurnTimeline([
      event(1, 'run.item', {
        item: {
          type: 'tool_call_item',
          raw_item: {
            name: 'search_papers',
            call_id: 'call-1',
            arguments: '{"query":"transformer scaling laws"}',
          },
        },
      }),
      event(2, 'tool.started', { tool_name: 'search_papers', tool_call_id: 'call-1' }),
    ]);

    expect(describeLiveActivity(timeline)).toMatchObject({
      phase: 'tool',
      label: 'Using Search papers',
      detail: 'transformer scaling laws',
    });

  });

  describe('tool failure messages', () => {
    it('shows the readable failure message instead of the raw exception', () => {
    const timeline = buildTurnTimeline([
      event(1, 'tool.started', {
        tool_name: 'download_web_page',
        tool_call_id: 'call-1',
      }),
      event(2, 'tool.failed', {
        tool_name: 'download_web_page',
        tool_call_id: 'call-1',
        category: 'upstream_unavailable',
        error: 'HTTPStatusError: 503 Service Unavailable at https://provider.internal',
        display_message: 'The source is temporarily unavailable. The agent can try another source.',
      }),
    ], { settled: true });

    expect(timeline.steps.filter((step) => step.kind === 'tool')).toMatchObject([
      {
        status: 'failed',
        detail: 'The source is temporarily unavailable. The agent can try another source.',
      },
    ]);
    });

    it('uses a readable category fallback for older persisted events', () => {
    const timeline = buildTurnTimeline([
      event(1, 'tool.failed', {
        tool_name: 'search_web',
        category: 'rate_limited',
        error: 'RateLimitError: raw provider response',
      }),
    ], { settled: true });

    expect(timeline.steps.filter((step) => step.kind === 'tool')).toMatchObject([
      {
        detail: 'The service is temporarily limiting requests. The agent can retry shortly.',
      },
    ]);
    });
  });

  it('reports thinking while reasoning streams and counts the settled steps behind it', () => {
    const timeline = buildTurnTimeline([
      event(1, 'run.item', {
        item: {
          type: 'tool_call_item',
          raw_item: { name: 'search_papers', call_id: 'call-1', arguments: '{"query":"a"}' },
        },
      }),
      event(2, 'tool.completed', { tool_name: 'search_papers', tool_call_id: 'call-1', result: {} }),
      event(3, 'model.stream', {
        raw_type: 'response.reasoning_text.delta',
        delta: 'Weighing the evidence',
      }),
    ]);

    expect(describeLiveActivity(timeline)).toMatchObject({
      phase: 'thinking',
      label: 'Thinking',
      completedSteps: 1,
    });
  });

  it('falls back to the writing and starting phases when nothing is in flight', () => {
    expect(describeLiveActivity(emptyTurnTimeline, { writing: true })).toMatchObject({
      phase: 'writing',
      label: 'Writing the answer',
    });
    expect(describeLiveActivity(emptyTurnTimeline)).toMatchObject({
      phase: 'starting',
      label: 'Starting the run',
      completedSteps: 0,
    });
  });
});
