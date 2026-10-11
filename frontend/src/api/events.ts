import { apiUrl } from './client';
import type { components } from './schema.generated';

export const REVIEW_PROGRESS_EVENT = 'review.progress';

export function subscribeToReview(
  reviewId: string,
  onProgress: (review: components['schemas']['ReviewResponse']) => void,
  onError: () => void,
): () => void {
  const source = new EventSource(apiUrl(`/literature-reviews/${encodeURIComponent(reviewId)}/events`));
  source.addEventListener(REVIEW_PROGRESS_EVENT, (message) => {
    try {
      onProgress(JSON.parse((message as MessageEvent<string>).data) as components['schemas']['ReviewResponse']);
    } catch {
      onError();
    }
  });
  source.onerror = onError;
  return () => source.close();
}

export interface RunStreamEvent {
  sequence: number;
  event_type: string;
  payload: Record<string, unknown>;
  created_at?: string | null;
}

export const RUN_EVENT_TYPES = [
  'run.started',
  'run.completed',
  'run.failed',
  'run.cancelled',
  'run.recovered',
  'run.interrupted',
  'run.policy.rejected',
  'run.epoch.started',
  'run.epoch.completed',
  'run.item',
  'agent.started',
  'agent.stream',
  'agent.completed',
  'agent.failed',
  'agent.superseded',
  'model.stream',
  'model.started',
  'model.phase',
  'model.completed',
  'model.telemetry',
  'model.retry',
  'steering.queued',
  'steering.applied',
  'tool.started',
  'tool.completed',
  'tool.failed',
  'tool.attempt.started',
  'tool.attempt.completed',
  'tool.attempt.failed',
  'tool.result.stored',
  'usage.updated',
  'context.sized',
  'context.prepared',
  'context.compaction_started',
  'context.compacted',
  'context.compaction_failed',
  'prompt.snapshot',
  'skill.activated',
  'goal.plan.updated',
  'goal.blocked',
  'goal.completed',
] as const;

export function subscribeToRun(
  runId: string,
  after: number,
  onEvent: (event: RunStreamEvent) => void,
  onError: () => void,
): () => void {
  let cursor = after;
  const source = new EventSource(
    apiUrl(`/runs/${encodeURIComponent(runId)}/events?after=${after}`),
  );
  const deliver = (message: MessageEvent<string>) => {
    const event = JSON.parse(message.data) as RunStreamEvent;
    if (event.sequence <= cursor) return;
    cursor = event.sequence;
    onEvent(event);
  };
  source.onmessage = deliver;
  for (const eventType of RUN_EVENT_TYPES) {
    source.addEventListener(eventType, (message) => {
      deliver(message as MessageEvent<string>);
    });
  }
  source.onerror = onError;
  return () => source.close();
}
