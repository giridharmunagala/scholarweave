import { describe, expect, it } from 'vitest';

import type { RunStreamEvent } from '../../api/events';
import { applyChatStreamEvent, emptyChatStream, restoreChatStream } from './chatStream';

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

  it('marks a failed tool as finished without leaving it running', () => {
    const started = applyChatStreamEvent(
      emptyChatStream,
      event(3, 'tool.started', { tool_name: 'download_web_page' }),
    );
    const failed = applyChatStreamEvent(
      started,
      event(4, 'tool.failed', {
        tool_name: 'download_web_page',
        error: 'Remote download failed.',
      }),
    );

    expect(failed.tools).toEqual([
      { sequence: 3, toolName: 'download_web_page', status: 'failed' },
    ]);
  });

  it('replaces partial text with persisted snapshots after reconnecting', () => {
    const partial = applyChatStreamEvent(
      emptyChatStream,
      event(1, 'model.stream', {
        raw_type: 'response.output_text.delta',
        delta: 'Partial',
      }),
    );
    const recovered = applyChatStreamEvent(
      partial,
      event(2, 'model.stream', {
        raw_type: 'response.output_text.delta',
        delta: 'Complete response',
        snapshot: true,
      }),
    );

    expect(recovered.assistant).toBe('Complete response');
  });

  it('restores reasoning, output, and tools from persisted events in sequence order', () => {
    const restored = restoreChatStream([
      event(4, 'tool.completed', { tool_name: 'search_papers' }),
      event(2, 'tool.started', { tool_name: 'search_papers' }),
      event(3, 'model.stream', {
        raw_type: 'response.output_text.delta',
        delta: 'Found the answer.',
      }),
      event(1, 'model.stream', {
        raw_type: 'response.reasoning_text.delta',
        delta: 'Searching the library.',
      }),
    ]);

    expect(restored).toEqual({
      reasoning: 'Searching the library.',
      assistant: 'Found the answer.',
      tools: [
        { sequence: 2, toolName: 'search_papers', status: 'completed' },
      ],
    });
  });
});
