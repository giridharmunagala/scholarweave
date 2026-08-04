import { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useParams } from '../lib/router';
import { useConfirm } from '../components/common/ConfirmDialog';
import { EmptyState } from '../components/common/EmptyState';
import { ErrorNotice } from '../components/common/ErrorNotice';
import { Icon } from '../components/common/Icon';
import { SkeletonList } from '../components/common/Skeleton';
import { StatusBadge } from '../components/common/StatusBadge';
import { toMessage, useToast } from '../components/common/Toast';
import { RunEventsPanel } from '../components/runs/RunEventsPanel';
import { api, runEventsUrl } from '../lib/api';
import { formatDateTime } from '../lib/format';
import type { NodeRunResponse, RunEventResponse, RunResponse } from '../types/api';

interface NodeProgress {
  phase: string;
  phase_label: string;
  completed_pages: number;
  total_pages: number;
  current_page?: number;
  average_seconds_per_page?: number;
  eta_seconds?: number;
}

function progressNumber(value: unknown): number | undefined {
  const number = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(number) ? number : undefined;
}

function formatDuration(seconds?: number): string {
  if (seconds == null) return 'Estimating after the first page…';
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))}s`;
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = Math.round(seconds % 60);
  return `${minutes}m ${remainingSeconds}s`;
}

function elapsed(run: RunResponse): string {
  const start = run.started_at || run.created_at;
  if (!start) return '—';
  const end = run.finished_at ? new Date(run.finished_at) : new Date();
  const seconds = (end.getTime() - new Date(start).getTime()) / 1000;
  return Number.isFinite(seconds) && seconds >= 0 ? formatDuration(seconds) : '—';
}

function mergeNodeRun(prev: NodeRunResponse[], patch: Partial<NodeRunResponse> & { node_path: string; node_type?: string }) {
  const index = prev.findIndex((item) => item.node_path === patch.node_path);
  if (index === -1) {
    return [
      ...prev,
      {
        id: patch.id || patch.node_path,
        run_id: patch.run_id || '',
        node_path: patch.node_path,
        node_id: patch.node_id || patch.node_path,
        node_type: patch.node_type || 'unknown',
        status: patch.status || 'pending',
        input: patch.input || {},
        output: patch.output || null,
        error: patch.error || null,
        started_at: patch.started_at || null,
        finished_at: patch.finished_at || null,
      },
    ];
  }
  const next = [...prev];
  next[index] = { ...next[index], ...patch } as NodeRunResponse;
  return next;
}

export function RunsPage() {
  const { runId } = useParams();
  const navigate = useNavigate();
  const toast = useToast();
  const confirm = useConfirm();
  const [runs, setRuns] = useState<RunResponse[]>([]);
  const [selectedRun, setSelectedRun] = useState<RunResponse | null>(null);
  const [events, setEvents] = useState<RunEventResponse[]>([]);
  const [tokenLog, setTokenLog] = useState<Record<string, string>>({});
  const [nodeProgress, setNodeProgress] = useState<Record<string, NodeProgress>>({});
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState('');
  const [streamState, setStreamState] = useState<'idle' | 'connecting' | 'live' | 'done' | 'lost'>('idle');
  const [deleting, setDeleting] = useState(false);

  const refreshRuns = async () => {
    const list = await api.listRuns();
    setRuns(list);
    return list;
  };

  const loadRun = async (id: string) => {
    setDetailLoading(true);
    try {
      setSelectedRun(await api.getRun(id));
      setError('');
    } catch (err) {
      setError(toMessage(err, 'Failed to load run'));
    } finally {
      setDetailLoading(false);
    }
  };

  useEffect(() => {
    let active = true;
    setLoading(true);
    refreshRuns()
      .then((list) => {
        if (!active) return;
        const nextId = runId || list[0]?.id;
        if (nextId) {
          void loadRun(nextId);
        }
      })
      .catch((err) => active && setError(toMessage(err, 'Failed to load runs')))
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, [runId]);

  useEffect(() => {
    if (!selectedRun) return;
    setEvents([]);
    setTokenLog({});
    setNodeProgress({});
    let closed = false;
    let terminalSeen = false;
    setStreamState('connecting');
    const source = new EventSource(runEventsUrl(selectedRun.id));
    source.onmessage = (message) => {
      try {
        const event = JSON.parse(message.data) as RunEventResponse;
        setEvents((prev) => [...prev, event]);
        if (event.event_type === 'node.token') {
          const nodePath = String(event.payload.node_path || 'node');
          const token = String(event.payload.token || '');
          setTokenLog((prev) => ({ ...prev, [nodePath]: `${prev[nodePath] || ''}${token}` }));
        }
        if (event.event_type === 'node.progress') {
          const nodePath = String(event.payload.node_path || 'node');
          setNodeProgress((prev) => ({
            ...prev,
            [nodePath]: {
              phase: String(event.payload.phase || 'working'),
              phase_label: String(event.payload.phase_label || 'Processing'),
              completed_pages: progressNumber(event.payload.completed_pages) ?? 0,
              total_pages: progressNumber(event.payload.total_pages) ?? 0,
              current_page: progressNumber(event.payload.current_page),
              average_seconds_per_page: progressNumber(event.payload.average_seconds_per_page),
              eta_seconds: progressNumber(event.payload.eta_seconds),
            },
          }));
        }
        setSelectedRun((prev) => {
          if (!prev) return prev;
          switch (event.event_type) {
            case 'run.started':
              return { ...prev, status: 'running' };
            case 'run.completed':
              return { ...prev, status: 'completed', output: event.payload.output, finished_at: event.created_at };
            case 'run.failed':
              return { ...prev, status: 'failed', error: String(event.payload.error || 'Run failed'), finished_at: event.created_at };
            case 'run.cancel_requested':
              return { ...prev, cancel_requested: true };
            case 'run.cancelled':
              return { ...prev, status: 'cancelled', finished_at: event.created_at };
            case 'node.started':
              return {
                ...prev,
                node_runs: mergeNodeRun(prev.node_runs, {
                  node_path: String(event.payload.node_path || ''),
                  node_type: String(event.payload.node_type || ''),
                  status: 'running',
                  input: (event.payload.input as Record<string, unknown>) || {},
                  started_at: event.created_at,
                }),
              };
            case 'node.completed':
              return {
                ...prev,
                node_runs: mergeNodeRun(prev.node_runs, {
                  node_path: String(event.payload.node_path || ''),
                  node_type: String(event.payload.node_type || ''),
                  status: 'completed',
                  finished_at: event.created_at,
                }),
              };
            case 'node.failed':
              return {
                ...prev,
                node_runs: mergeNodeRun(prev.node_runs, {
                  node_path: String(event.payload.node_path || ''),
                  node_type: String(event.payload.node_type || ''),
                  status: 'failed',
                  error: String(event.payload.error || 'Node failed'),
                  finished_at: event.created_at,
                }),
              };
            case 'node.skipped':
              return {
                ...prev,
                node_runs: mergeNodeRun(prev.node_runs, {
                  node_path: String(event.payload.node_path || ''),
                  node_type: String(event.payload.node_type || ''),
                  status: 'skipped',
                  input: (event.payload.input as Record<string, unknown>) || {},
                  error: String(event.payload.reason || 'Node was skipped'),
                  started_at: event.created_at,
                  finished_at: event.created_at,
                }),
              };
            default:
              return prev;
          }
        });
        if (['run.completed', 'run.failed', 'run.cancelled'].includes(event.event_type)) {
          terminalSeen = true;
          setStreamState('done');
          source.close();
          void Promise.all([refreshRuns(), api.getRun(selectedRun.id).then(setSelectedRun)]);
        } else {
          setStreamState('live');
        }
      } catch {
        setStreamState('lost');
      }
    };
    source.onerror = () => {
      if (!closed && !terminalSeen) {
        setStreamState('lost');
      }
    };
    return () => {
      closed = true;
      source.close();
    };
  }, [selectedRun?.id]);

  const visibleRun = useMemo(() => selectedRun ?? runs.find((run) => run.id === runId) ?? null, [runId, runs, selectedRun]);
  const topLevelNodeRuns = visibleRun?.node_runs.filter((node) => !node.node_path.includes('.')) ?? [];
  const completedNodes = topLevelNodeRuns.filter((node) => ['completed', 'failed', 'cancelled', 'skipped'].includes(node.status)).length;
  const totalNodes = visibleRun?.total_nodes || topLevelNodeRuns.length || 0;
  const progress = totalNodes ? Math.min(100, Math.round((completedNodes / totalNodes) * 100)) : 0;
  const runningNodes = visibleRun?.node_runs.filter((node) => node.status === 'running') ?? [];
  const isActive = visibleRun ? ['pending', 'running'].includes(visibleRun.status) : false;

  const streamLabel: Record<typeof streamState, string> = {
    idle: '',
    connecting: 'Connecting to the live event stream…',
    live: 'Streaming live updates',
    done: 'Run finished — stream closed',
    lost: 'Event stream disconnected. Reload the page to reconnect.',
  };

  const deleteSelectedRun = async () => {
    if (!visibleRun) return;
    const confirmed = await confirm({
      title: 'Delete this run?',
      description: `The ${visibleRun.workflow_name} run, its events, and its artifacts will be removed. This cannot be undone.`,
      confirmLabel: 'Delete run',
    });
    if (!confirmed) return;
    setDeleting(true);
    setError('');
    try {
      await api.deleteRun(visibleRun.id);
      const list = await refreshRuns();
      setSelectedRun(null);
      setEvents([]);
      setTokenLog({});
      setNodeProgress({});
      const next = list[0];
      navigate(next ? `/runs/${next.id}` : '/runs');
      if (next) {
        await loadRun(next.id);
      }
      toast.success('Run deleted');
    } catch (err) {
      const message = toMessage(err, 'Failed to delete run');
      setError(message);
      toast.failure('Delete failed', message);
    } finally {
      setDeleting(false);
    }
  };

  return (
    <div className="page-grid-two page-stack">
      <section className="panel stack gap-md">
        <div className="panel-subheader">
          <h3>History</h3>
          <button type="button" className="button subtle icon-only sm" title="Refresh runs" aria-label="Refresh runs" onClick={() => void refreshRuns()}>
            <Icon name="refresh" size={14} />
          </button>
        </div>
        {error ? <ErrorNotice message={error} /> : null}
        {loading ? <SkeletonList rows={4} /> : null}
        <div className="stack gap-sm scroll-area">
          {runs.map((run) => (
            <button
              type="button"
              key={run.id}
              className={`list-item ${visibleRun?.id === run.id ? 'active' : ''}`}
              onClick={() => {
                navigate(`/runs/${run.id}`);
                void loadRun(run.id);
              }}
            >
              <div className="list-item-main">
                <strong className="truncate">{run.workflow_name}</strong>
                <p>{formatDateTime(run.started_at || run.created_at)}</p>
              </div>
              <StatusBadge status={run.status} />
            </button>
          ))}
          {!loading && runs.length === 0 ? (
            <EmptyState
              icon="runs"
              title="No runs yet"
              description="Open the agent builder and press Run to see live execution here."
              action={
                <Link className="button primary sm" to="/agents">
                  <Icon name="workflow" size={12} />
                  Open the editor
                </Link>
              }
            />
          ) : null}
        </div>
      </section>

      <section className="stack gap-lg">
        <section className="panel">
          <div className="panel-header">
            <div>
              <p className="eyebrow">Run detail</p>
              <h2>{visibleRun?.workflow_name || 'Select a run'}</h2>
            </div>
            {visibleRun ? <StatusBadge status={visibleRun.status} /> : null}
          </div>
          {visibleRun ? (
            <div className="stack gap-md">
              <div className={`run-progress-card ${visibleRun.status}`}>
                <div className="run-progress-heading">
                  <div>
                    <span className="eyebrow">{visibleRun.status === 'running' ? 'Currently running' : 'Execution status'}</span>
                    <strong className="truncate">
                      {runningNodes.length
                        ? runningNodes.map((node) => node.node_path).join(', ')
                        : visibleRun.status === 'pending'
                          ? 'Waiting to start'
                          : visibleRun.status === 'completed'
                            ? 'Agent completed'
                            : visibleRun.status === 'failed'
                              ? 'Agent failed'
                              : 'Cancelled'}
                    </strong>
                  </div>
                  <span>
                    {completedNodes} / {totalNodes} nodes · {elapsed(visibleRun)}
                  </span>
                </div>
                <div
                  className="run-progress-track"
                  role="progressbar"
                  aria-label="Agent progress"
                  aria-valuemin={0}
                  aria-valuemax={totalNodes}
                  aria-valuenow={completedNodes}
                >
                  <span style={{ width: `${visibleRun.status === 'completed' ? 100 : progress}%` }} />
                </div>
                {streamState !== 'idle' && streamLabel[streamState] ? (
                  <p className="muted-text small">{streamLabel[streamState]}</p>
                ) : null}
              </div>

              <div className="button-row wrap">
                <Link className="button" to="/agents">
                  <Icon name="workflow" size={14} />
                  All agents
                </Link>
                <button
                  type="button"
                  className="button danger"
                  disabled={!isActive}
                  title={isActive ? 'Ask the engine to stop after the current node' : 'Only pending or running runs can be cancelled'}
                  onClick={async () => {
                    const next = await api.cancelRun(visibleRun.id);
                    setSelectedRun(next);
                    await refreshRuns();
                    toast.info('Cancellation requested', 'The run stops after the current node finishes.');
                  }}
                >
                  <Icon name="close" size={14} />
                  Cancel run
                </button>
                <button
                  type="button"
                  className="button subtle danger-text"
                  disabled={deleting || isActive}
                  title={isActive ? 'Finish or cancel the run before deleting it' : 'Delete this run and its artifacts'}
                  onClick={() => void deleteSelectedRun()}
                >
                  <Icon name="trash" size={14} />
                  {deleting ? 'Deleting…' : 'Delete'}
                </button>
              </div>

              {visibleRun.error ? <ErrorNotice title="Run error" message={visibleRun.error} /> : null}

              <dl className="definition-grid">
                <div>
                  <dt>Run ID</dt>
                  <dd className="path">{visibleRun.id}</dd>
                </div>
                <div>
                  <dt>Created</dt>
                  <dd>{formatDateTime(visibleRun.created_at)}</dd>
                </div>
                <div>
                  <dt>Started</dt>
                  <dd>{formatDateTime(visibleRun.started_at)}</dd>
                </div>
                <div>
                  <dt>Finished</dt>
                  <dd>{formatDateTime(visibleRun.finished_at)}</dd>
                </div>
              </dl>

              <div className="output-grid">
                <div className="field-stack">
                  <h4>Input</h4>
                  <pre className="preview-block">{JSON.stringify(visibleRun.input, null, 2)}</pre>
                </div>
                <div className="field-stack">
                  <h4>Output</h4>
                  <pre className="preview-block">{JSON.stringify(visibleRun.output, null, 2)}</pre>
                </div>
              </div>
            </div>
          ) : (
            <EmptyState icon="runs" title="Nothing selected" description="Pick a run from the history to inspect its live state." />
          )}
        </section>

        <section className="panel">
          <div className="panel-subheader">
            <h3>Node statuses</h3>
            {selectedRun?.node_runs.length ? <span className="tiny-tag">{selectedRun.node_runs.length}</span> : null}
          </div>
          {detailLoading ? <SkeletonList rows={3} /> : null}
          <div className="stack gap-sm">
            {selectedRun?.node_runs.map((nodeRun) => {
              const pageProgress = nodeProgress[nodeRun.node_path];
              const pagePercent = pageProgress?.total_pages
                ? Math.min(100, Math.round((pageProgress.completed_pages / pageProgress.total_pages) * 100))
                : 0;
              return (
                <article className={`node-run-item ${nodeRun.status}`} key={nodeRun.node_path}>
                  <div className="node-run-main">
                    <strong>{nodeRun.node_path}</strong>
                    <p className="muted-text">{nodeRun.node_type}</p>
                    {nodeRun.status === 'running' ? <p className="node-running-label">Running now…</p> : null}
                    {nodeRun.status === 'skipped' ? <p className="muted-text small">Skipped — {nodeRun.error || 'The node condition did not match.'}</p> : null}
                    {pageProgress ? (
                      <div className="node-page-progress">
                        <div>
                          <strong>{pageProgress.phase_label}</strong>
                          <span>
                            {pageProgress.total_pages
                              ? `${pageProgress.completed_pages} of ${pageProgress.total_pages} pages`
                              : 'Working…'}
                          </span>
                        </div>
                        <div className="run-progress-track">
                          <span style={{ width: `${pagePercent}%` }} />
                        </div>
                        <div className="node-page-progress-meta">
                          <span>ETA: {formatDuration(pageProgress.eta_seconds)}</span>
                          {pageProgress.average_seconds_per_page != null ? (
                            <span>Average: {formatDuration(pageProgress.average_seconds_per_page)} per page</span>
                          ) : null}
                        </div>
                      </div>
                    ) : null}
                    {nodeRun.error && nodeRun.status !== 'skipped' ? <p className="field-error">{nodeRun.error}</p> : null}
                    {Object.keys(nodeRun.input).length > 0 || nodeRun.output ? (
                      <details>
                        <summary>Input and output</summary>
                        {Object.keys(nodeRun.input).length > 0 ? <pre>{JSON.stringify(nodeRun.input, null, 2)}</pre> : null}
                        {nodeRun.output ? <pre>{JSON.stringify(nodeRun.output, null, 2)}</pre> : null}
                      </details>
                    ) : null}
                  </div>
                  <StatusBadge status={nodeRun.status} />
                </article>
              );
            })}
            {!detailLoading && !selectedRun?.node_runs.length ? (
              <p className="empty-state">Node runs appear here once execution starts.</p>
            ) : null}
          </div>
        </section>

        {selectedRun ? <RunEventsPanel events={events} tokenLog={tokenLog} /> : null}
      </section>
    </div>
  );
}
