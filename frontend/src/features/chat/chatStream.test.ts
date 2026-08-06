import { describe, expect, it } from 'vitest';

import type { RunStreamEvent } from '../../api/events';
import { applyChatStreamEvent, emptyChatStream } from './chatStream';

function event(
  sequence: number,
  event_type: string,
  payload: Record<string, unknown>,
): RunStreamEvent {
  return { sequence, event_type, payload };
}

describe('builder chat streaming', () => {
  it('streams reasoning and assistant text independently', () => {
    const reasoning = applyChatStreamEvent(
      emptyChatStream,
      event(1, 'model.stream', {
        raw_type: 'response.reasoning_summary_text.delta',
        delta: 'Inspecting tools',
      }),
    );
    const output = applyChatStreamEvent(
      reasoning,
      event(2, 'model.stream', {
        raw_type: 'response.output_text.delta',
        delta: 'Saved the agent.',
      }),
    );

    expect(output.reasoning).toBe('Inspecting tools');
    expect(output.assistant).toBe('Saved the agent.');
  });

  it('tracks tool lifecycle without duplicating completion', () => {
    const started = applyChatStreamEvent(
      emptyChatStream,
      event(3, 'tool.started', { tool_name: 'save_agent_blueprint' }),
    );
    const completed = applyChatStreamEvent(
      started,
      event(4, 'tool.completed', { tool_name: 'save_agent_blueprint' }),
    );

    expect(completed.tools).toEqual([
      { sequence: 3, toolName: 'save_agent_blueprint', status: 'completed' },
    ]);
  });
});
