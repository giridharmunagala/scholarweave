import { useEffect, useState } from 'react';
import { request } from '../../api/client';
import type { components } from '../../api/schema.generated';
import { Link } from '../../app/router';
import { Icon, type IconName } from '../../shared/components/Icons';
import { EmptyState, ErrorNotice, Loading, PageHeader, Panel, StatusPill } from '../../shared/components/Ui';
import './dashboard.css';

type Health = components['schemas']['HealthResponse'];
type Agent = components['schemas']['AgentResponse'];
type Run = components['schemas']['RunResponse'];
type Document = components['schemas']['DocumentResponse'];

interface Workspace {
  health: Health;
  agents: Agent[];
  runs: Run[];
  documents: Document[];
}

const QUICK_ACTIONS: Array<{ to: string; label: string; description: string; icon: IconName }> = [
  { to: '/agents/new', label: 'Compose an agent', description: 'Wire agents, tools and handoffs on the canvas.', icon: 'agents' },
  { to: '/chat', label: 'Build by chat', description: 'Describe what you need and let the builder draft it.', icon: 'builder' },
  { to: '/papers', label: 'Add a paper', description: 'Ingest a PDF and index it for retrieval.', icon: 'papers' },
];

export default function DashboardPage() {
  const [data, setData] = useState<Workspace | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    Promise.all([
      request<Health>('/health'),
      request<Agent[]>('/agents'),
      request<Run[]>('/runs'),
      request<Document[]>('/documents'),
    ])
      .then(([health, agents, runs, documents]) => setData({ health, agents, runs, documents }))
      .catch(setError);
  }, []);

  if (!data && !error) return <Loading label="Loading workspace…" />;

  return (
    <div className="page">
      <PageHeader
        eyebrow="Research agent workspace"
        title="Overview"
        description="Compose, run and inspect agents directly on the OpenAI Agents SDK."
        actions={
          <Link className="button" to="/agents/new">
            <Icon name="plus" size={16} />
            New agent
          </Link>
        }
      />
      {error ? <ErrorNotice error={error} /> : null}
      {data ? (
        <>
          <div className="stat-grid">
            <Stat icon="shield" label="SDK version" value={data.health.sdk_version} />
            <Stat icon="agents" label="Saved agents" value={data.agents.length} to="/agents" />
            <Stat icon="runs" label="Runs" value={data.runs.length} to="/runs" />
            <Stat icon="papers" label="Papers" value={data.documents.length} to="/papers" />
          </div>

          <div className="quick-grid">
            {QUICK_ACTIONS.map((action) => (
              <Link className="quick-action" to={action.to} key={action.to}>
                <span className="quick-icon">
                  <Icon name={action.icon} size={18} />
                </span>
                <span className="quick-body">
                  <strong>{action.label}</strong>
                  <small>{action.description}</small>
                </span>
                <Icon name="arrowRight" size={16} className="quick-arrow" />
              </Link>
            ))}
          </div>

          <div className="split">
            <Panel
              title="Recent runs"
              description="The latest SDK Runner executions."
              actions={
                data.runs.length ? (
                  <Link className="button ghost small" to="/runs">
                    View all
                  </Link>
                ) : null
              }
            >
              {data.runs.length ? (
                <div className="stack-tight">
                  {data.runs.slice(0, 6).map((run) => (
                    <Link className="list-row" key={run.id} to={`/runs/${run.id}`}>
                      <span className="list-main">
                        <strong className="truncate">{run.agent_name}</strong>
                        <small>{new Date(run.created_at).toLocaleString()}</small>
                      </span>
                      <StatusPill value={run.status} />
                    </Link>
                  ))}
                </div>
              ) : (
                <EmptyState
                  icon="runs"
                  title="No runs yet"
                  description="Run an agent from the canvas or send a builder message to see execution details here."
                  action={
                    <Link className="button" to="/agents/new">
                      Create your first agent
                    </Link>
                  }
                />
              )}
            </Panel>

            <Panel title="Library" description="Papers available to retrieval tools.">
              {data.documents.length ? (
                <div className="stack-tight">
                  {data.documents.slice(0, 6).map((document) => (
                    <Link className="list-row" key={document.id} to="/papers">
                      <span className="list-main">
                        <strong className="truncate">{document.title}</strong>
                        <small className="truncate">{document.source_filename}</small>
                      </span>
                      <StatusPill value={document.status} />
                    </Link>
                  ))}
                </div>
              ) : (
                <p>Upload a PDF under Papers to start the research library.</p>
              )}
            </Panel>
          </div>
        </>
      ) : null}
    </div>
  );
}

function Stat({
  icon,
  label,
  value,
  to,
}: {
  icon: IconName;
  label: string;
  value: string | number;
  to?: string;
}) {
  const body = (
    <>
      <Icon name={icon} size={18} className="stat-icon" />
      <span>{label}</span>
      <strong>{value}</strong>
    </>
  );
  return to ? (
    <Link className="stat stat-link" to={to}>
      {body}
    </Link>
  ) : (
    <div className="stat">{body}</div>
  );
}
