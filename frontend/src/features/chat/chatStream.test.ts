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
  it('retracts only a retried response, including Unicode, identically on replay', () => {
    const events = [
      event(1, 'model.stream', { raw_type: 'response.output_text.delta', delta: 'Earlier. ' }),
      event(2, 'model.stream', { raw_type: 'response.output_text.delta', delta: 'Partial \u{1F4DA}' }),
      event(3, 'model.retry', { discarded_text_characters: 9 }),
      event(4, 'model.stream', {
        raw_type: 'response.output_text.delta', delta: 'Earlier. ', snapshot: true,
      }),
      event(5, 'model.retry', { discarded_text_characters: 9, delegated: true }),
      event(6, 'model.stream', { raw_type: 'response.output_text.delta', delta: 'Complete.' }),
    ];
    expect(events.reduce(applyChatStreamEvent, emptyChatStream).assistant).toBe('Earlier. Complete.');
    expect(restoreChatStream(events).assistant).toBe('Earlier. Complete.');
  });

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

    expect(restored.reasoning).toBe('Searching the library.');
    expect(restored.assistant).toBe('Found the answer.');
    expect(restored.tools).toEqual([
      { sequence: 2, toolName: 'search_papers', status: 'completed' },
    ]);
    // Events are retained in order so the turn timeline can be rebuilt from them.
    expect(restored.events.map((candidate) => candidate.sequence)).toEqual([1, 2, 3, 4]);
  });
});
