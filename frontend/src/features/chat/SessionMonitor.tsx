import { useMemo } from 'react';
import type { SessionObservabilitySummary } from './sessionObservability';
import { useThrottledRates } from './useThrottledRates';

function count(value: number | null): string {
  return value === null ? 'Unavailable' : value.toLocaleString('en-US', { maximumFractionDigits: 0 });
}

function speed(value: number | null): string {
  return value === null ? 'Unavailable'
    : `${value.toLocaleString('en-US', { maximumFractionDigits: 1 })} tok/s`;
}

export function SessionOverview({ summary }: { summary: SessionObservabilitySummary }) {
  const { totals, performance, workPlan, workPlanCounts } = summary;
  const key = summary.runs[0]?.id ?? 'session';
  const metrics = useMemo(() => new Map([[key, {
    promptRate: performance.prefill.tokensPerSecond,
    generationRate: performance.generation.tokensPerSecond,
  }]]), [key, performance]);
  const rates = useThrottledRates(metrics).get(key);
  const errors = summary.runs.flatMap((run) => run.errors.map((error) => `${run.label}: ${error}`));
  return (
    <div className="session-observability" aria-label="Session overview">
      <details className="session-observability-section" aria-label="Session totals">
        <summary>
          Usage · {totals.totalTokens === null ? 'tokens unavailable' : `${totals.estimated ? '~' : ''}${count(totals.totalTokens)} tokens`}
          {' · '}{count(performance.modelCalls)} calls{!totals.complete ? ' · incomplete' : ''}
        </summary>
        <dl className="session-metrics-grid">
          <div><dt>Input tokens</dt><dd>{count(totals.inputTokens)}</dd></div>
          <div><dt>Output tokens</dt><dd>{count(totals.outputTokens)}</dd></div>
          <div><dt>Average prefill</dt><dd>{speed(rates?.promptRate ?? null)}</dd></div>
          <div><dt>Average generation</dt><dd>{speed(rates?.generationRate ?? null)}</dd></div>
        </dl>
        {performance.cache ? (
          <p className="session-observability-note" aria-label="Prompt cache">
            Prompt cache: {count(performance.cache.tokens)} input tokens reused
            {' · '}{count(performance.cache.reportedCalls)}
            {performance.callsComplete ? `/${count(performance.modelCalls)}` : ''} calls reported.
          </p>
        ) : null}
        <p className="session-observability-note">
          {totals.estimated ? 'Estimated' : 'Reported'}{totals.complete ? '' : ', incomplete'} usage.
          {' '}Rates use server active time{performance.prefill.complete && performance.generation.complete ? '.' : ' where available.'}
        </p>
      </details>
      {workPlan.length > 0 && (
        <details className="session-observability-section" aria-label="Work plan">
          <summary>
            Work plan · {workPlanCounts.completed}/{workPlan.length} completed
            {workPlanCounts.blocked > 0 ? ` · ${workPlanCounts.blocked} blocked` : ''}
          </summary>
          <ul className="session-work-plan">
            {workPlan.map((item) => (
              <li className="session-work-item" key={`${item.runId}:${item.id}`} data-status={item.status}>
                <span className="session-status-badge" data-status={item.status}>{item.status.replace('_', ' ')}</span>
                <strong>{item.title}</strong>
                {item.notes && <p>{item.notes}</p>}
              </li>
            ))}
          </ul>
        </details>
      )}
      {errors.length > 0 && (
        <details className="session-observability-section session-observability-error">
          <summary>Errors · {errors.length}</summary>
          <ul aria-label="Run errors">{errors.map((error, index) => <li key={index}>{error}</li>)}</ul>
        </details>
      )}
    </div>
  );
}
