import { useEffect, useState } from 'react';
import { formatStepDuration } from './chatTimeline';
import type { RunStage } from './runStages';
import type { SessionWorkItem } from './sessionObservability';

export function RunActivity({
  stages, workPlan = [], onOpenActivity,
}: {
  stages: RunStage[];
  workPlan?: SessionWorkItem[];
  onOpenActivity: () => void;
}) {
  const [now, setNow] = useState(Date.now);
  const active = stages.some((stage) => stage.status === 'running');
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  if (!stages.length && !workPlan.length) return null;
  // Keep active and unsuccessful work visible even in a long turn.
  const visible = stages.filter((stage, index) =>
    index >= stages.length - 7 || stage.status !== 'completed');
  const hidden = stages.length - visible.length;
  return (
    <section className="run-activity" aria-label="Turn activity">
      <header><strong>Activity</strong><button type="button" onClick={onOpenActivity}>Details</button></header>
      {workPlan.length ? (
        <>
          <p>Tasks {workPlan.filter((item) => item.status === 'completed').length}/{workPlan.length} complete</p>
          <ul aria-label="Turn tasks">
            {workPlan.map((item) => (
              <li key={item.id} className={`status-${item.status}`}>
                <span aria-hidden="true">{item.status === 'completed' ? '✓' : item.status === 'in_progress' ? '●' : item.status === 'blocked' ? '!' : '○'}</span>
                <span>{item.title}<small className="sr-only"> {item.status.replace('_', ' ')}</small></span>
              </li>
            ))}
          </ul>
        </>
      ) : null}
      <ul aria-label="Turn stages">
        {hidden ? <li className="earlier-stages">{hidden} earlier completed stage{hidden === 1 ? '' : 's'}</li> : null}
        {visible.map((stage) => {
          const end = stage.finishedAt ?? (stage.status === 'running' ? now : null);
          const elapsed = stage.startedAt !== null && end !== null
            ? formatStepDuration(Math.max(0, (end - stage.startedAt) / 1000)) || '0s' : null;
          return (
            <li key={stage.id} className={`status-${stage.status}`}>
              <span aria-hidden="true">{stage.status === 'running' ? '●' : stage.status === 'completed' ? '✓' : '!'}</span>
              <span>{stage.label}<small className={['running', 'completed'].includes(stage.status) ? 'sr-only' : ''}> {stage.status}</small></span>
              {elapsed ? <time aria-live="off">{elapsed}</time> : null}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
