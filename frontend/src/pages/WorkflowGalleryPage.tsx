import { useEffect, useMemo, useState } from 'react';
import { useConfirm } from '../components/common/ConfirmDialog';
import { EmptyState } from '../components/common/EmptyState';
import { ErrorNotice } from '../components/common/ErrorNotice';
import { Icon } from '../components/common/Icon';
import { SkeletonList } from '../components/common/Skeleton';
import { StatusBadge } from '../components/common/StatusBadge';
import { toMessage, useToast } from '../components/common/Toast';
import { api } from '../lib/api';
import { formatRelative } from '../lib/format';
import { categoryMeta, categoryVars } from '../lib/nodeCatalog';
import { Link, useNavigate } from '../lib/router';
import type {
  NodeDefinitionResponse,
  RunResponse,
  WorkflowDefinition,
  WorkflowResponse,
} from '../types/api';

type Filter = 'all' | 'saved' | 'templates';

interface CategoryTally {
  id: string;
  label: string;
  count: number;
}

/** Counts how many nodes of each category a definition uses, for the card's colour strip. */
function tallyCategories(
  definition: WorkflowDefinition | null | undefined,
  catalog: Map<string, NodeDefinitionResponse>,
): CategoryTally[] {
  if (!definition) return [];
  const counts = new Map<string, number>();
  for (const node of definition.nodes) {
    const category = catalog.get(node.type)?.category ?? 'unknown';
    counts.set(category, (counts.get(category) ?? 0) + 1);
  }
  return [...counts.entries()]
    .map(([id, count]) => ({ id, label: categoryMeta(id).label, count }))
    .sort((left, right) => right.count - left.count || left.label.localeCompare(right.label));
}

function CategoryStrip({ tallies }: { tallies: CategoryTally[] }) {
  if (tallies.length === 0) return <div className="wf-card-strip empty">Empty graph</div>;
  const total = tallies.reduce((sum, entry) => sum + entry.count, 0);
  return (
    <div className="wf-card-strip" title={tallies.map((entry) => `${entry.label}: ${entry.count}`).join('\n')}>
      {tallies.map((entry) => (
        <span
          key={entry.id}
          className="wf-card-strip-seg"
          style={{ ...categoryVars(entry.id), flexGrow: entry.count / total }}
        />
      ))}
    </div>
  );
}

function CardStats({ definition, extra }: { definition: WorkflowDefinition | null | undefined; extra?: string }) {
  return (
    <p className="wf-card-stats">
      <span>
        <Icon name="grid" size={11} />
        {definition?.nodes.length ?? 0} nodes
      </span>
      <span>
        <Icon name="link" size={11} />
        {definition?.edges.length ?? 0} links
      </span>
      {extra ? <span>{extra}</span> : null}
    </p>
  );
}

export function WorkflowGalleryPage() {
  const navigate = useNavigate();
  const toast = useToast();
  const confirm = useConfirm();

  const [workflows, setWorkflows] = useState<WorkflowResponse[]>([]);
  const [templates, setTemplates] = useState<WorkflowDefinition[]>([]);
  const [catalog, setCatalog] = useState<NodeDefinitionResponse[]>([]);
  const [runs, setRuns] = useState<RunResponse[]>([]);
  const [filter, setFilter] = useState<Filter>('all');
  const [search, setSearch] = useState('');
  const [loading, setLoading] = useState(true);
  const [pageError, setPageError] = useState('');
  const [busyId, setBusyId] = useState('');

  const catalogMap = useMemo(() => new Map(catalog.map((entry) => [entry.type, entry])), [catalog]);

  useEffect(() => {
    let active = true;
    setLoading(true);
    Promise.all([api.listWorkflows(), api.listWorkflowTemplates(), api.listNodes(), api.listRuns()])
      .then(([saved, starter, nodeCatalog, runList]) => {
        if (!active) return;
        setWorkflows(saved);
        setTemplates(starter);
        setCatalog(nodeCatalog);
        setRuns(runList);
        setPageError('');
      })
      .catch((err) => active && setPageError(toMessage(err, 'Failed to load agents')))
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, []);

  /** Latest run per workflow name — the only link the run API exposes back to a workflow. */
  const latestRunByName = useMemo(() => {
    const map = new Map<string, RunResponse>();
    for (const run of runs) {
      const current = map.get(run.workflow_name);
      if (!current || new Date(run.created_at) > new Date(current.created_at)) {
        map.set(run.workflow_name, run);
      }
    }
    return map;
  }, [runs]);

  const term = search.trim().toLowerCase();

  const visibleWorkflows = useMemo(() => {
    if (filter === 'templates') return [];
    return workflows.filter(
      (workflow) => !term || [workflow.name, workflow.description || ''].join(' ').toLowerCase().includes(term),
    );
  }, [filter, term, workflows]);

  const visibleTemplates = useMemo(() => {
    if (filter === 'saved') return [];
    return templates.filter(
      (template) => !term || [template.name, template.description || ''].join(' ').toLowerCase().includes(term),
    );
  }, [filter, templates, term]);

  const deleteWorkflow = async (workflow: WorkflowResponse) => {
    const confirmed = await confirm({
      title: `Delete “${workflow.name}”?`,
      description: 'Every saved version of this agent will be removed. Existing run history is kept.',
      confirmLabel: 'Delete agent',
    });
    if (!confirmed) return;
    setBusyId(workflow.id);
    try {
      await api.deleteWorkflow(workflow.id);
      setWorkflows(await api.listWorkflows());
      toast.success(`Deleted ${workflow.name}`);
      setPageError('');
    } catch (err) {
      const message = toMessage(err, 'Failed to delete agent');
      setPageError(message);
      toast.failure('Delete failed', message);
    } finally {
      setBusyId('');
    }
  };

  const runWorkflow = async (workflow: WorkflowResponse) => {
    const versionId = workflow.latest_version?.id;
    if (!versionId) {
      toast.failure('Nothing to run', 'This agent has no saved version yet.');
      return;
    }
    setBusyId(workflow.id);
    try {
      const run = await api.createRun({ workflow_version_id: versionId, inputs: {} });
      toast.success('Run started', 'Streaming live output on the Runs page.');
      navigate(`/runs/${run.id}`);
    } catch (err) {
      const message = toMessage(err, 'Failed to start run');
      setPageError(message);
      toast.failure('Could not start the run', message);
    } finally {
      setBusyId('');
    }
  };

  const nothingToShow = !loading && visibleWorkflows.length === 0 && visibleTemplates.length === 0;

  return (
    <div className="stack gap-md wf-gallery">
      <header className="panel wf-gallery-toolbar">
        <label className="search-field grow">
          <Icon name="search" size={14} />
          <input
            value={search}
            placeholder="Search agents and templates"
            aria-label="Search agents"
            onChange={(event) => setSearch(event.target.value)}
          />
          {search ? (
            <button type="button" aria-label="Clear search" onClick={() => setSearch('')}>
              <Icon name="close" size={12} />
            </button>
          ) : null}
        </label>

        <div className="segmented-control inline three" role="tablist" aria-label="Filter agents">
          <button type="button" className={filter === 'all' ? 'active' : ''} onClick={() => setFilter('all')}>
            All
          </button>
          <button type="button" className={filter === 'saved' ? 'active' : ''} onClick={() => setFilter('saved')}>
            Saved <span className="tiny-tag">{workflows.length}</span>
          </button>
          <button type="button" className={filter === 'templates' ? 'active' : ''} onClick={() => setFilter('templates')}>
            Templates <span className="tiny-tag">{templates.length}</span>
          </button>
        </div>

        <Link className="button primary" to="/agents/new">
          <Icon name="plus" size={14} />
          New agent
        </Link>
      </header>

      {pageError ? <ErrorNotice message={pageError} /> : null}

      {loading ? (
        <div className="panel">
          <SkeletonList rows={4} />
        </div>
      ) : null}

      {visibleWorkflows.length > 0 ? (
        <section className="stack gap-sm">
          <div className="panel-subheader">
            <h3>Your agents</h3>
            <span className="muted-text small">{visibleWorkflows.length} shown</span>
          </div>
          <div className="wf-card-grid">
            {visibleWorkflows.map((workflow) => {
              const definition = workflow.latest_version?.definition;
              const lastRun = latestRunByName.get(workflow.name);
              return (
                <article key={workflow.id} className="wf-card">
                  <CategoryStrip tallies={tallyCategories(definition, catalogMap)} />
                  <Link className="wf-card-body" to={`/agents/${workflow.id}`}>
                    <div className="wf-card-title">
                      <h4 className="truncate">{workflow.name}</h4>
                      {workflow.is_template ? <span className="tiny-tag">Template</span> : null}
                    </div>
                    <p className="wf-card-desc">{workflow.description || 'No description'}</p>
                    <CardStats definition={definition} extra={`v${workflow.latest_version?.version ?? 0}`} />
                  </Link>
                  <footer className="wf-card-footer">
                    <span className="wf-card-run" title={lastRun ? `Last run ${lastRun.status}` : 'Never run'}>
                      {lastRun ? (
                        <>
                          <StatusBadge status={lastRun.status} />
                          {formatRelative(lastRun.created_at)}
                        </>
                      ) : (
                        <span className="muted-text small">Never run</span>
                      )}
                    </span>
                    <span className="button-row compact">
                      <button
                        type="button"
                        className="button sm"
                        title="Run the latest saved version with empty inputs"
                        disabled={busyId === workflow.id || !workflow.latest_version}
                        onClick={() => void runWorkflow(workflow)}
                      >
                        <Icon name="play" size={11} />
                        Run
                      </button>
                      <button
                        type="button"
                        className="button subtle sm icon-only danger-text"
                        aria-label={`Delete ${workflow.name}`}
                        title="Delete agent"
                        disabled={busyId === workflow.id}
                        onClick={() => void deleteWorkflow(workflow)}
                      >
                        <Icon name="trash" size={12} />
                      </button>
                    </span>
                  </footer>
                </article>
              );
            })}
          </div>
        </section>
      ) : null}

      {visibleTemplates.length > 0 ? (
        <section className="stack gap-sm">
          <div className="panel-subheader">
            <h3>Starter templates</h3>
            <span className="muted-text small">Open one to copy it onto a fresh canvas</span>
          </div>
          <div className="wf-card-grid">
            {visibleTemplates.map((template) => (
              <article key={template.name} className="wf-card template">
                <CategoryStrip tallies={tallyCategories(template, catalogMap)} />
                <Link className="wf-card-body" to={`/agents/new?template=${encodeURIComponent(template.name)}`}>
                  <div className="wf-card-title">
                    <h4 className="truncate">{template.name}</h4>
                    <span className="tiny-tag">Template</span>
                  </div>
                  <p className="wf-card-desc">{template.description || 'No description'}</p>
                  <CardStats definition={template} />
                </Link>
                <footer className="wf-card-footer">
                  <span className="muted-text small">Starter graph</span>
                  <Link className="button sm" to={`/agents/new?template=${encodeURIComponent(template.name)}`}>
                    <Icon name="arrowRight" size={11} />
                    Open
                  </Link>
                </footer>
              </article>
            ))}
          </div>
        </section>
      ) : null}

      {nothingToShow ? (
        <div className="panel">
          <EmptyState
            icon="workflow"
            title={term ? `Nothing matched “${search}”` : 'No agents yet'}
            description={
              term
                ? 'Try a different search, or clear the filter to see templates too.'
                : 'Start from a blank canvas or open one of the starter templates.'
            }
            action={
              <Link className="button primary" to="/agents/new">
                <Icon name="plus" size={14} />
                New agent
              </Link>
            }
          />
        </div>
      ) : null}
    </div>
  );
}
