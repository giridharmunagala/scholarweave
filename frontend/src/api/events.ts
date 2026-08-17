import { apiUrl } from './client';

export interface RunStreamEvent {
  sequence: number;
  event_type: string;
  payload: Record<string, unknown>;
  created_at?: string | null;
}

export const RUN_EVENT_TYPES = [
  'run.started',
  'run.resumed',
  'run.completed',
  'run.failed',
  'run.cancelled',
  'run.paused',
  'run.item',
  'agent.updated',
  'agent.started',
  'agent.completed',
  'agent.failed',
  'agent.superseded',
  'model.stream',
  'model.started',
  'model.completed',
  'tool.started',
  'tool.completed',
  'tool.failed',
  'tool.result_truncated',
  'context.compacted',
  'builder.todos.updated',
  'extended.plan.updated',
  'extended.note.saved',
  'handoff.completed',
  'guardrail.result',
  'guardrail.tripwire',
  'approval.requested',
  'approval.resolved',
  'usage.updated',
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
