import {
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
} from 'react';
import type { RunStreamEvent } from '../../api/events';
import { Icon } from '../../shared/components/Icons';
import { MarkdownViewer } from '../../shared/components/MarkdownViewer';
import type { AgentStep, TurnStep, TurnTimeline } from './chatTimeline';
import { ExtendedWorkPanel } from './ExtendedWorkPanel';

type InsightTab = 'overview' | 'plan' | 'agents';

interface UsageMetrics {
  inputTokens: number | null;
  outputTokens: number | null;
  inputEstimated: boolean;
  outputEstimated: boolean;
}

const INSIGHTS_WIDTH_KEY = 'scholarweave:run-insights-width';
const MIN_INSIGHTS_WIDTH = 260;
const MAX_INSIGHTS_WIDTH = 640;

export function RunInsightsPanel({
  status,
  events,
  timeline,
  metrics,
  hidden = false,
}: {
  status: string;
  events: readonly RunStreamEvent[];
  timeline: TurnTimeline;
  metrics: UsageMetrics | null;
  hidden?: boolean;
}) {
  const [activeTab, setActiveTab] = useState<InsightTab>('overview');
  const [width, setWidth] = useState(readInsightsWidth);
  const dragCleanupRef = useRef<(() => void) | null>(null);
  const agents = timeline.steps.filter((step) => step.kind === 'agent');
  const taskCount = latestTaskCount(events);
  const hasPlan = taskCount > 0;
  const totalTokens =
    metrics?.inputTokens != null || metrics?.outputTokens != null
      ? (metrics.inputTokens ?? 0) + (metrics.outputTokens ?? 0)
      : null;

  useEffect(() => {
    if ((activeTab === 'plan' && !hasPlan) || (activeTab === 'agents' && !agents.length)) {
      setActiveTab('overview');
    }
  }, [activeTab, agents.length, hasPlan]);

  useEffect(() => {
    try {
      localStorage.setItem(INSIGHTS_WIDTH_KEY, String(width));
    } catch {
      /* Persisting the panel width is best effort. */
    }
  }, [width]);

  useEffect(() => () => dragCleanupRef.current?.(), []);

  const resizeBy = (amount: number) => setWidth((current) => clampWidth(current + amount));
  const startResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    dragCleanupRef.current?.();
    const startX = event.clientX;
    const startWidth = width;
    const move = (nextEvent: PointerEvent) => {
      setWidth(clampWidth(startWidth + startX - nextEvent.clientX));
    };
    const cleanup = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', cleanup);
      dragCleanupRef.current = null;
    };
    dragCleanupRef.current = cleanup;
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', cleanup);
  };

  return (
    <aside
      className="run-insights"
      aria-label="Run progress and context usage"
      hidden={hidden}
      style={{ width: `${width}px` } as CSSProperties}
    >
      <div
        className="run-insights-resizer"
        role="separator"
        aria-label="Resize run insights"
        aria-orientation="vertical"
        aria-valuemin={MIN_INSIGHTS_WIDTH}
        aria-valuemax={MAX_INSIGHTS_WIDTH}
        aria-valuenow={width}
        tabIndex={0}
        onPointerDown={startResize}
        onKeyDown={(event) => {
          if (event.key === 'ArrowLeft') resizeBy(16);
          else if (event.key === 'ArrowRight') resizeBy(-16);
          else return;
          event.preventDefault();
        }}
      />
      <header className="run-insights-head">
        <div>
          <span className="eyebrow">Current request</span>
          <strong>Run insights</strong>
        </div>
        <div className="run-insights-head-actions">
          <span className={`run-insights-status ${status}`}>{status}</span>
        </div>
      </header>

      <div className="run-insights-tabs" role="tablist" aria-label="Run insight views">
        <InsightTabButton active={activeTab === 'overview'} tab="overview" onSelect={setActiveTab}>
          Overview
        </InsightTabButton>
        {hasPlan ? (
          <InsightTabButton active={activeTab === 'plan'} tab="plan" onSelect={setActiveTab}>
            Plan <span>{taskCount}</span>
          </InsightTabButton>
        ) : null}
        {agents.length ? (
          <InsightTabButton active={activeTab === 'agents'} tab="agents" onSelect={setActiveTab}>
            Agents <span>{agents.length}</span>
          </InsightTabButton>
        ) : null}
      </div>

      <div className="run-insights-content">
        {activeTab === 'overview' ? (
          <section className="context-usage-card" aria-label="Token usage">
            <div className="context-usage-title">
              <Icon name="runs" size={15} />
              <strong>Context usage</strong>
            </div>
            {metrics?.inputTokens != null ? (
              <div className="context-usage-total">
                <strong>{estimatedPrefix(metrics.inputEstimated)}{formatTokens(metrics.inputTokens)}</strong>
                <span>context tokens consumed</span>
              </div>
            ) : (
              <p>{isTerminal(status) ? 'Token usage was not reported.' : 'Counting tokens…'}</p>
            )}
            {totalTokens != null ? (
              <div className="context-usage-breakdown">
                <span>
                  Generated
                  <strong>{estimatedPrefix(metrics?.outputEstimated)}{formatTokens(metrics?.outputTokens ?? 0)}</strong>
                </span>
                <span>
                  Total
                  <strong>
                    {estimatedPrefix(metrics?.inputEstimated || metrics?.outputEstimated)}
                    {formatTokens(totalTokens)}
                  </strong>
                </span>
              </div>
            ) : null}
          </section>
        ) : null}

        {activeTab === 'plan' ? <ExtendedWorkPanel events={events} sidebar /> : null}

        {activeTab === 'agents' ? (
          <section className="subagent-insights">
            <header>
              <Icon name="agents" size={15} />
              <strong>Sub-agent outputs</strong>
              <span>{agents.length}</span>
            </header>
            <div className="subagent-insights-list">
              {agents.map((agent) => (
                <details key={agent.id} open={agent.status === 'running'}>
                  <summary>
                    {agent.status === 'running' ? (
                      <span className="spinner tiny" aria-hidden="true" />
                    ) : (
                      <Icon
                        name={agent.status === 'completed' ? 'check' : 'close'}
                        size={13}
                      />
                    )}
                    <span>
                      <strong>{agent.name}</strong>
                      <small>{agentStatusLabel(agent.status)}</small>
                    </span>
                    <Icon className="subagent-chevron" name="arrowRight" size={13} />
                  </summary>
                  <div className="subagent-output">
                    <SubagentActivity agent={agent} steps={timeline.steps} />
                    {agent.output != null ? (
                      <section className="subagent-final">
                        <strong>Final response</strong>
                        {typeof agent.output === 'string' ? (
                          <MarkdownViewer content={agent.output} />
                        ) : (
                          <pre>{formatPayload(agent.output)}</pre>
                        )}
                      </section>
                    ) : null}
                  </div>
                </details>
              ))}
            </div>
          </section>
        ) : null}
      </div>
    </aside>
  );
}

function SubagentActivity({ agent, steps }: { agent: AgentStep; steps: TurnStep[] }) {
  const activity = steps.filter(
    (step): step is Exclude<TurnStep, AgentStep> =>
      step.kind !== 'agent'
      && step.sequence > agent.sequence
      && (agent.completedSequence === null || step.sequence <= agent.completedSequence),
  );
  if (!activity.length) {
    return <p>{agent.status === 'running' ? 'Waiting for live activity…' : 'No intermediate activity was reported.'}</p>;
  }

  return (
    <section className="subagent-activity" aria-label={`${agent.name} activity`}>
      <strong>Recent activity</strong>
      <div>
        {activity.slice(-5).map((step) => (
          <div
            className={`subagent-activity-row ${step.kind}${step.kind === 'tool' ? ` ${step.status}` : ''}`}
            key={step.id}
          >
            {step.kind === 'reasoning' ? (
              <>
                <Icon name="bulb" size={13} />
                <span><small>Reasoning</small>{activityPreview(step.text)}</span>
              </>
            ) : step.kind === 'tool' ? (
              <>
                {step.status === 'running'
                  ? <span className="spinner tiny" aria-hidden="true" />
                  : <Icon name={step.status === 'failed' ? 'close' : 'tools'} size={13} />}
                <span><small>Tool</small>{step.label}</span>
              </>
            ) : (
              <>
                <Icon name="agents" size={13} />
                <span><small>Handoff</small>{step.from} to {step.to}</span>
              </>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}

function activityPreview(value: string): string {
  const collapsed = value.replace(/\s+/g, ' ').trim();
  return collapsed.length > 160 ? `${collapsed.slice(0, 159)}…` : collapsed;
}

function agentStatusLabel(status: AgentStep['status']): string {
  if (status === 'running') return 'Working…';
  if (status === 'completed') return 'Completed';
  if (status === 'superseded') return 'Superseded';
  return 'Failed';
}

function InsightTabButton({
  active,
  tab,
  onSelect,
  children,
}: {
  active: boolean;
  tab: InsightTab;
  onSelect: (tab: InsightTab) => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      className={active ? 'active' : ''}
      onClick={() => onSelect(tab)}
    >
      {children}
    </button>
  );
}

function latestTaskCount(events: readonly RunStreamEvent[]): number {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event.event_type === 'extended.plan.updated' && Array.isArray(event.payload.tasks)) {
      return event.payload.tasks.length;
    }
  }
  return 0;
}

function readInsightsWidth(): number {
  try {
    return clampWidth(Number(localStorage.getItem(INSIGHTS_WIDTH_KEY)) || 320);
  } catch {
    return 320;
  }
}

function clampWidth(value: number): number {
  return Math.min(MAX_INSIGHTS_WIDTH, Math.max(MIN_INSIGHTS_WIDTH, Math.round(value)));
}

function isTerminal(status: string): boolean {
  return ['completed', 'failed', 'cancelled', 'paused'].includes(status);
}

function estimatedPrefix(estimated: boolean | undefined): string {
  return estimated ? '~' : '';
}

export function formatTokens(value: number): string {
  return new Intl.NumberFormat(undefined, {
    notation: value >= 1000 ? 'compact' : 'standard',
    maximumFractionDigits: value >= 1000 ? 1 : 0,
  }).format(value);
}

function formatPayload(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}
