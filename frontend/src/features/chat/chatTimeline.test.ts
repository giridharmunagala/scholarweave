import { describe, expect, it } from 'vitest';
import type { RunStreamEvent } from '../../api/events';
import { buildTurnTimeline } from './chatTimeline';

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
      output: 'The focused evidence agrees across two sources.',
      seconds: 4,
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
});
