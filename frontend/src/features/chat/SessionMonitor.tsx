import { useEffect, useMemo, useState } from 'react';
import type {
  SessionObservabilitySummary, SessionPerformance, SessionRate, SessionTokenTotals, SessionWorker,
} from './sessionObservability';
import { useThrottledRates } from './useThrottledRates';

function rateValues(performance: SessionPerformance) {
  return { promptRate: performance.prefill.tokensPerSecond, generationRate: performance.generation.tokensPerSecond };
}

function displayedPerformance(
  performance: SessionPerformance,
  displayed: ReturnType<typeof rateValues> | undefined,
): SessionPerformance {
  if (!displayed) return performance;
  return {
    ...performance,
    prefill: { ...performance.prefill, tokensPerSecond: displayed.promptRate },
    generation: { ...performance.generation, tokensPerSecond: displayed.generationRate },
  };
}

function useOverviewRates(summary: SessionObservabilitySummary) {
  const sessionKey = `session:${summary.runs[0]?.id ?? 'empty'}`;
  const metrics = useMemo(() => new Map([
    [sessionKey, rateValues(summary.performance)],
    ...summary.runs.map((run) => [`run:${run.id}`, rateValues(run.performance)] as const),
  ]), [sessionKey, summary]);
  const displayed = useThrottledRates(metrics);
  return {
    session: displayedPerformance(summary.performance, displayed.get(sessionKey)),
    runs: new Map(summary.runs.map((run) => [
      run.id, displayedPerformance(run.performance, displayed.get(`run:${run.id}`)),
    ])),
  };
}

function count(value: number | null): string {
  return value === null ? 'Unavailable' : value.toLocaleString('en-US', { maximumFractionDigits: 0 });
}

function speed(value: SessionRate): string {
  return value.tokensPerSecond === null ? 'Unavailable'
    : `${value.tokensPerSecond.toLocaleString('en-US', { maximumFractionDigits: 1 })} tok/s`;
}

function seconds(value: number): string {
  return `${value.toLocaleString('en-US', { maximumFractionDigits: 1 })}s`;
}

function usageLabel(totals: SessionTokenTotals): string {
  if (totals.inputTokens === null && totals.outputTokens === null && totals.totalTokens === null) return 'Usage unavailable';
  if (totals.estimated) return totals.complete ? 'Estimated usage' : 'Estimated, incomplete usage';
  return totals.complete ? 'Reported usage' : 'Incomplete usage';
}

function coverage(rate: SessionRate, calls: number | null): string {
  if (rate.tokensPerSecond === null) return 'Server timing unavailable';
  return `${rate.complete ? 'Server timing' : 'Partial server timing'} · ${rate.timedCalls === null ? '?' : count(rate.timedCalls)}/${calls === null ? '?' : count(calls)} calls`;
}

/*
 * A read-out, not a control surface. Opening the observability panel is the
 * header's "Observe" button and nothing else, so these numbers are plain text:
 * anything here that could be clicked would be a third way to open the one
 * panel that Observe already owns.
 */
export function SessionStatusStrip({ summary }: { summary: SessionObservabilitySummary }) {
  const { session: performance } = useOverviewRates(summary);
  const active = summary.activeWorkers > 0 || summary.activeRuns > 0;
  return (
    <div className="session-status-strip meter-row" aria-label="Session usage and performance">
      <span className="meter" title={`${count(summary.totals.inputTokens)} input + ${count(summary.totals.outputTokens)} output tokens`}>
        <span>Session tokens</span> <strong>{summary.totals.estimated ? '~' : ''}{count(summary.totals.totalTokens)}</strong>
      </span>
      <span className="meter" title={coverage(performance.prefill, performance.modelCalls)}>
        <span>Prefill</span> <strong>{speed(performance.prefill)}</strong>
        {performance.prefill.tokensPerSecond !== null && !performance.prefill.complete && <small>partial</small>}
      </span>
      <span className="meter" title={coverage(performance.generation, performance.modelCalls)}>
        <span>Generation</span> <strong>{speed(performance.generation)}</strong>
        {performance.generation.tokensPerSecond !== null && !performance.generation.complete && <small>partial</small>}
      </span>
      <span className={`meter session-status-state${active ? ' active' : ''}`} data-active={active}>
        <strong>
          {summary.activeWorkers > 0
            ? `${summary.activeWorkers} worker${summary.activeWorkers === 1 ? '' : 's'} active`
            : summary.activeRuns > 0 ? 'Main agent active'
              : summary.completedWorkers > 0 ? `${summary.completedWorkers} worker tasks done` : 'Idle'}
        </strong>
      </span>
      {summary.workPlan.length > 0 ? (
        <span className="meter">
          <strong>{summary.workPlanCounts.completed}/{summary.workPlan.length} tasks</strong>
          {summary.workPlanCounts.blocked > 0 ? <small className="session-observability-error">{summary.workPlanCounts.blocked} blocked</small> : null}
        </span>
      ) : null}
      {!summary.totals.complete || summary.totals.estimated ? (
        <span className="session-observability-note">{usageLabel(summary.totals)}</span>
      ) : null}
    </div>
  );
}

function Metrics({ totals, performance }: { totals: SessionTokenTotals; performance: SessionPerformance }) {
  return (
    <>
      <dl className="session-metrics-grid">
        <div className="session-metric"><dt>Input tokens</dt><dd>{count(totals.inputTokens)}</dd></div>
        <div className="session-metric"><dt>Output tokens</dt><dd>{count(totals.outputTokens)}</dd></div>
        <div className="session-metric"><dt>Total tokens</dt><dd>{count(totals.totalTokens)}</dd></div>
        <div className="session-metric"><dt>Model calls</dt><dd>{count(performance.modelCalls)}{performance.modelCalls !== null && !performance.callsComplete ? ' (incomplete)' : ''}</dd></div>
        <div className="session-metric"><dt>Main calls</dt><dd>{count(performance.mainModelCalls)}</dd></div>
        <div className="session-metric"><dt>Delegated calls</dt><dd>{count(performance.delegatedModelCalls)}</dd></div>
        <div className="session-metric">
          <dt>Average prefill</dt><dd>{speed(performance.prefill)}</dd>
          <dd className="session-observability-note">{coverage(performance.prefill, performance.modelCalls)}</dd>
          {performance.prefill.tokensPerSecond !== null && (
            <dd className="session-observability-note">{count(performance.prefill.tokens)} tokens / {seconds(performance.prefill.seconds)} active time</dd>
          )}
        </div>
        <div className="session-metric">
          <dt>Average generation</dt><dd>{speed(performance.generation)}</dd>
          <dd className="session-observability-note">{coverage(performance.generation, performance.modelCalls)}</dd>
          {performance.generation.tokensPerSecond !== null && (
            <dd className="session-observability-note">{count(performance.generation.tokens)} tokens / {seconds(performance.generation.seconds)} active time</dd>
          )}
        </div>
      </dl>
      <p className="session-observability-note">{usageLabel(totals)}. Missing provider counts are not estimated.</p>
    </>
  );
}

function WorkerRow({ worker, now }: { worker: SessionWorker; now: number }) {
  const elapsed = worker.elapsedSeconds ?? (worker.status === 'running' && worker.startedAt !== null
    ? Math.max(0, (now - worker.startedAt) / 1000) : null);
  return (
    <li className="session-worker-row" data-status={worker.status}>
      <div className="session-worker-heading">
        <strong>{worker.name}</strong>
        <span className="session-status-badge" data-status={worker.status}>{worker.status}</span>
        {!worker.delegated && <small>Main</small>}
      </div>
      <p className="session-worker-request">
        {worker.request ? <>Assigned request{worker.requestTruncated ? ' (truncated)' : ''}: {worker.request}</>
          : 'Assigned request unavailable in this run history.'}
      </p>
      <p className="session-worker-phase">{worker.phase}{worker.model ? ` · ${worker.model}` : ''}</p>
      <p className="session-observability-note">
        {elapsed === null ? 'Elapsed time unavailable' : `${seconds(elapsed)} elapsed`}
        {' · '}{worker.completedTools} tools completed · {worker.completedModels} model calls completed
      </p>
      {worker.error && <p className="session-observability-error">{worker.error}</p>}
    </li>
  );
}

export function SessionOverview({ summary }: { summary: SessionObservabilitySummary }) {
  const rates = useOverviewRates(summary);
  const [now, setNow] = useState(Date.now);
  const running = summary.workers.some((worker) => worker.status === 'running');
  const liveWorkers = summary.workers.filter((worker) => worker.status === 'running');
  const visibleWorkers = liveWorkers.length ? liveWorkers : summary.workers.slice(-2);
  const visibleWorkerIds = new Set(visibleWorkers.map((worker) => worker.id));
  const workerHistory = summary.workers.filter((worker) => !visibleWorkerIds.has(worker.id));
  useEffect(() => {
    if (!running) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running]);

  return (
    <div className="session-observability" aria-label="Session overview">
      <section className="session-observability-section" aria-label="Session totals">
        <h3>Session totals</h3>
        <Metrics totals={summary.totals} performance={rates.session} />
        <p className="session-observability-note">
          Includes main, delegated, compaction and summary calls recorded in these runs, once.
          Rates use summed server token counts / summed active time, not wall-clock estimates or a mean of call speeds.
          {' '}Displayed rates refresh at most every five seconds; token counts update immediately.
          {summary.activeRuns > 0 ? ' Live totals include reported usage so far; in-flight calls may not have reported yet.' : ''}
          {' '}Explicitly deleted history cannot be included.
        </p>
      </section>

      <section className="session-observability-section" aria-label="Agents and workers">
        <h3>Agents &amp; workers</h3>
        <p className="session-observability-note">
          {summary.activeWorkers} active workers · {summary.completedWorkers} worker tasks completed.
          Elapsed time is not task completion.
        </p>
        {summary.workers.length ? (
          <ul className="session-worker-list">
            {visibleWorkers.map((worker) => <WorkerRow key={worker.id} worker={worker} now={now} />)}
          </ul>
        ) : <p className="session-observability-note">No agent activity recorded.</p>}
        {workerHistory.length > 0 ? (
          <details className="session-worker-history">
            <summary>Earlier agent activity · {workerHistory.length}</summary>
            <ul className="session-worker-list">
              {workerHistory.map((worker) => <WorkerRow key={worker.id} worker={worker} now={now} />)}
            </ul>
          </details>
        ) : null}
      </section>

      <section className="session-observability-section" aria-label="Deep Work plan">
        <h3>Deep Work plan</h3>
        {summary.workPlan.length ? (
          <>
            <p className="session-observability-note">
              {summary.workPlanCounts.pending} pending · {summary.workPlanCounts.in_progress} in progress
              {' · '}{summary.workPlanCounts.completed} completed · {summary.workPlanCounts.blocked} blocked
            </p>
            <ul className="session-work-plan">
              {summary.workPlan.map((item) => (
                <li className="session-work-item" key={`${item.runId}:${item.id}`} data-status={item.status}>
                  <span className="session-status-badge" data-status={item.status}>{item.status.replace('_', ' ')}</span>
                  <strong>{item.title}</strong>
                  {item.notes && <p>{item.notes}</p>}
                </li>
              ))}
            </ul>
          </>
        ) : <p className="session-observability-note">No work plan recorded. A conversation does not require one.</p>}
      </section>

      <section className="session-observability-section" aria-label="Run metrics">
        <h3>Runs · {summary.runs.length}</h3>
        <div className="session-run-list">
          {summary.runs.map((run, index) => (
            <details className="session-run-row" key={run.id}>
              <summary>
                <strong>Run {index + 1} · {run.label}</strong>{' '}
                <span className="session-status-badge" data-status={run.status}>{run.status}</span>{' '}
                <span>{count(run.totals.totalTokens)} tokens</span>
                {run.errors.length > 0 && <span className="session-observability-error"> · {run.errors.length} errors</span>}
              </summary>
              <p className="session-observability-note">Run ID: {run.id}</p>
              <Metrics totals={run.totals} performance={rates.runs.get(run.id) ?? run.performance} />
              {run.errors.length > 0 && (
                <ul className="session-observability-error" aria-label="Run errors">
                  {run.errors.map((error) => <li key={error}>{error}</li>)}
                </ul>
              )}
              {run.calls.length > 0 && (
                <details className="session-call-details">
                  <summary>Recorded model calls · {run.calls.length}</summary>
                  {run.calls.map((call) => (
                    <details key={call.id}>
                      <summary>{call.agentName} · {call.scope} · {call.model ?? 'Model unavailable'}{call.complete ? '' : ' · incomplete usage'}</summary>
                      <pre>{JSON.stringify(call.raw, null, 2)}</pre>
                    </details>
                  ))}
                </details>
              )}
            </details>
          ))}
        </div>
      </section>
    </div>
  );
}
