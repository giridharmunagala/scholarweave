import { useMemo, useState } from 'react';
import { useConfirm } from '../common/ConfirmDialog';
import { Icon } from '../common/Icon';
import { JsonEditor } from '../common/JsonEditor';
import { Modal } from '../common/Modal';
import { SchemaForm } from '../common/SchemaForm';
import { AgentNodeInspector } from './AgentNodeInspector';
import { ConditionBuilder } from './ConditionBuilder';
import { ProviderModelPicker } from './ProviderModelPicker';
import { PythonNodeInspector } from './PythonNodeInspector';
import { categoryMeta, categoryVars } from '../../lib/nodeCatalog';
import { createConditionGroup } from '../../lib/conditions';
import {
  modelCapabilityForNode,
  modelReferenceFromConfig,
  withModelReference,
  withoutModelFields,
} from '../../lib/models';
import type { FlowNode } from '../../lib/workflowFlow';
import type {
  ConditionRule,
  ProviderProfileResponse,
  SettingsResponse,
  WorkflowModelDefaults,
} from '../../types/api';

type EditorTab = 'setup' | 'model' | 'run-when' | 'inputs' | 'advanced';

interface NodeEditorModalProps {
  node: FlowNode;
  onUpdate: (updater: (node: FlowNode) => FlowNode) => void;
  onDelete: () => void;
  onClose: () => void;
  profiles: ProviderProfileResponse[];
  settings: SettingsResponse | null;
  workflowDefaults: WorkflowModelDefaults | null;
  wiredTools: string[];
  wiredHandoffs: string[];
  incomingNames: string[];
}

function conditionFromConfig(value: unknown): ConditionRule | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const candidate = value as { type?: unknown };
  return candidate.type === 'group' || candidate.type === 'predicate' ? (value as ConditionRule) : null;
}

export function NodeEditorModal({
  node,
  onUpdate,
  onDelete,
  onClose,
  profiles,
  settings,
  workflowDefaults,
  wiredTools,
  wiredHandoffs,
  incomingNames,
}: NodeEditorModalProps) {
  const confirm = useConfirm();
  const agentHasTools = wiredTools.length > 0 || wiredHandoffs.length > 0;
  const capability = modelCapabilityForNode(
    node.data.definition.type,
    node.data.definition.config_schema,
    node.data.config,
    { agentHasTools },
  );
  const [tab, setTab] = useState<EditorTab>('setup');
  const meta = categoryMeta(node.data.definition.category);
  const tabs = useMemo(
    () =>
      ([
        ['setup', 'Setup'],
        ...(capability ? [['model', 'Model']] : []),
        ['run-when', 'Run when'],
        ['inputs', 'Inputs'],
        ['advanced', 'Advanced'],
      ] as Array<[EditorTab, string]>),
    [capability],
  );

  const updateConfig = (config: Record<string, unknown>) =>
    onUpdate((current) => ({ ...current, data: { ...current.data, config } }));
  const updateName = (nodeName: string) =>
    onUpdate((current) => ({ ...current, data: { ...current.data, nodeName } }));
  const updateRunWhen = (runWhen: ConditionRule | null) =>
    onUpdate((current) => ({ ...current, data: { ...current.data, runWhen } }));
  const deleteNode = async () => {
    const approved = await confirm({
      title: `Delete “${node.data.nodeName || node.data.definition.label}”?`,
      description: 'This removes the node and every connection to it.',
      confirmLabel: 'Delete node',
    });
    if (approved) onDelete();
  };

  return (
    <Modal
      title={node.data.nodeName || node.data.definition.label}
      description={node.data.definition.description}
      onClose={onClose}
      className="node-editor-modal"
    >
      <div className="node-editor-identity" style={categoryVars(node.data.definition.category)}>
        <span className="node-editor-identity-icon">
          <Icon name={meta.icon} size={18} />
        </span>
        <span>{meta.label}</span>
        <code>{node.data.definition.type}</code>
      </div>
      <div className="segmented-control node-editor-tabs" role="tablist" aria-label="Node editor sections">
        {tabs.map(([id, label]) => (
          <button key={id} type="button" role="tab" aria-selected={tab === id} className={tab === id ? 'active' : ''} onClick={() => setTab(id)}>
            {label}
          </button>
        ))}
      </div>
      <div className="node-editor-body">
        {tab === 'setup' ? (
          <section className="stack gap-md">
            <div className="field-stack">
              <label className="field-label" htmlFor="node-display-name">
                Display name
              </label>
              <input id="node-display-name" className="input" value={node.data.nodeName} onChange={(event) => updateName(event.target.value)} />
            </div>
            {node.data.definition.type === 'agent' ? (
              <AgentNodeInspector
                config={node.data.config}
                onChange={updateConfig}
                wiredTools={wiredTools}
                wiredHandoffs={wiredHandoffs}
                hideModel
              />
            ) : node.data.definition.type === 'python_code' ? (
              <PythonNodeInspector
                config={node.data.config}
                onChange={updateConfig}
                incomingNames={incomingNames}
                allowedImports={settings?.python_node_allowed_imports ?? []}
                enabled={settings?.python_node_enabled ?? true}
              />
            ) : node.data.definition.type === 'if_else' ? (
              <ConditionBuilder
                label="Branch condition"
                description="The true output receives the value when this branch matches; otherwise the false output receives it."
                value={conditionFromConfig(node.data.config.condition)}
                onChange={(condition) => updateConfig({ ...node.data.config, condition: condition ?? createConditionGroup() })}
              />
            ) : (
              <SchemaForm schema={withoutModelFields(node.data.definition.config_schema)} value={node.data.config} onChange={updateConfig} />
            )}
          </section>
        ) : null}

        {tab === 'model' && capability ? (
          <section className="stack gap-md">
            {node.data.definition.type === 'agent' ? (
              <p className="muted-text small">
                {agentHasTools
                  ? 'This agent has tools or handoffs connected, so it inherits the tool-capable model default.'
                  : 'This agent has no tools or handoffs connected, so it inherits the chat model default.'}
              </p>
            ) : null}
            <ProviderModelPicker
              id={`node-model-${node.id}`}
              label={`${meta.label} model`}
              capability={capability}
              value={modelReferenceFromConfig(node.data.config)}
              onChange={(reference) => updateConfig(withModelReference(node.data.config, reference))}
              profiles={profiles}
              settings={settings}
              workflowDefaults={workflowDefaults}
              hint="Choose a node override, or clear it to inherit the workflow and Settings defaults."
            />
          </section>
        ) : null}

        {tab === 'run-when' ? (
          <section className="stack gap-md">
            <ConditionBuilder
              label="Run this node only when"
              description="This condition is evaluated before execution. A skipped node is recorded without failing the run."
              value={node.data.runWhen}
              onChange={updateRunWhen}
            />
          </section>
        ) : null}

        {tab === 'inputs' ? (
          <section className="stack gap-md">
            <p className="muted-text small">Static values are used when a port is not wired from another node.</p>
            <JsonEditor
              label="Static inputs"
              value={node.data.staticInputs}
              onApply={(staticInputs) =>
                onUpdate((current) => ({ ...current, data: { ...current.data, staticInputs } }))
              }
              height="md"
            />
          </section>
        ) : null}

        {tab === 'advanced' ? (
          <section className="stack gap-md">
            <JsonEditor label="Raw config" value={node.data.config} onApply={updateConfig} height="lg" />
            <div className="node-editor-danger">
              <div>
                <strong>Delete this node</strong>
                <p>Its connections will be removed too.</p>
              </div>
              <button type="button" className="button danger" onClick={() => void deleteNode()}>
                <Icon name="trash" size={13} />
                Delete node
              </button>
            </div>
          </section>
        ) : null}
      </div>
    </Modal>
  );
}
