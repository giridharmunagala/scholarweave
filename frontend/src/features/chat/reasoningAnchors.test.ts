import { describe, expect, it } from 'vitest';
import type { RunStreamEvent } from '../../api/events';
import { anchorRunsToItems, turnMetrics } from './ChatPage';
import { restoreChatStream } from './chatStream';
import type { ConversationDetail, Run } from './api';

function item(role: string, text: string): ConversationDetail['items'][number] {
  return { raw: null, role, text, type: 'message' };
}

function run(id: string, input: string, reasoning: string): Run {
  return {
    id,
    conversation_id: 'conversation-1',
    agent_name: 'Researcher',
    status: 'completed',
    input,
    final_output: null,
    last_agent_name: null,
    usage: {},
    error: null,
    cancel_requested: false,
    created_at: '2026-08-06T00:00:00Z',
    started_at: null,
    finished_at: null,
    items: [],
    events: [
      {
        sequence: 1,
        event_type: 'model.stream',
        payload: {
          raw_type: 'response.reasoning_summary_text.delta',
          delta: reasoning,
          snapshot: true,
        },
        created_at: '2026-08-06T00:00:00Z',
      },
    ],
  } as unknown as Run;
}

describe('anchorRunsToItems', () => {
  it('keeps every turn its own reasoning trace', () => {
    const items = [
      item('user', 'First question'),
      item('assistant', 'First answer'),
      item('user', 'Second question'),
      item('assistant', 'Second answer'),
    ];
    const runs = [
      run('run-1', 'First question', 'Thought about the first question.'),
      run('run-2', 'Second question', 'Thought about the second question.'),
    ];

    const anchors = anchorRunsToItems(items, runs);

    expect(anchors.byIndex.get(0)?.id).toBe('run-1');
    expect(anchors.byIndex.get(2)?.id).toBe('run-2');
    expect(anchors.responseByIndex.get(1)?.id).toBe('run-1');
    expect(anchors.responseByIndex.get(3)?.id).toBe('run-2');
    expect(anchors.anchored).toEqual(new Set(['run-1', 'run-2']));
    expect(restoreChatStream(anchors.byIndex.get(0)!.events).reasoning).toBe(
      'Thought about the first question.',
    );
  });

  it('leaves a run unanchored while its user turn is still optimistic', () => {
    const items = [item('user', 'First question'), item('assistant', 'First answer')];
    const runs = [
      run('run-1', 'First question', 'Done thinking.'),
      run('run-2', 'Follow-up question', 'Still thinking.'),
    ];

    const anchors = anchorRunsToItems(items, runs);

    expect(anchors.byIndex.size).toBe(1);
    expect(anchors.anchored.has('run-2')).toBe(false);
  });

  it('matches repeated prompts in chronological order', () => {
    const items = [
      item('user', 'Same question'),
      item('assistant', 'First answer'),
      item('user', 'Same question'),
      item('assistant', 'Second answer'),
    ];
    const runs = [
      run('run-1', 'Same question', 'First pass.'),
      run('run-2', 'Same question', 'Second pass.'),
    ];

    const anchors = anchorRunsToItems(items, runs);

    expect(anchors.byIndex.get(0)?.id).toBe('run-1');
    expect(anchors.byIndex.get(2)?.id).toBe('run-2');
  });

  it('falls back to chronological pairing when session text was transformed', () => {
    const items = [
      item('user', 'Normalized first question'),
      item('assistant', 'First answer'),
      item('user', 'Normalized second question'),
      item('assistant', 'Second answer'),
    ];
    const runs = [
      run('run-1', 'Original first question', 'First pass.'),
      run('run-2', 'Original second question', 'Second pass.'),
    ];

    const anchors = anchorRunsToItems(items, runs);

    expect(anchors.byIndex.get(0)?.id).toBe('run-1');
    expect(anchors.byIndex.get(2)?.id).toBe('run-2');
    expect(anchors.responseByIndex.get(3)?.id).toBe('run-2');
  });
});

describe('turnMetrics', () => {
  it('uses measured performance and preserves estimate labels', () => {
    const candidate = run('run-1', 'Question', 'Reasoning');
    candidate.started_at = '2026-08-06T00:00:00Z';
    candidate.finished_at = '2026-08-06T00:00:05Z';
    candidate.usage = {
      performance: {
        input_tokens: 120,
        output_tokens: 40,
        input_tokens_estimated: true,
        output_tokens_estimated: false,
        prompt_tokens_per_second: 60,
        generation_tokens_per_second: 20,
      },
    };

    expect(turnMetrics(candidate)).toEqual({
      inputTokens: 120,
      outputTokens: 40,
      inputEstimated: true,
      outputEstimated: false,
      promptRate: 60,
      generationRate: 20,
      durationSeconds: 5,
    });
  });

  it('does not present missing provider usage as zero tokens', () => {
    const candidate = run('run-1', 'Question', 'Reasoning');
    candidate.started_at = '2026-08-06T00:00:00Z';
    candidate.finished_at = '2026-08-06T00:00:02Z';
    candidate.usage = { input_tokens: 0, output_tokens: 0 };

    expect(turnMetrics(candidate)?.inputTokens).toBeNull();
    expect(turnMetrics(candidate)?.outputTokens).toBeNull();
    expect(turnMetrics(candidate)?.durationSeconds).toBe(2);
  });

  it('uses the latest streamed usage update while a run is active', () => {
    const candidate = run('run-1', 'Question', 'Reasoning');
    candidate.status = 'running';
    candidate.usage = {};
    const events: RunStreamEvent[] = [
      {
        sequence: 8,
        event_type: 'usage.updated',
        payload: {
          performance: {
            input_tokens: 240,
            output_tokens: 60,
            input_tokens_estimated: false,
            output_tokens_estimated: true,
            prompt_tokens_per_second: 80,
            generation_tokens_per_second: 30,
          },
        },
      },
    ];

    expect(turnMetrics(candidate, events)).toMatchObject({
      inputTokens: 240,
      outputTokens: 60,
      inputEstimated: false,
      outputEstimated: true,
      promptRate: 80,
      generationRate: 30,
    });
  });
});
