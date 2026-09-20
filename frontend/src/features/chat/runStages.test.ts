import { describe, expect, it } from 'vitest';
import type { RunStreamEvent } from '../../api/events';
import { buildRunStages } from './runStages';

const at = (seconds: number) => new Date(Date.parse('2026-09-01T12:00:00Z') + seconds * 1000).toISOString();
const event = (sequence: number, event_type: string, payload: Record<string, unknown> = {}): RunStreamEvent =>
  ({ sequence, event_type, payload, created_at: at(sequence) });
const run = (events: RunStreamEvent[]) => ({
  events, status: 'running' as const, created_at: at(0), started_at: at(0), finished_at: null,
});

describe('buildRunStages', () => {
  it('replays context, model phases, tool calls, and writing in order with stable elapsed times', () => {
    const events = [
      event(0, 'run.started'),
      event(1, 'context.prepared'),
      event(2, 'model.phase', { model_call_id: 'm', phase: 'thinking' }),
      event(4, 'tool.started', { tool_name: 'search_web', tool_call_id: 't' }),
      event(7, 'tool.completed', { tool_name: 'search_web', tool_call_id: 't' }),
      event(8, 'model.phase', { model_call_id: 'n', phase: 'writing' }),
      event(12, 'run.completed'),
    ];
    const stages = buildRunStages({ ...run(events), status: 'completed', finished_at: at(12) });
    expect(stages.map((stage) => stage.label)).toEqual([
      'Context prepared', 'Thinking', 'Search web', 'Writing the answer',
    ]);
    expect(stages.map((stage) => stage.status)).toEqual(Array(4).fill('completed'));
    expect(stages.map((stage) => (stage.finishedAt! - stage.startedAt!) / 1000)).toEqual([1, 2, 3, 4]);
    expect(buildRunStages({ ...run([...events].reverse().concat(events)), status: 'completed', finished_at: at(12) })).toEqual(stages);
  });

  it('keeps delegated completion and phases from settling main-model work', () => {
    const stages = buildRunStages(run([
      event(0, 'model.phase', { model_call_id: 'm', phase: 'thinking' }),
      event(1, 'agent.started', { delegated: true, agent_name: 'Reader', invocation_id: 'a' }),
      event(2, 'model.phase', { delegated: true, model_call_id: 'd', phase: 'writing' }),
      event(3, 'tool.started', { delegated: true, tool_name: 'read_source', invocation_id: 'a' }),
      event(4, 'agent.completed', { delegated: true, agent_name: 'Reader', invocation_id: 'a' }),
      event(5, 'run.completed', { delegated: true }),
    ]));
    expect(stages[0].status).toBe('running');
    expect(stages[1].status).toBe('completed');
    expect(stages.map((stage) => stage.label)).not.toContain('Writing the answer');
  });

  it('settles missing terminal events from durable status without claiming cancellation succeeded', () => {
    const stages = buildRunStages({
      ...run([event(1, 'tool.started', { tool_name: 'read_source' })]),
      status: 'cancelled', finished_at: at(4),
    });
    expect(stages[0].status).toBe('cancelled');
    expect(stages[0].finishedAt).toBe(Date.parse(at(4)));
  });

  it('preserves failures, retries and unknown timestamps', () => {
    const stages = buildRunStages(run([
      event(0, 'tool.started', { tool_name: 'search_web', tool_call_id: 'a' }),
      event(1, 'tool.failed', { tool_name: 'search_web', tool_call_id: 'a' }),
      { sequence: 2, event_type: 'tool.started', payload: { tool_name: 'search_web', tool_call_id: 'a' } },
    ]));
    expect(stages.map((stage) => stage.status)).toEqual(['failed', 'running']);
    expect(stages[0].id).not.toBe(stages[1].id);
    expect(stages[1].startedAt).toBeNull();
  });
});
