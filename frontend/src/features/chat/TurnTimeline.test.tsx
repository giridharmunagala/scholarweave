// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ActivitySidebar, LiveActivityBar, TurnTimelineView } from './TurnTimeline';
import { buildTurnTimeline } from './chatTimeline';

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
    vi.useRealTimers();
  });

  it('restores run elapsed time and offers details before any tool completes', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-09-06T10:02:30Z'));
    const open = vi.fn();
    await act(async () => {
      root.render(<LiveActivityBar
        activity={{ phase: 'thinking', label: 'Thinking', detail: null, completedSteps: 0 }}
        startedAt="2026-09-06T10:00:00Z"
        onOpenActivity={open}
      />);
    });
    expect(container.querySelector('.live-activity-elapsed')?.textContent).toBe('2m 30s elapsed');
    await act(async () => {
      container.querySelector<HTMLButtonElement>('.live-activity-open')!.click();
      vi.advanceTimersByTime(1000);
    });
    expect(open).toHaveBeenCalledOnce();
    expect(container.querySelector('.live-activity-elapsed')?.textContent).toBe('2m 31s elapsed');
  });

  it('keeps a new active tool outside collapsed groups after many completed calls', () => {
    const events = Array.from({ length: 12 }, (_, index) => ([
      { sequence: index * 2, event_type: 'tool.started', payload: { tool_name: 'read_source', tool_call_id: `${index}` } },
      { sequence: index * 2 + 1, event_type: 'tool.completed', payload: { tool_name: 'read_source', tool_call_id: `${index}` } },
    ])).flat();
    events.push({ sequence: 25, event_type: 'tool.started', payload: { tool_name: 'search_web', tool_call_id: 'new' } });
    act(() => root.render(<TurnTimelineView timeline={buildTurnTimeline(events)} />));
    expect(container.querySelector('.turn-timeline > .kind-tool.live')?.textContent).toContain('Search web');
    expect(container.querySelector('.timeline-row.group')?.textContent).toContain('12 tool calls');
  });

  it('ticks the current phase and total timer independently while preserving trace headlines', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-09-06T10:00:10Z'));
    const activity = {
      phase: 'waiting' as const, label: 'Waiting for model', detail: null,
      completedSteps: 2, startedAt: Date.parse('2026-09-06T10:00:08Z'),
    };
    act(() => root.render(<LiveActivityBar activity={activity} startedAt="2026-09-06T10:00:00Z" headlines={['Read paper', 'Search web']} />));
    act(() => vi.advanceTimersByTime(3000));
    expect(container.querySelector('.live-activity-elapsed')?.textContent).toBe('5s on this step13s elapsed');
    expect(container.querySelector('[aria-label="Recent trace steps"]')?.textContent).toContain('Read paper');
    act(() => root.render(<LiveActivityBar activity={{
      ...activity, phase: 'writing', label: 'Writing the answer', startedAt: Date.now(),
    }} startedAt="2026-09-06T10:00:00Z" />));
    expect(container.querySelector('.live-activity-elapsed')?.textContent).toBe('0s on this step13s elapsed');
    expect(container.querySelector('[role="status"]')?.textContent).not.toContain('13s');
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

  it('opens directly to the full trace and shows live worker tools inside the delegation', async () => {
    const timeline = buildTurnTimeline([
      { sequence: 1, event_type: 'agent.started', payload: { agent_name: 'Coordinator', invocation_id: 'root' } },
      { sequence: 2, event_type: 'tool.started', payload: { tool_name: 'focused_research_worker', tool_call_id: 'delegate' } },
      { sequence: 3, event_type: 'agent.started', payload: {
        agent_name: 'Worker', invocation_id: 'worker', delegated: true, parent_tool_call_id: 'delegate',
        assignment: 'Inspect the method', assignment_truncated: true,
      } },
      { sequence: 4, event_type: 'tool.started', payload: {
        agent_name: 'Worker', invocation_id: 'worker', tool_name: 'read_paper', tool_call_id: 'read',
      } },
    ]);
    await act(async () => root.render(<ActivitySidebar
      open onClose={() => undefined}
      overview={<details><summary>Usage</summary>Metrics</details>}
      timelines={[{ id: 'run', label: 'Run 1', timeline }]}
    />));
    expect(container.querySelector('.session-trace')?.tagName).toBe('SECTION');
    const delegation = container.querySelector('.session-trace .turn-timeline > .kind-tool')!;
    const worker = delegation.querySelector('.timeline-children > .kind-agent')!;
    expect(worker.querySelector<HTMLButtonElement>(':scope > button')?.getAttribute('aria-expanded')).toBe('true');
    expect(worker.querySelector('.turn-timeline .kind-tool')?.textContent).toContain('Read paper');
    expect(worker.textContent).toContain('Using Read paper');
    expect(worker.querySelector('details > summary')?.textContent).toBe('Assignment (truncated)');
  });
});
