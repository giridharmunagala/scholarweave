// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { ActivitySidebar, TurnTimelineView } from './TurnTimeline';

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

  it('shows the immutable effective prompt and tool contracts', async () => {
    await act(async () => {
      root.render(
        <ActivitySidebar
          open
          onClose={() => undefined}
          timelines={[{
            id: 'run-1',
            label: 'Run 1',
            timeline: {
              sources: [],
              toolCount: 0,
              agentCount: 0,
              reasoningSeconds: null,
              running: false,
              steps: [],
            },
            snapshot: {
              run_id: 'run-1',
              prompt_revision: 'revision-123',
              agents: [{
                id: 'researcher',
                name: 'Researcher',
                effective_instructions: 'Use primary evidence.',
              }],
              tools: [{
                name: 'search_web',
                description: 'Search public sources.',
                parameters_schema: { type: 'object' },
              }],
              activated_skills: [],
            },
          }]}
        />,
      );
    });

    const row = container.querySelector<HTMLElement>('.kind-prompt')!;
    await act(async () => row.querySelector<HTMLButtonElement>('.timeline-head')!.click());
    expect(container.textContent).toContain('Use primary evidence.');
    expect(container.textContent).toContain('search_web');
    expect(container.textContent).toContain('revision-123');
  });

  it('shows a delegated worker while it runs and exposes its completed handoff', async () => {
    const timeline = {
      sources: [],
      toolCount: 0,
      agentCount: 1,
      reasoningSeconds: null,
      running: false,
      steps: [{
        kind: 'agent' as const,
        id: 'worker-1',
        sequence: 1,
        completedSequence: 2,
        name: 'Focused Research Worker',
        output: '**Evidence handoff:** two sources agree.',
        status: 'completed' as const,
        seconds: 8,
      }],
    };
    await act(async () => {
      root.render(<TurnTimelineView timeline={timeline} />);
    });

    expect(container.textContent).toContain('Delegated worker');
    expect(container.textContent).toContain('Focused Research Worker');
    await act(async () => container.querySelector<HTMLButtonElement>('.kind-agent .timeline-head')!.click());
    expect(container.innerHTML).toContain('<strong>Evidence handoff:</strong>');
  });
});
