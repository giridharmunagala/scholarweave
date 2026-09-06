import { describe, expect, it } from 'vitest';
import type { RunStreamEvent } from '../../api/events';
import {
  anchorRunsToItems,
  missingRunResponsesByUserIndex,
  turnMetrics,
} from './ChatPage';
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

describe('missingRunResponsesByUserIndex', () => {
  it('recovers model turns retained by a failed run but rolled out of session context', () => {
    const items = [
      item('user', 'First question'),
      item('user', 'Second question'),
      item('assistant', 'Latest answer'),
    ];
    const failed = run('run-1', 'First question', 'First pass.');
    failed.status = 'failed';
    failed.items = [
      {
        type: 'message_output_item',
        agent_name: 'Researcher',
        raw_item: {},
        content: 'Intermediate answer',
      },
      {
        type: 'message_output_item',
        agent_name: 'Researcher',
        raw_item: {},
        content: 'Rejected final answer',
      },
    ];
    const latest = run('run-2', 'Second question', 'Second pass.');

    expect(missingRunResponsesByUserIndex(items, [failed, latest])).toEqual(
      new Map([[0, ['Intermediate answer', 'Rejected final answer']]]),
    );
  });

  it('does not duplicate model turns already present in the transcript', () => {
    const items = [
      item('user', 'Question'),
      item('assistant', 'Intermediate answer'),
      item('assistant', 'Final answer'),
    ];
    const completed = run('run-1', 'Question', 'Reasoning');
    completed.items = [
      {
        type: 'message_output_item',
        agent_name: 'Researcher',
        raw_item: {},
        content: 'Intermediate answer',
      },
      {
        type: 'message_output_item',
        agent_name: 'Researcher',
        raw_item: {},
        content: 'Final answer',
      },
    ];

    expect(missingRunResponsesByUserIndex(items, [completed])).toEqual(new Map());
  });
});

describe('turnMetrics', () => {
  it('preserves historical estimate labels but rejects old wall-clock speeds', () => {
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
      promptRate: null,
      generationRate: null,
      durationSeconds: 5,
      modelCalls: null,
      usageComplete: null,
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
    candidate.usage = { performance: { input_tokens: 100, output_tokens: 20 } };
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
            timing_source: 'server',
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

  describe('authoritative telemetry', () => {
    it.each(['completed', 'failed', 'cancelled'] as const)(
      'prefers persisted final performance to an earlier epoch event when %s',
      (status) => {
        const candidate = run('run-1', 'Question', 'Reasoning');
        candidate.status = status;
        candidate.usage = {
          performance: {
            input_tokens: 500, output_tokens: 100, model_calls: 4,
            usage_complete: false,
            prompt_tokens_per_second: 999, generation_tokens_per_second: 888,
          },
        };
        const events: RunStreamEvent[] = [{
          sequence: 20, event_type: 'usage.updated', payload: {
            performance: {
              input_tokens: 200, output_tokens: 40, model_calls: 2,
              usage_complete: true, timing_source: 'server',
              prompt_tokens_per_second: 80, generation_tokens_per_second: 20,
            },
          },
        }];
        expect(turnMetrics(candidate, events)).toMatchObject({
          inputTokens: 500, outputTokens: 100, modelCalls: 4,
          usageComplete: false, promptRate: null, generationRate: null,
        });
      },
    );

    it('uses cumulative events for terminal runs without persisted performance', () => {
      const candidate = run('run-1', 'Question', 'Reasoning');
      const events: RunStreamEvent[] = [{
        sequence: 20, event_type: 'usage.updated',
        payload: { performance: { input_tokens: 500, output_tokens: 100 } },
      }];
      expect(turnMetrics(candidate, events)).toMatchObject({
        inputTokens: 500, outputTokens: 100,
      });
    });

    it('uses authoritative all-call totals and weighted rates without adding the delegate subset', () => {
      const candidate = run('run-1', 'Question', 'Reasoning');
      candidate.usage = {
        performance: {
          input_tokens: 1600,
          output_tokens: 400,
          model_calls: 5,
          delegated_input_tokens: 500,
          delegated_output_tokens: 100,
          delegated_model_calls: 2,
          timing_source: 'server',
          prompt_seconds: 10,
          generation_seconds: 8,
          timed_prompt_tokens: 1200,
          timed_output_tokens: 320,
          prompt_tokens_per_second: 120,
          generation_tokens_per_second: 40,
          usage_complete: false,
        },
      };
      expect(turnMetrics(candidate)).toMatchObject({
        inputTokens: 1600, outputTokens: 400, modelCalls: 5,
        promptRate: 120, generationRate: 40, usageComplete: false,
      });
    });

    it.each([undefined, 'client', 'wallclock'])('rejects speeds with timing source %s', (source) => {
      const candidate = run('run-1', 'Question', 'Reasoning');
      candidate.usage = {
        performance: {
          input_tokens: 100, output_tokens: 10, timing_source: source,
          prompt_tokens_per_second: 200, generation_tokens_per_second: 50,
        },
      };
      expect(turnMetrics(candidate)).toMatchObject({ promptRate: null, generationRate: null });
    });

    it('does not derive missing speeds from tokens, active seconds, or run duration', () => {
      const candidate = run('run-1', 'Question', 'Reasoning');
      candidate.usage = {
        performance: {
          usage_complete: false, model_calls: 2, timing_source: 'server',
          prompt_seconds: 1, generation_seconds: 1,
          timed_prompt_tokens: 100, timed_output_tokens: 50,
        },
      };
      expect(turnMetrics(candidate)).toMatchObject({
        inputTokens: null, outputTokens: null,
        promptRate: null, generationRate: null, usageComplete: false,
      });
    });

    it('does not present missing usage as zero or fabricate invalid speeds', () => {
      const candidate = run('run-1', 'Question', 'Reasoning');
      candidate.usage = {
        performance: {
          input_tokens: 0, output_tokens: 0, usage_complete: false, timing_source: 'server',
          prompt_tokens_per_second: -1, generation_tokens_per_second: NaN,
        },
      };
      expect(turnMetrics(candidate)).toMatchObject({
        inputTokens: null, outputTokens: null, promptRate: null, generationRate: null,
        usageComplete: false,
      });
    });

    it('preserves explicitly complete zero usage', () => {
      const candidate = run('run-1', 'Question', 'Reasoning');
      candidate.usage = {
        performance: { input_tokens: 0, output_tokens: 0, usage_complete: true },
      };
      expect(turnMetrics(candidate)).toMatchObject({
        inputTokens: 0, outputTokens: 0, usageComplete: true,
      });
    });
  });
});
