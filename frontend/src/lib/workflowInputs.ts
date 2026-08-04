import type { WorkflowDefinition, WorkflowNode } from '../types/api';

/** Node type that declares one entry of a workflow's public input interface. */
export const WORKFLOW_INPUT_TYPE = 'workflow_input';
export const AGENT_START_TYPE = '__agent_start__';
export const AGENT_START_NODE_ID = '__agent_start_ui__';
export const AGENT_INPUT_NODES_KEY = 'inputNodes';

export type WorkflowInputKind = 'any' | 'text' | 'number' | 'json' | 'list';

export const WORKFLOW_INPUT_KINDS: WorkflowInputKind[] = ['any', 'text', 'number', 'json', 'list'];

export interface DeclaredWorkflowInput {
  key: string;
  kind: WorkflowInputKind;
  label: string;
  description: string;
  required: boolean;
  default: unknown;
  /** Every node that declares this key. More than one means the value feeds several places. */
  nodeIds: string[];
  /** True when the nodes sharing this key disagree about its kind. */
  conflicting: boolean;
}

export function agentInputPortName(nodeId: string, port: 'value' | 'text'): string {
  return `${encodeURIComponent(nodeId)}:${port}`;
}

export function parseAgentInputPortName(name: string): { nodeId: string; port: 'value' | 'text' } | null {
  const separator = name.lastIndexOf(':');
  if (separator < 1) return null;
  const port = name.slice(separator + 1);
  if (port !== 'value' && port !== 'text') return null;
  try {
    return { nodeId: decodeURIComponent(name.slice(0, separator)), port };
  } catch {
    return null;
  }
}

export function agentInputNodes(config: Record<string, unknown> | undefined): WorkflowNode[] {
  const value = config?.[AGENT_INPUT_NODES_KEY];
  if (!Array.isArray(value)) return [];
  return value.filter(
    (entry): entry is WorkflowNode =>
      Boolean(entry) &&
      typeof entry === 'object' &&
      typeof (entry as WorkflowNode).id === 'string' &&
      (entry as WorkflowNode).type === WORKFLOW_INPUT_TYPE,
  );
}

function readString(config: Record<string, unknown> | undefined, field: string): string {
  const value = config?.[field];
  return typeof value === 'string' ? value : '';
}

function readKind(config: Record<string, unknown> | undefined): WorkflowInputKind {
  const value = config?.kind;
  return typeof value === 'string' && (WORKFLOW_INPUT_KINDS as string[]).includes(value)
    ? (value as WorkflowInputKind)
    : 'any';
}

/**
 * Collapses every `workflow_input` node into the workflow's public input list, merging
 * repeated keys so a value consumed by several nodes is still declared — and entered — once.
 */
export function declaredWorkflowInputs(nodes: WorkflowNode[]): DeclaredWorkflowInput[] {
  const merged = new Map<string, DeclaredWorkflowInput>();

  for (const node of nodes) {
    if (node.type !== WORKFLOW_INPUT_TYPE) continue;
    const config = node.config || {};
    const key = readString(config, 'key') || 'input';
    const kind = readKind(config);
    const existing = merged.get(key);

    if (!existing) {
      merged.set(key, {
        key,
        kind,
        label: readString(config, 'label') || key,
        description: readString(config, 'description'),
        required: config.required !== false && config.default === undefined,
        default: config.default,
        nodeIds: [node.id],
        conflicting: false,
      });
      continue;
    }

    existing.nodeIds.push(node.id);
    existing.conflicting = existing.conflicting || existing.kind !== kind;
    // First declaration wins for presentation; later ones only contribute missing detail.
    existing.label = existing.label || readString(config, 'label') || key;
    existing.description = existing.description || readString(config, 'description');
    if (existing.default === undefined) existing.default = config.default;
    // If any node needs the value, the run cannot proceed without it.
    existing.required = existing.required || (config.required !== false && config.default === undefined);
  }

  for (const input of merged.values()) {
    if (input.default !== undefined) input.required = false;
  }

  return [...merged.values()].sort((left, right) => left.key.localeCompare(right.key));
}

export function definitionInputs(definition: WorkflowDefinition | null | undefined): DeclaredWorkflowInput[] {
  return definition ? declaredWorkflowInputs(definition.nodes) : [];
}

/** True when the workflow declares an input with this key, so a known value can be prefilled. */
export function acceptsInput(definition: WorkflowDefinition | null | undefined, key: string): boolean {
  return definitionInputs(definition).some((input) => input.key === key);
}

/** Builds the `config` object for a `workflow_input` node from a declared input. */
export function inputToNodeConfig(input: DeclaredWorkflowInput): Record<string, unknown> {
  const config: Record<string, unknown> = {
    key: input.key,
    kind: input.kind,
    label: input.label,
    description: input.description,
    required: input.required,
  };
  if (input.default !== undefined) {
    config.default = input.default;
  }
  return config;
}
