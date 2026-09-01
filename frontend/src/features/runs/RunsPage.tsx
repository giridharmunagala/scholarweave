import { useEffect, useState } from 'react';
import { subscribeToRun } from '../../api/events';
import { Link, useLocation, useNavigate } from '../../app/router';
import { Icon } from '../../shared/components/Icons';
import { EmptyState, ErrorNotice, Loading, PageHeader, Panel, StatusPill } from '../../shared/components/Ui';
import { runsApi, type Run, type RunItem } from './api';
import './runs.css';

export default function RunsPage() {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const id = pathname.split('/').filter(Boolean)[1];
  const [runs, setRuns] = useState<Run[]>([]);
  const [selected, setSelected] = useState<Run | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);

  const refresh = () =>
    Promise.all([runsApi.list(), id ? runsApi.get(id) : Promise.resolve(null)])
      .then(([list, detail]) => { setRuns(list); setSelected(detail); });
  useEffect(() => {
    refresh().catch(setError).finally(() => setLoading(false));
  }, [id]);
  useEffect(() => {
    if (!selected || ['completed', 'failed', 'cancelled', 'paused'].includes(selected.status)) return;
    const after = selected.events.reduce((max, event) => Math.max(max, event.sequence), -1);
    return subscribeToRun(selected.id, after, () => {
      runsApi.get(selected.id).then(setSelected).catch(setError);
    }, () => undefined);
  }, [selected?.id, selected?.status, selected?.events.length]);
  if (loading) return <Loading label="Loading runs…" />;

  if (!id) {
    return (
      <div className="page">
        <PageHeader title="Runs" description="Every agent run, with what it did, what it used and where it stopped." />
        {error ? <ErrorNotice error={error} /> : null}
        {runs.length ? (
          <Panel>
            <div className="table-wrap">
              <table><thead><tr><th>Agent</th><th>Status</th><th>Last agent</th><th>Started</th></tr></thead>
                <tbody>{runs.map((run) => <tr key={run.id}><td><Link to={`/runs/${run.id}`}>{run.agent_name}</Link></td><td><StatusPill value={run.status} /></td><td>{run.last_agent_name ?? '—'}</td><td>{new Date(run.created_at).toLocaleString()}</td></tr>)}</tbody>
              </table>
            </div>
          </Panel>
        ) : (
          <EmptyState
            icon="runs"
            title="No runs yet"
            description="Run an agent blueprint or send a builder message to create one."
            action={<Link className="button" to="/agents">Open agents</Link>}
          />
        )}
      </div>
    );
  }
  if (!selected) {
    return (
      <EmptyState
        icon="runs"
        title="Run not found"
        description="This run no longer exists."
        action={<Link className="button" to="/runs">Back to runs</Link>}
      />
    );
  }
  return (
    <div className="page">
      <PageHeader
        title={selected.agent_name}
        description={`Run ${selected.id}`}
        actions={
          <>
            <button className="button secondary" type="button" onClick={() => navigate('/runs')}>All runs</button>
            {['pending', 'running'].includes(selected.status) ? (
              <button className="button danger" type="button" onClick={() => void runsApi.cancel(selected.id).then(setSelected).catch(setError)}>
                <Icon name="close" size={15} />
                Cancel run
              </button>
            ) : null}
          </>
        }
      />
      {error ? <ErrorNotice error={error} /> : null}
      <div className="stat-grid">
        <div className="stat"><span>Status</span><strong><StatusPill value={selected.status} /></strong></div>
        <div className="stat"><span>Last agent</span><strong>{selected.last_agent_name ?? '—'}</strong></div>
        <div className="stat"><span>Run items</span><strong>{selected.items.length}</strong></div>
        <div className="stat"><span>Input tokens</span><strong>{usageValue(selected.usage, 'input_tokens')}</strong></div>
      </div>
      {selected.interruptions.filter((item) => item.status === 'pending').map((interruption) => (
        <Panel key={interruption.id} title={`Approval: ${interruption.tool_name ?? 'tool'}`} description="The SDK paused this RunState before executing the tool.">
          <pre className="code-block">{JSON.stringify(interruption.item, null, 2)}</pre>
          <div className="button-row"><button className="button" type="button" onClick={() => void runsApi.resolve(selected.id, interruption.id, true).then(setSelected).catch(setError)}>Approve and resume</button><button className="button danger" type="button" onClick={() => void runsApi.resolve(selected.id, interruption.id, false, 'Rejected by user.').then(setSelected).catch(setError)}>Reject</button></div>
        </Panel>
      ))}
      {selected.error ? <div className="notice error">{selected.error}</div> : null}
      <div className="split">
        <Panel title="Run item timeline" description="Semantic items emitted by the SDK Runner.">
          <div className="timeline">
            {selected.items.map((item, index) => <RunItemView item={item} index={index} key={index} />)}
            {!selected.items.length ? <p>No completed semantic items yet.</p> : null}
          </div>
          <div className="split">
            <Panel title="Supervisor epochs" description="Bounded execution segments and stop reasons.">
              <div className="event-log">
                {selected.epochs.map((epoch) => (
                  <details className="disclosure" key={epoch.id}>
                    <summary>
                      Epoch {epoch.epoch_index + 1} · {epoch.status} · {epoch.terminal_reason ?? 'running'}
                    </summary>
                    <pre>{JSON.stringify(epoch, null, 2)}</pre>
                  </details>
                ))}
                {!selected.epochs.length ? <p>No epoch has started.</p> : null}
              </div>
            </Panel>
            <Panel title="Tool attempts" description="Retries, failures, and uncertain write outcomes.">
              <div className="event-log">
                {selected.tool_attempts.map((attempt) => (
                  <details className="disclosure" key={attempt.id}>
                    <summary>
                      {attempt.catalog_id} · attempt {attempt.attempt} · {attempt.status}
                    </summary>
                    <pre>{JSON.stringify(attempt, null, 2)}</pre>
                  </details>
                ))}
                {!selected.tool_attempts.length ? <p>No application tool attempts.</p> : null}
              </div>
            </Panel>
          </div>
        </Panel>
        <Panel title="Usage and output">
          <div className="run-output">
            <div><span className="eyebrow">Final output</span><pre>{format(selected.final_output)}</pre></div>
            <div><span className="eyebrow">Usage</span><pre>{JSON.stringify(selected.usage, null, 2)}</pre></div>
          </div>
        </Panel>
      </div>
      <Panel title="Event log" description={`${selected.events.length} lifecycle events`}>
        <div className="event-log">
          {selected.events.map((event) => (
            <details className="disclosure" key={event.sequence}>
              <summary><code>{event.sequence}</code> {event.event_type}</summary>
              <pre>{JSON.stringify(event.payload, null, 2)}</pre>
            </details>
          ))}
        </div>
      </Panel>
    </div>
  );
}

function RunItemView({ item, index }: { item: RunItem; index: number }) {
  let detail: unknown = item.raw_item;
  if ('content' in item) detail = item.content;
  if ('output' in item) detail = item.output;
  if (item.type === 'handoff_output_item') detail = `${item.source_agent} → ${item.target_agent}`;
  return (
    <article className={`timeline-item item-${item.type}`}>
      <span className="timeline-index">{index + 1}</span>
      <div className="timeline-body">
        <span className="eyebrow">{item.type.split('_').join(' ')}</span>
        <strong>{item.agent_name}</strong>
        <pre>{format(detail)}</pre>
      </div>
    </article>
  );
}

function usageValue(usage: Record<string, unknown>, key: string) {
  return typeof usage[key] === 'number' ? String(usage[key]) : '—';
}
function format(value: unknown) {
  if (value == null) return '—';
  return typeof value === 'string' ? value : JSON.stringify(value, null, 2);
}
