import type { RunStreamEvent } from '../../api/events';
import { Icon } from '../../shared/components/Icons';
import { MarkdownViewer } from '../../shared/components/MarkdownViewer';
import type { TurnTimeline } from './chatTimeline';
import { ExtendedWorkPanel } from './ExtendedWorkPanel';

interface UsageMetrics {
  inputTokens: number | null;
  outputTokens: number | null;
  inputEstimated: boolean;
  outputEstimated: boolean;
}

export function RunInsightsPanel({
  status,
  events,
  timeline,
  metrics,
}: {
  status: string;
  events: readonly RunStreamEvent[];
  timeline: TurnTimeline;
  metrics: UsageMetrics | null;
}) {
  const agents = timeline.steps.filter((step) => step.kind === 'agent');
  const totalTokens =
    metrics?.inputTokens != null || metrics?.outputTokens != null
      ? (metrics.inputTokens ?? 0) + (metrics.outputTokens ?? 0)
      : null;

  return (
    <aside className="run-insights" aria-label="Run progress and context usage">
      <header className="run-insights-head">
        <div>
          <span className="eyebrow">Current request</span>
          <strong>Run insights</strong>
        </div>
        <span className={`run-insights-status ${status}`}>{status}</span>
      </header>

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

      <ExtendedWorkPanel events={events} sidebar />

      {agents.length ? (
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
                  {agent.status === 'running'
                    ? <span className="spinner tiny" aria-hidden="true" />
                    : <Icon name="check" size={13} />}
                  <span>
                    <strong>{agent.name}</strong>
                    <small>{agent.status === 'running' ? 'Working…' : 'Completed'}</small>
                  </span>
                  <Icon className="subagent-chevron" name="arrowRight" size={13} />
                </summary>
                <div className="subagent-output">
                  {agent.output == null ? (
                    <p>Waiting for focused findings…</p>
                  ) : typeof agent.output === 'string' ? (
                    <MarkdownViewer content={agent.output} />
                  ) : (
                    <pre>{formatPayload(agent.output)}</pre>
                  )}
                </div>
              </details>
            ))}
          </div>
        </section>
      ) : null}
    </aside>
  );
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
