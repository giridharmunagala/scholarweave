import { useEffect, useState } from 'react';
import { Link, useNavigate } from '../../../app/router';
import { Icon } from '../../../shared/components/Icons';
import { EmptyState, ErrorNotice, PageHeader, Panel, SkeletonCards } from '../../../shared/components/Ui';
import { agentsApi } from '../api';
import type { AgentBlueprint, AgentResponse } from '../types';
import '../editor/agents.css';

export default function AgentsGalleryPage() {
  const navigate = useNavigate();
  const [agents, setAgents] = useState<AgentResponse[]>([]);
  const [templates, setTemplates] = useState<AgentBlueprint[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([agentsApi.list(), agentsApi.templates()])
      .then(([saved, starter]) => {
        setAgents(saved);
        setTemplates(starter);
      })
      .catch(setError)
      .finally(() => setLoading(false));
  }, []);
  if (loading)
    return (
      <div className="page">
        <PageHeader eyebrow="SDK blueprints" title="Agents" description="Saved revisions compile directly into OpenAI Agents SDK objects." />
        <SkeletonCards count={6} />
      </div>
    );

  const startFromTemplate = (template: AgentBlueprint) => {
    sessionStorage.setItem('scholarweave:new-blueprint', JSON.stringify(template));
    navigate('/agents/new');
  };

  return (
    <div className="page">
      <PageHeader
        eyebrow="SDK blueprints"
        title="Agents"
        description="Saved revisions compile directly into OpenAI Agents SDK objects."
        actions={
          <Link className="button" to="/agents/new">
            <Icon name="plus" size={16} />
            New agent
          </Link>
        }
      />
      {error ? <ErrorNotice error={error} /> : null}

      <Panel title="Saved agents" description={agents.length ? `${agents.length} blueprint${agents.length === 1 ? '' : 's'}` : undefined}>
        {agents.length ? (
          <div className="card-grid">
            {agents.map((agent) => (
              <Link className="card agent-card" to={`/agents/${agent.id}`} key={agent.id}>
                <div className="row-between">
                  <span className="eyebrow">Revision {agent.latest_revision.revision}</span>
                  <Icon name="arrowRight" size={15} className="card-arrow" />
                </div>
                <h2 className="truncate">{agent.name}</h2>
                <p>{agent.description || 'No description'}</p>
                <div className="card-meta">
                  <span>
                    <Icon name="agents" size={13} /> {agent.latest_revision.blueprint.agents.length} agents
                  </span>
                  <span>
                    <Icon name="tools" size={13} /> {agent.latest_revision.blueprint.tools?.length ?? 0} tools
                  </span>
                </div>
              </Link>
            ))}
          </div>
        ) : (
          <EmptyState
            icon="agents"
            title="No saved agents"
            description="Start from a blank SDK Agent, or pick one of the starter blueprints below."
            action={
              <Link className="button" to="/agents/new">
                Create agent
              </Link>
            }
          />
        )}
      </Panel>

      <Panel title="Starter blueprints" description="Templates use only direct SDK primitives.">
        <div className="card-grid">
          {templates.map((template) => (
            <button className="card template-card" type="button" key={template.name} onClick={() => startFromTemplate(template)}>
              <div className="row-between">
                <span className="eyebrow">Template</span>
                <Icon name="sparkle" size={15} className="card-arrow" />
              </div>
              <h2 className="truncate">{template.name}</h2>
              <p>{template.description}</p>
            </button>
          ))}
        </div>
      </Panel>
    </div>
  );
}
