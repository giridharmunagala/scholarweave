import { describe, expect, it } from 'vitest';

import { RUN_EVENT_TYPES, subscribeToRun, type RunStreamEvent } from './events';

describe('run event subscription', () => {
  it('subscribes to the native run lifecycle events', () => {
    expect(RUN_EVENT_TYPES).toEqual(
      expect.arrayContaining([
        'run.started',
        'run.completed',
        'run.failed',
        'run.cancelled',
        'run.policy.rejected',
        'context.compacted',
        'context.sized',
        'context.prepared',
      ]),
    );
    expect(RUN_EVENT_TYPES).not.toEqual(
      expect.arrayContaining(['run.paused', 'guardrail.tripwire', 'approval.requested']),
    );
  });

  it('ignores replayed events after an EventSource reconnect', () => {
    const original = globalThis.EventSource;
    const fake = new FakeEventSource();
    globalThis.EventSource = class {
      onmessage = fake.onmessage;
      onerror = fake.onerror;
      constructor(_url: string) {
        return fake;
      }
    } as unknown as typeof EventSource;
    try {
      const received: RunStreamEvent[] = [];
      subscribeToRun('run-1', 5, (event) => received.push(event), () => undefined);

      fake.emit('model.stream', {
        sequence: 6,
        event_type: 'model.stream',
        payload: { raw_type: 'response.output_text.delta', delta: 'Hello' },
      });
      fake.emit('model.stream', {
        sequence: 6,
        event_type: 'model.stream',
        payload: { raw_type: 'response.output_text.delta', delta: 'Hello' },
      });
      fake.emit('model.stream', {
        sequence: 4,
        event_type: 'model.stream',
        payload: { raw_type: 'response.output_text.delta', delta: 'Old' },
      });

      expect(received.map((event) => event.sequence)).toEqual([6]);
    } finally {
      globalThis.EventSource = original;
    }
  });
});

class FakeEventSource {
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  private listeners = new Map<string, Array<(event: MessageEvent<string>) => void>>();

  addEventListener(type: string, listener: EventListenerOrEventListenerObject): void {
    const callback = listener as (event: MessageEvent<string>) => void;
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), callback]);
  }

  emit(type: string, event: RunStreamEvent): void {
    const message = { data: JSON.stringify(event) } as MessageEvent<string>;
    for (const listener of this.listeners.get(type) ?? []) listener(message);
  }

  close(): void {}
}
