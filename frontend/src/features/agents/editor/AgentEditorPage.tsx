import { useState } from 'react';
import { useLocation, useNavigate } from '../../../app/router';
import { Icon } from '../../../shared/components/Icons';
import { ErrorNotice, Loading, PageHeader, Panel } from '../../../shared/components/Ui';
import { AgentCanvas } from './AgentCanvas';
import { AddNodeMenu } from './AddNodeMenu';
import { PrimitiveInspector } from './PrimitiveInspector';
import { useAgentEditor } from './useAgentEditor';
import { agentsApi } from '../api';
import './agents.css';

export default function AgentEditorPage() {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const segment = pathname.split('/').filter(Boolean)[1];
  const agentId = segment && segment !== 'new' ? segment : null;
  const editor = useAgentEditor(agentId);
  const [runInput, setRunInput] = useState('');
  const [running, setRunning] = useState(false);

  if (editor.loading) return <Loading label="Loading SDK blueprint…" />;

  const run = async () => {
    if (!runInput.trim()) return;
    setRunning(true);
    try {
      const saved = await editor.save();
      const response = saved
        ? await agentsApi.run(saved.latest_revision.id, runInput)
        : await agentsApi.runEphemeral(editor.blueprint, runInput);
      navigate(`/runs/${response.id}`);
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="page page-wide agent-editor-page">
      <PageHeader
        eyebrow="OpenAI Agents SDK"
        title={editor.blueprint.name}
        description="The canvas edits direct Agent, FunctionTool, Agent.as_tool, and Handoff relationships. It is not a data-flow graph."
        actions={
          <>
            <button className="button secondary" type="button" onClick={() => void editor.validate()}>
              <Icon name="check" size={15} />
              Validate
            </button>
            <button
              className="button"
              type="button"
              disabled={editor.saving}
              onClick={() => void editor.save().then((saved) => saved && !agentId && navigate(`/agents/${saved.id}`, { replace: true }))}
            >
              <Icon name="save" size={15} />
              {editor.saving ? 'Saving…' : 'Save revision'}
            </button>
          </>
        }
      />
      {editor.error ? <ErrorNotice error={editor.error} /> : null}
      {editor.issues.length ? <div className="notice error"><strong>Blueprint issues</strong><ul>{editor.issues.map((issue) => <li key={issue}>{issue}</li>)}</ul></div> : null}
      <Panel className="editor-toolbar">
        <div className="toolbar">
          <div className="toolbar-group">
            <AddNodeMenu
              catalog={editor.catalog}
              onAddAgent={editor.addAgent}
              onAddFunctionTool={editor.addFunctionTool}
              onAddGuardrail={editor.addGuardrail}
            />
            <span className="toolbar-hint">Add a node, then connect its handles on the canvas.</span>
          </div>
          <div className="toolbar-group run-inline">
            <input placeholder="Run input" value={runInput} onChange={(event) => setRunInput(event.target.value)} />
            <button className="button" type="button" disabled={running || !runInput.trim()} onClick={() => void run()}>
              <Icon name="play" size={15} />
              {running ? 'Starting…' : 'Run'}
            </button>
          </div>
        </div>
      </Panel>
      <div className="editor-grid">
        <div className="stack-tight">
          <AgentCanvas
            blueprint={editor.blueprint}
            presentation={editor.presentation}
            selectedId={editor.selectedId}
            onSelect={editor.setSelectedId}
            onConnect={editor.connect}
            onMove={(nodeId, position) => editor.setPresentation((current) => ({ positions: { ...current.positions, [nodeId]: position } }))}
            onDeleteEdge={editor.deleteEdge}
          />
          <div className="canvas-legend" aria-hidden="true">
            <span className="legend-tool"><i /> FunctionTool</span>
            <span className="legend-agent-tool"><i /> Agent.as_tool()</span>
            <span className="legend-handoff"><i /> handoff()</span>
            <span className="legend-guardrail"><i /> guardrail</span>
          </div>
        </div>
        <Panel className="inspector-panel">
          <PrimitiveInspector
            blueprint={editor.blueprint}
            setBlueprint={editor.setBlueprint}
            providers={editor.providers}
            selected={editor.selected}
            onRemove={editor.removeSelected}
          />
        </Panel>
      </div>
    </div>
  );
}
