// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { TurnTimelineView } from './TurnTimeline';

describe('TurnTimelineView', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  it('marks only the failed tool call red inside a mixed group', async () => {
    await act(async () => {
      root.render(
        <TurnTimelineView
          timeline={{
            sources: [],
            toolCount: 2,
            agentCount: 0,
            reasoningSeconds: null,
            running: false,
            steps: [
              {
                kind: 'tool',
                id: 'tool-1',
                sequence: 1,
                name: 'search_web',
                label: 'Search web',
                query: null,
                detail: null,
                args: null,
                result: null,
                status: 'failed',
                seconds: 1,
                sources: [],
              },
              {
                kind: 'tool',
                id: 'tool-2',
                sequence: 2,
                name: 'read_page',
                label: 'Read page',
                query: null,
                detail: null,
                args: null,
                result: null,
                status: 'completed',
                seconds: 1,
                sources: [],
              },
            ],
          }}
        />,
      );
    });

    const group = container.querySelector<HTMLElement>('.timeline-row.group')!;
    expect(group.classList.contains('failed')).toBe(false);
    await act(async () => group.querySelector<HTMLButtonElement>(':scope > .timeline-head')!.click());
    expect(container.querySelectorAll('.timeline-detail.group .timeline-row.failed')).toHaveLength(1);
  });
});
