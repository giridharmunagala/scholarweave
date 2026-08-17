// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { buildTurnTimeline, emptyTurnTimeline } from './chatTimeline';
import { RunInsightsPanel } from './RunInsightsPanel';

describe('RunInsightsPanel', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    localStorage.clear();
    (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  it('switches sidebar content with tabs and persists width changes', async () => {
    await act(async () => {
      root.render(
        <RunInsightsPanel
          status="running"
          events={[{
            sequence: 1,
            event_type: 'extended.plan.updated',
            payload: {
              tasks: [
                { id: 'evidence', title: 'Collect evidence', status: 'in_progress' },
                { id: 'compare', title: 'Compare results', status: 'pending' },
              ],
            },
          }]}
          timeline={emptyTurnTimeline}
          metrics={null}
        />,
      );
    });

    expect(container.querySelector('.context-usage-card')).not.toBeNull();
    expect(container.querySelector('.extended-work')).toBeNull();
    const planTab = Array.from(container.querySelectorAll<HTMLButtonElement>('[role="tab"]'))
      .find((button) => button.textContent?.includes('Plan'))!;
    await act(async () => planTab.click());
    expect(container.querySelector('section.extended-work.sidebar')).not.toBeNull();
    expect(container.querySelector('.extended-work.sidebar > summary')).toBeNull();
    expect(container.querySelector('.extended-work-status[aria-label="In progress"]')).not.toBeNull();
    expect(container.querySelector('.context-usage-card')).toBeNull();

    const panel = container.querySelector<HTMLElement>('.run-insights')!;
    const resizer = container.querySelector<HTMLElement>('[aria-label="Resize run insights"]')!;
    expect(container.querySelector('.run-insights-size-controls')).toBeNull();
    await act(async () => {
      resizer.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true }));
    });
    expect(panel.style.width).toBe('336px');
    expect(localStorage.getItem('scholarweave:run-insights-width')).toBe('336');
  });

  it('shows recent reasoning, tool use, and the final sub-agent response', async () => {
    const events = [
      { sequence: 1, event_type: 'agent.started', payload: { agent_name: 'Coordinator' } },
      { sequence: 2, event_type: 'agent.started', payload: { agent_name: 'Researcher' } },
      {
        sequence: 3,
        event_type: 'model.stream',
        payload: {
          raw_type: 'response.reasoning_summary_text.delta',
          delta: 'Comparing the available evidence.',
        },
      },
      { sequence: 4, event_type: 'tool.started', payload: { tool_name: 'search_web' } },
      { sequence: 5, event_type: 'tool.completed', payload: { tool_name: 'search_web' } },
      {
        sequence: 6,
        event_type: 'agent.completed',
        payload: { agent_name: 'Researcher', output: 'Two sources support the result.' },
      },
    ];
    await act(async () => {
      root.render(
        <RunInsightsPanel
          status="completed"
          events={events}
          timeline={buildTurnTimeline(events, { settled: true })}
          metrics={null}
        />,
      );
    });

    const agentsTab = Array.from(container.querySelectorAll<HTMLButtonElement>('[role="tab"]'))
      .find((button) => button.textContent?.includes('Agents'))!;
    await act(async () => agentsTab.click());
    expect(agentsTab.getAttribute('aria-selected')).toBe('true');
    const agent = container.querySelector<HTMLDetailsElement>('.subagent-insights-list details')!;
    await act(async () => {
      agent.open = true;
      agent.dispatchEvent(new Event('toggle', { bubbles: false }));
    });

    expect(agent.textContent).toContain('Comparing the available evidence.');
    expect(agent.textContent).toContain('Search web');
    expect(agent.textContent).toContain('Final response');
    expect(agent.textContent).toContain('Two sources support the result.');

    await act(async () => {
      root.render(
        <RunInsightsPanel
          status="completed"
          events={events}
          timeline={buildTurnTimeline(events, { settled: true })}
          metrics={null}
          hidden
        />,
      );
    });
    expect(container.querySelector('.run-insights')?.hasAttribute('hidden')).toBe(true);

    await act(async () => {
      root.render(
        <RunInsightsPanel
          status="completed"
          events={events}
          timeline={buildTurnTimeline(events, { settled: true })}
          metrics={null}
        />,
      );
    });
    expect(agentsTab.getAttribute('aria-selected')).toBe('true');
  });
});
