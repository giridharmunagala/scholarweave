import { type Connection, type Edge, type Node } from '@xyflow/react';
import type {
  NodeDefinitionResponse,
  PortDefinitionResponse,
  WorkflowCanvasNodeData,
  WorkflowDefinition,
  WorkflowEdge,
  WorkflowModelDefaults,
  WorkflowNode,
} from '../types/api';
import { coercionFor, kindsCompatible, type PortCoercion } from './ports';
import {
  AGENT_INPUT_NODES_KEY,
  AGENT_START_NODE_ID,
  AGENT_START_TYPE,
  WORKFLOW_INPUT_TYPE,
  agentInputNodes,
  agentInputPortName,
  parseAgentInputPortName,
} from './workflowInputs';

const UI_KEY = '__ui';
const GRID_COLUMNS = 3;
const GRID_X = 260;
const GRID_Y = 150;

export type FlowNode = Node<WorkflowCanvasNodeData, 'workflowNode'>;

export function inputHandleId(portName: string): string {
  return `in:${portName}`;
}

export function outputHandleId(portName: string): string {
  return `out:${portName}`;
}

export function parseHandleId(handleId?: string | null): string | null {
  if (!handleId) return null;
  const separator = handleId.indexOf(':');
  return separator >= 0 ? handleId.slice(separator + 1) || null : null;
}

function nextGridPosition(index: number) {
  return {
    x: 48 + (index % GRID_COLUMNS) * GRID_X,
    y: 48 + Math.floor(index / GRID_COLUMNS) * GRID_Y,
  };
}

function sanitizeConfig(config: Record<string, unknown>): Record<string, unknown> {
  const next = { ...config };
  delete next[UI_KEY];
  return next;
}

export function workflowToFlowNodes(
  definition: WorkflowDefinition,
  catalog: Map<string, NodeDefinitionResponse>,
): FlowNode[] {
  const inputNodes = definition.nodes.filter((node) => node.type === WORKFLOW_INPUT_TYPE);
  const firstInputIndex = definition.nodes.findIndex((node) => node.type === WORKFLOW_INPUT_TYPE);
  const firstInput = firstInputIndex >= 0 ? definition.nodes[firstInputIndex] : undefined;
  const inputUi = (firstInput?.config?.[UI_KEY] as { position?: { x?: number; y?: number } } | undefined)?.position;
  const inputDefinition = catalog.get(WORKFLOW_INPUT_TYPE);
  const agentStart: FlowNode = {
    id: AGENT_START_NODE_ID,
    type: 'workflowNode',
    deletable: false,
    position:
      inputUi?.x != null && inputUi?.y != null
        ? { x: inputUi.x, y: inputUi.y }
        : nextGridPosition(firstInputIndex >= 0 ? firstInputIndex : 0),
    data: {
      definition: agentStartDefinition(inputNodes, inputDefinition),
      nodeName: 'Agent inputs',
      config: { [AGENT_INPUT_NODES_KEY]: inputNodes.map((node) => ({ ...node, config: sanitizeConfig(node.config || {}) })) },
      staticInputs: {},
      runWhen: null,
    },
  };

  const canvasNodes: FlowNode[] = definition.nodes
    .filter((node) => node.type !== WORKFLOW_INPUT_TYPE)
    .map((node, index) => {
      const definitionEntry = catalog.get(node.type) ?? {
        type: node.type,
        label: node.type,
        description: 'Unknown node type',
        category: 'unknown',
        tags: [],
        inputs: [],
        outputs: [],
        config_schema: {},
      };
      const ui = (node.config?.[UI_KEY] as { position?: { x?: number; y?: number } } | undefined)?.position;
      return {
        id: node.id,
        type: 'workflowNode' as const,
        position: ui?.x != null && ui?.y != null ? { x: ui.x, y: ui.y } : nextGridPosition(index),
        data: {
          definition: definitionEntry,
          nodeName: node.name || '',
          config: sanitizeConfig(node.config || {}),
          staticInputs: node.static_inputs || {},
          runWhen: node.run_when || null,
        },
      };
    });
  return [agentStart, ...canvasNodes];
}

export function workflowToFlowEdges(definition: WorkflowDefinition): Edge[] {
  const inputIds = new Set(
    definition.nodes.filter((node) => node.type === WORKFLOW_INPUT_TYPE).map((node) => node.id),
  );
  return withDirectionMarkers(
    definition.edges.map((edge) => ({
      id: edgeId(edge),
      source: inputIds.has(edge.source_node_id) ? AGENT_START_NODE_ID : edge.source_node_id,
      sourceHandle: outputHandleId(
        inputIds.has(edge.source_node_id)
          ? agentInputPortName(edge.source_node_id, edge.source_port === 'text' ? 'text' : 'value')
          : edge.source_port,
      ),
      target: edge.target_node_id,
      targetHandle: inputHandleId(edge.target_port),
      animated: false,
    })),
  );
}

function agentStartDefinition(
  inputs: WorkflowNode[],
  inputDefinition?: NodeDefinitionResponse,
): NodeDefinitionResponse {
  const baseOutputs = inputDefinition?.outputs ?? [
    { name: 'value', kind: 'any', item_kind: null, description: '', required: true },
    { name: 'text', kind: 'text', item_kind: null, description: '', required: true },
  ];
  return {
    type: AGENT_START_TYPE,
    label: 'Agent inputs',
    description: 'All customer-provided values enter the agent through this single node.',
    category: 'inputs',
    tags: ['inputs', 'customer', 'start'],
    inputs: [],
    outputs: inputs.flatMap((input) =>
      baseOutputs.map((port) => ({
        ...port,
        name: agentInputPortName(input.id, port.name === 'text' ? 'text' : 'value'),
      })),
    ),
    config_schema: {},
    interface_role: 'input',
  };
}

export function withAgentInputNodes(
  node: FlowNode,
  inputs: WorkflowNode[],
  inputDefinition?: NodeDefinitionResponse,
): FlowNode {
  return {
    ...node,
    data: {
      ...node.data,
      definition: agentStartDefinition(inputs, inputDefinition),
      config: { ...node.data.config, [AGENT_INPUT_NODES_KEY]: inputs },
    },
  };
}

export const BIDIRECTIONAL_EDGE_CLASS = 'is-bidirectional';

/**
 * Arrowheads themselves are attached in CSS so they can follow the edge's hover
 * and selection colour. All this needs to do is flag the edges that also need a
 * head at the source end because a matching reverse edge exists.
 */
export function withDirectionMarkers(edges: Edge[]): Edge[] {
  const directions = new Set(edges.map((edge) => `${edge.source}\0${edge.target}`));

  return edges.map((edge) => {
    const bidirectional = directions.has(`${edge.target}\0${edge.source}`);
    const classes = (edge.className || '')
      .split(/\s+/)
      .filter((name) => name && name !== BIDIRECTIONAL_EDGE_CLASS);
    if (bidirectional) classes.push(BIDIRECTIONAL_EDGE_CLASS);

    return { ...edge, className: classes.join(' ') || undefined };
  });
}

/**
 * Prefills interface nodes with values already known from the surrounding context,
 * such as the paper a workflow was opened from.
 */
export function withRunInputValues(
  definition: WorkflowDefinition,
  inputs: Record<string, unknown>,
): WorkflowDefinition {
  return {
    ...definition,
    nodes: definition.nodes.map((node) => {
      const [inputKey, valueField] =
        node.type === 'text_input'
          ? [node.config?.input_key, 'value']
          : node.type === 'workflow_input'
            ? [node.config?.key, 'default']
            : [null, ''];
      if (typeof inputKey !== 'string' || !Object.prototype.hasOwnProperty.call(inputs, inputKey)) {
        return node;
      }
      return {
        ...node,
        config: {
          ...node.config,
          [valueField]: inputs[inputKey],
        },
      };
    }),
  };
}

export function edgeId(edge: WorkflowEdge): string {
  return `${edge.source_node_id}:${edge.source_port}->${edge.target_node_id}:${edge.target_port}`;
}

export function flowToWorkflowDefinition(
  name: string,
  description: string,
  nodes: FlowNode[],
  edges: Edge[],
  modelDefaults?: WorkflowModelDefaults | null,
): WorkflowDefinition {
  const workflowNodes: WorkflowNode[] = nodes.flatMap((node) => {
    if (node.data.definition.type === AGENT_START_TYPE) {
      return agentInputNodes(node.data.config).map((input) => ({
        ...input,
        config: {
          ...input.config,
          [UI_KEY]: { position: node.position },
        },
      }));
    }
    return [{
      id: node.id,
      type: node.data.definition.type,
      name: node.data.nodeName || undefined,
      config: {
        ...node.data.config,
        [UI_KEY]: { position: node.position },
      },
      static_inputs: node.data.staticInputs,
      ...(node.data.runWhen ? { run_when: node.data.runWhen } : {}),
    }];
  });

  const workflowEdges: WorkflowEdge[] = edges
    .map((edge) => {
      let sourceNodeId = edge.source;
      let sourcePort = parseHandleId(edge.sourceHandle);
      const targetPort = parseHandleId(edge.targetHandle);
      if (edge.source === AGENT_START_NODE_ID && sourcePort) {
        const inputPort = parseAgentInputPortName(sourcePort);
        if (!inputPort) return null;
        sourceNodeId = inputPort.nodeId;
        sourcePort = inputPort.port;
      }
      if (!sourcePort || !targetPort) {
        return null;
      }
      return {
        source_node_id: sourceNodeId,
        source_port: sourcePort,
        target_node_id: edge.target,
        target_port: targetPort,
      };
    })
    .filter((value): value is WorkflowEdge => Boolean(value));

  return {
    name,
    description,
    nodes: workflowNodes,
    edges: workflowEdges,
    ...(modelDefaults ? { model_defaults: modelDefaults } : {}),
  };
}

function getPort(definition: NodeDefinitionResponse | undefined, direction: 'inputs' | 'outputs', portName: string | null): PortDefinitionResponse | undefined {
  if (!definition || !portName) return undefined;
  return definition[direction].find((port) => port.name === portName);
}

export function portsCompatible(source?: PortDefinitionResponse, target?: PortDefinitionResponse): boolean {
  if (!source || !target) return false;
  if (source.kind === 'list' && target.kind === 'list') {
    return !target.item_kind || !source.item_kind || source.item_kind === target.item_kind;
  }
  return kindsCompatible(source.kind, target.kind);
}

/** The conversion an edge's value needs, or `null` when it passes straight through. */
export function edgeCoercion(source?: PortDefinitionResponse, target?: PortDefinitionResponse): PortCoercion | null {
  if (!source || !target) return null;
  return coercionFor(source.kind, target.kind);
}

/** Resolves the coercion for a live canvas edge, for marking it in the UI. */
export function coercionForEdge(edge: Edge, nodes: FlowNode[]): PortCoercion | null {
  const sourceNode = nodes.find((node) => node.id === edge.source);
  const targetNode = nodes.find((node) => node.id === edge.target);
  return edgeCoercion(
    getPort(sourceNode?.data.definition, 'outputs', parseHandleId(edge.sourceHandle ?? null)),
    getPort(targetNode?.data.definition, 'inputs', parseHandleId(edge.targetHandle ?? null)),
  );
}

/**
 * Finds the first type-compatible port pair between two nodes, so dropping a node next
 * to a selected one can wire itself up instead of leaving the author to hunt for handles.
 */
export function suggestConnection(
  sourceNode: FlowNode,
  targetNode: FlowNode,
  existingEdges: Edge[],
): { sourcePort: string; targetPort: string } | null {
  if (sourceNode.id === targetNode.id) return null;
  const taken = new Set(
    existingEdges
      .filter((edge) => edge.target === targetNode.id)
      .map((edge) => parseHandleId(edge.targetHandle ?? null)),
  );
  for (const target of targetNode.data.definition.inputs) {
    if (taken.has(target.name) && !target.fan_in) continue;
    for (const source of sourceNode.data.definition.outputs) {
      if (portsCompatible(source, target)) {
        return { sourcePort: source.name, targetPort: target.name };
      }
    }
  }
  return null;
}

export function canConnect(
  connection: {
    source?: string | null;
    sourceHandle?: string | null;
    target?: string | null;
    targetHandle?: string | null;
  },
  nodes: FlowNode[],
  existingEdges: Edge[],
): boolean {
  if (!connection.source || !connection.target || connection.source === connection.target) {
    return false;
  }
  if (!connection.sourceHandle || !connection.targetHandle) {
    return false;
  }
  const sourceNode = nodes.find((node) => node.id === connection.source);
  const targetNode = nodes.find((node) => node.id === connection.target);
  const sourcePort = getPort(sourceNode?.data.definition, 'outputs', parseHandleId(connection.sourceHandle));
  const targetPort = getPort(targetNode?.data.definition, 'inputs', parseHandleId(connection.targetHandle));
  // Fan-in ports (an agent's tools and handoffs) gather many sources, so the
  // one-edge-per-port rule only applies to ordinary inputs.
  if (
    !targetPort?.fan_in &&
    existingEdges.some(
      (edge) =>
        edge.target === connection.target &&
        edge.targetHandle === connection.targetHandle &&
        !(edge.source === connection.source && edge.sourceHandle === connection.sourceHandle),
    )
  ) {
    return false;
  }
  return portsCompatible(sourcePort, targetPort);
}
