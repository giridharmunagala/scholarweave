import type { NodeProps } from '@xyflow/react';
import { Handle, Position } from '@xyflow/react';
import { Icon } from '../common/Icon';
import { conditionSummary } from '../../lib/conditions';
import { modelReferenceFromConfig, modelReferenceLabel } from '../../lib/models';
import { categoryMeta, categoryVars } from '../../lib/nodeCatalog';
import type { FlowNode } from '../../lib/workflowFlow';
import { inputHandleId, outputHandleId } from '../../lib/workflowFlow';
import type { PortDefinitionResponse } from '../../types/api';
import {
  AGENT_START_TYPE,
  agentInputNodes,
  agentInputPortName,
  declaredWorkflowInputs,
} from '../../lib/workflowInputs';

function portKind(port: PortDefinitionResponse): string {
  return port.item_kind ? `${port.kind}:${port.item_kind}` : port.kind;
}

function portTitle(port: PortDefinitionResponse, direction: 'Input' | 'Output'): string {
  const requirement = direction === 'Input' && !port.required ? ' (optional)' : '';
  return `${direction} · ${port.name}${requirement} — ${portKind(port)}${port.description ? `\n${port.description}` : ''}`;
}

function edgeOffset(index: number, total: number): string {
  return `${((index + 1) / (total + 1)) * 100}%`;
}

function configPreview(config: Record<string, unknown>): [string, string] | null {
  const first = Object.entries(config).find(
    ([key, value]) =>
      !['model', 'provider_profile_id', 'model_reference'].includes(key) &&
      value != null &&
      ['string', 'number', 'boolean'].includes(typeof value) &&
      String(value) !== '',
  );
  return first ? [first[0], String(first[1])] : null;
}

function AgentStartNodeCard({ data, selected }: Pick<NodeProps<FlowNode>, 'data' | 'selected'>) {
  const inputs = declaredWorkflowInputs(agentInputNodes(data.config));
  return (
    <div className={`flow-node agent-start-node expanded${selected ? ' selected' : ''}`} style={categoryVars('inputs')}>
      <div className="flow-node-header">
        <span className="flow-node-icon">
          <Icon name="login" size={13} />
        </span>
        <div className="flow-node-title">
          <strong>Agent inputs</strong>
          <div className="flow-node-subtitle">Customer starts here</div>
        </div>
        <span className="flow-node-category">{inputs.length}</span>
      </div>
      {inputs.length ? (
        <div className="agent-start-inputs">
          {inputs.map((input, index) => (
            <div className="agent-start-input" key={input.key}>
              <div>
                <strong>{input.label}</strong>
                <span>{input.key}</span>
              </div>
              <span className="port-kind-pill">{input.kind}</span>
              <span className="agent-start-ports">
                <span title="Typed value">
                  data
                  {input.nodeIds.map((nodeId) => (
                    <Handle
                      key={`${nodeId}:value`}
                      type="source"
                      id={outputHandleId(agentInputPortName(nodeId, 'value'))}
                      position={Position.Right}
                      style={{ top: 58 + index * 48 }}
                    />
                  ))}
                </span>
                <span title="Value converted to text">
                  text
                  {input.nodeIds.map((nodeId) => (
                    <Handle
                      key={`${nodeId}:text`}
                      type="source"
                      id={outputHandleId(agentInputPortName(nodeId, 'text'))}
                      position={Position.Right}
                      style={{ top: 74 + index * 48 }}
                    />
                  ))}
                </span>
              </span>
            </div>
          ))}
        </div>
      ) : (
        <p className="agent-start-empty">Add the information this agent should ask the customer for.</p>
      )}
    </div>
  );
}

export function WorkflowNodeCard({ data, selected }: NodeProps<FlowNode>) {
  if (data.definition.type === AGENT_START_TYPE) {
    return <AgentStartNodeCard data={data} selected={selected} />;
  }

  const meta = categoryMeta(data.definition.category);
  const inputs = data.definition.inputs;
  const outputs = data.definition.outputs;
  const preview = configPreview(data.config);
  const reference = modelReferenceFromConfig(data.config);
  const model = data.effectiveModel;
  const condition = conditionSummary(data.runWhen);
  const customRevision = data.definition.type.startsWith('custom:')
    ? data.definition.type.slice('custom:'.length, 'custom:'.length + 8)
    : '';
  const customRevisionNumber = data.definition.tags.find((tag) => tag.startsWith('custom-revision:'))?.slice('custom-revision:'.length);

  return (
    <div className={`flow-node compact stable${selected ? ' selected' : ''}`} style={categoryVars(data.definition.category)}>
      <div className="flow-node-header">
        <span className="flow-node-icon" title={meta.label}>
          <Icon name={meta.icon} size={13} />
        </span>
        <div className="flow-node-title">
          <strong title={data.nodeName || data.definition.label}>{data.nodeName || data.definition.label}</strong>
          <div className="flow-node-subtitle">{data.definition.type}</div>
        </div>
        <span className="flow-node-category">{meta.label}</span>
      </div>
      <div className="flow-node-stable-body">
        {preview ? (
          <div className="flow-node-summary" title={`${preview[0]}: ${preview[1]}`}>
            <span>{preview[0]}</span>
            <strong>{preview[1]}</strong>
          </div>
        ) : (
          <div className="flow-node-summary empty">
            <span>{data.definition.description || 'Configure this node'}</span>
          </div>
        )}
        <div className="flow-node-badges">
          {model ? (
            <span
              className={`flow-node-badge model${model.issue ? ' issue' : ''}`}
              title={`${model.source}: ${model.label}${model.issue ? ` · ${model.issue.replace(/-/g, ' ')}` : ''}`}
            >
              {model.label}
            </span>
          ) : reference ? (
            <span className="flow-node-badge model" title={modelReferenceLabel(reference)}>
              {modelReferenceLabel(reference)}
            </span>
          ) : null}
          {condition ? <span className="flow-node-badge condition" title={condition}>when {condition}</span> : null}
          {customRevision ? <span className="flow-node-badge custom" title={`Pinned custom-node revision ${customRevisionNumber || customRevision}`}>custom r{customRevisionNumber || `·${customRevision}`}</span> : null}
        </div>
      </div>
      <div className="flow-node-ports">
        <span className="flow-port-count" title={inputs.map((port) => `${port.name} (${portKind(port)})`).join(', ') || 'No inputs'}>
          {inputs.length} in
        </span>
        <span className="flow-port-count" title={outputs.map((port) => `${port.name} (${portKind(port)})`).join(', ') || 'No outputs'}>
          {outputs.length} out
        </span>
      </div>
      {inputs.map((port, index) => (
        <Handle
          key={port.name}
          className={`compact-handle${port.required ? '' : ' optional'}`}
          type="target"
          id={inputHandleId(port.name)}
          position={Position.Left}
          style={{ top: edgeOffset(index, inputs.length) }}
          title={portTitle(port, 'Input')}
        />
      ))}
      {outputs.map((port, index) => (
        <Handle
          key={port.name}
          className="compact-handle"
          type="source"
          id={outputHandleId(port.name)}
          position={Position.Right}
          style={{ top: edgeOffset(index, outputs.length) }}
          title={portTitle(port, 'Output')}
        />
      ))}
    </div>
  );
}
