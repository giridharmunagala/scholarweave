import { useEffect, useState } from 'react';
import { Link } from '../lib/router';
import { EmptyState } from '../components/common/EmptyState';
import { ErrorNotice } from '../components/common/ErrorNotice';
import { Icon, type IconName } from '../components/common/Icon';
import { SkeletonList } from '../components/common/Skeleton';
import { StatusBadge } from '../components/common/StatusBadge';
import { api } from '../lib/api';
import { formatDateTime } from '../lib/format';
import type { DocumentResponse, HealthResponse, RunResponse, SettingsResponse, WorkflowResponse } from '../types/api';

interface StatTileProps {
  to: string;
  icon: IconName;
  label: string;
  value: string | number;
  foot: string;
}

function StatTile({ to, icon, label, value, foot }: StatTileProps) {
  return (
    <Link to={to} className="stat-card panel">
      <span className="stat-card-top">
        <Icon name={icon} size={14} />
        <span className="stat-label">{label}</span>
      </span>
      <strong>{value}</strong>
      <span className="stat-foot">{foot}</span>
    </Link>
  );
}

function localPathLabel(path: string, dataDirectory?: string): string {
  const normalizedPath = path.replace(/\\/g, '/');
  const parts = normalizedPath.split('/').filter(Boolean);
  const name = parts[parts.length - 1] || path;
  if (!dataDirectory || path === dataDirectory) return name;
  const normalizedDirectory = dataDirectory.replace(/\\/g, '/');
  const directoryParts = normalizedDirectory.split('/').filter(Boolean);
  const directoryName = directoryParts[directoryParts.length - 1];
  return normalizedPath.startsWith(`${normalizedDirectory}/`) && directoryName ? `${directoryName}/${name}` : name;
}

export function DashboardPage() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [settings, setSettings] = useState<SettingsResponse | null>(null);
  const [documents, setDocuments] = useState<DocumentResponse[]>([]);
  const [workflows, setWorkflows] = useState<WorkflowResponse[]>([]);
  const [runs, setRuns] = useState<RunResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    let active = true;
    setLoading(true);
    Promise.all([
      api.getHealth(),
      api.listDocuments(),
      api.listWorkflows(),
      api.listRuns(),
      api.getSettings().catch(() => null),
    ])
      .then(([nextHealth, nextDocuments, nextWorkflows, nextRuns, nextSettings]) => {
        if (!active) return;
        setHealth(nextHealth);
        setDocuments(nextDocuments);
        setWorkflows(nextWorkflows);
        setRuns(nextRuns);
        setSettings(nextSettings);
        setError('');
      })
      .catch((err) => active && setError(err instanceof Error ? err.message : 'Failed to load dashboard'))
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, []);

  const readyDocuments = documents.filter((doc) => doc.status === 'ready');
  const activeRuns = runs.filter((run) => ['pending', 'running'].includes(run.status));
  const hasModel = Boolean(settings?.default_generation_model);

  const steps = [
    {
      to: '/settings',
      done: hasModel,
      title: 'Choose your Ollama models',
      copy: hasModel ? `Generating with ${settings?.default_generation_model}` : 'Pick a generation and embedding model before running anything.',
    },
    {
      to: '/papers',
      done: readyDocuments.length > 0,
      title: 'Upload and ingest a paper',
      copy: readyDocuments.length
        ? `${readyDocuments.length} paper${readyDocuments.length === 1 ? '' : 's'} ingested and ready to query`
        : 'Drop in a PDF — text, tables, and figures are extracted locally.',
    },
    {
      to: '/agents',
      done: workflows.length > 0,
      title: 'Build an agent',
      copy: workflows.length ? `${workflows.length} agent${workflows.length === 1 ? '' : 's'} saved` : 'Start from a template, define customer inputs, and connect tools.',
    },
    {
      to: '/runs',
      done: runs.length > 0,
      title: 'Run it and watch live',
      copy: runs.length ? `${runs.length} run${runs.length === 1 ? '' : 's'} recorded` : 'Stream tokens, node output, and OCR progress as it happens.',
    },
  ];

  const nextStep = steps.find((step) => !step.done);

  return (
    <div className="page-stack">
      <section className="hero panel">
        <div>
          <p className="eyebrow">Workspace overview</p>
          <h2>Everything stays on this machine</h2>
          <p className="muted-text">
            {nextStep
              ? `Next up: ${nextStep.title.toLowerCase()}. ${nextStep.copy}`
              : 'Your workspace is fully set up. Upload a paper or open the editor to keep going.'}
          </p>
        </div>
        <div className="button-row wrap">
          <Link className="button primary" to="/papers">
            <Icon name="upload" size={14} />
            Upload paper
          </Link>
          <Link className="button" to="/agents">
            <Icon name="workflow" size={14} />
            Open editor
          </Link>
        </div>
      </section>

      {error ? <ErrorNotice message={error} /> : null}

      <section className="stats-grid">
        <StatTile
          to="/papers"
          icon="papers"
          label="Papers"
          value={documents.length}
          foot={`${readyDocuments.length} ready to query`}
        />
        <StatTile
          to="/agents"
          icon="workflow"
          label="Agents"
          value={workflows.length}
          foot={workflows.length ? 'Saved in your library' : 'Templates available'}
        />
        <StatTile
          to="/runs"
          icon="runs"
          label="Runs"
          value={runs.length}
          foot={activeRuns.length ? `${activeRuns.length} in progress` : 'Nothing running'}
        />
        <StatTile
          to="/settings"
          icon="sparkle"
          label="OCR"
          value={health?.ocr_available ? 'Ready' : 'Off'}
          foot={health?.ocr_available ? 'Tesseract detected' : 'Install tesseract-ocr to enable'}
        />
      </section>

      <div className="dashboard-grid">
        <section className="panel">
          <div className="panel-header">
            <div>
              <p className="eyebrow">Getting started</p>
              <h3>Your setup checklist</h3>
            </div>
            <span className="tiny-tag">
              {steps.filter((step) => step.done).length}/{steps.length} done
            </span>
          </div>
          <div className="steps-list">
            {steps.map((step, index) => (
              <Link className={`step-item ${step.done ? 'done' : ''}`} to={step.to} key={step.title}>
                <span className="step-index">{step.done ? <Icon name="check" size={11} /> : index + 1}</span>
                <span className="step-body">
                  <strong>{step.title}</strong>
                  <p>{step.copy}</p>
                </span>
                <Icon name="chevronRight" size={14} className="step-arrow" />
              </Link>
            ))}
          </div>
        </section>

        <section className="panel">
          <div className="panel-header">
            <div>
              <p className="eyebrow">System</p>
              <h3>Backend health</h3>
            </div>
            {health ? <StatusBadge status={health.status} /> : null}
          </div>
          {loading && !health ? <SkeletonList rows={2} /> : null}
          {health ? (
            <dl className="definition-grid">
              <div>
                <dt>Database</dt>
                <dd className="path">{localPathLabel(health.database_path, health.data_dir)}</dd>
              </div>
              <div>
                <dt>Data directory</dt>
                <dd className="path">{localPathLabel(health.data_dir)}</dd>
              </div>
              <div>
                <dt>Local OCR</dt>
                <dd>{health.ocr_available ? 'Available' : 'Unavailable'}</dd>
              </div>
              <div>
                <dt>Web UI build</dt>
                <dd>{health.frontend_available ? 'Present' : 'Missing — run npm run build'}</dd>
              </div>
            </dl>
          ) : null}
        </section>

        <section className="panel">
          <div className="panel-header">
            <div>
              <p className="eyebrow">Library</p>
              <h3>Recent papers</h3>
            </div>
            <Link to="/papers" className="button subtle sm">
              View all
              <Icon name="chevronRight" size={12} />
            </Link>
          </div>
          <div className="stack gap-sm">
            {loading && documents.length === 0 ? <SkeletonList rows={3} /> : null}
            {documents.slice(0, 4).map((document) => (
              <Link to={`/papers/${document.id}`} className="list-item link-card" key={document.id}>
                <div className="list-item-main">
                  <strong className="truncate">{document.title}</strong>
                  <p className="truncate">{document.source_filename}</p>
                </div>
                <StatusBadge status={document.status} />
              </Link>
            ))}
            {!loading && documents.length === 0 ? (
              <EmptyState
                icon="papers"
                title="No papers yet"
                description="Upload a PDF to extract its text, tables, and figures locally."
                action={
                  <Link className="button primary sm" to="/papers">
                    <Icon name="upload" size={12} />
                    Upload a paper
                  </Link>
                }
              />
            ) : null}
          </div>
        </section>

        <section className="panel">
          <div className="panel-header">
            <div>
              <p className="eyebrow">Executions</p>
              <h3>Latest runs</h3>
            </div>
            <Link to="/runs" className="button subtle sm">
              Inspect
              <Icon name="chevronRight" size={12} />
            </Link>
          </div>
          <div className="stack gap-sm">
            {loading && runs.length === 0 ? <SkeletonList rows={3} /> : null}
            {runs.slice(0, 4).map((run) => (
              <Link to={`/runs/${run.id}`} className="list-item link-card" key={run.id}>
                <div className="list-item-main">
                  <strong className="truncate">{run.workflow_name}</strong>
                  <p>Started {formatDateTime(run.started_at || run.created_at)}</p>
                </div>
                <StatusBadge status={run.status} />
              </Link>
            ))}
            {!loading && runs.length === 0 ? (
              <EmptyState
                icon="runs"
                title="No runs yet"
                description="Build an agent and press Run to stream its output here."
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
      </div>
    </div>
  );
}
