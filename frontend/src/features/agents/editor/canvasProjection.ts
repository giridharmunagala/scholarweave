import type { Edge, Node } from '@xyflow/react';
import type {
  AgentBlueprint,
  AgentPresentation,
  AgentSpec,
  ToolSpec,
} from '../types';

export interface PrimitiveNodeData extends Record<string, unknown> {
  entityId: string;
  kind: 'agent' | 'function_tool' | 'hosted_tool' | 'guardrail';
  title: string;
  subtitle: string;
  entry: boolean;
}

export type PrimitiveNode = Node<PrimitiveNodeData, 'primitive'>;

export function projectNodes(
  blueprint: AgentBlueprint,
  presentation: AgentPresentation,
): PrimitiveNode[] {
  const agentNodes = blueprint.agents.map((agent, index) =>
    primitiveNode(
      `agent:${agent.id}`,
      agent.id,
      'agent',
      agent.name,
      agent.model?.model || 'Inherited model',
      blueprint.entry_agent_id === agent.id,
      positionFor(presentation, `agent:${agent.id}`, index, 0),
    ),
  );
  const toolNodes = (blueprint.tools ?? []).map((tool, index) =>
    primitiveNode(
      `tool:${tool.id}`,
      tool.id,
      tool.kind === 'function' ? 'function_tool' : 'hosted_tool',
      toolTitle(tool),
      tool.kind === 'function' ? tool.catalog_id : tool.kind,
      false,
      positionFor(presentation, `tool:${tool.id}`, index, 1),
    ),
  );
  const guardrailNodes = (blueprint.guardrails ?? []).map((guardrail, index) =>
    primitiveNode(
      `guardrail:${guardrail.id}`,
      guardrail.id,
      'guardrail',
      guardrail.id,
      guardrail.kind.split('_').join(' '),
      false,
      positionFor(presentation, `guardrail:${guardrail.id}`, index, 2),
    ),
  );
  return [...agentNodes, ...toolNodes, ...guardrailNodes];
}

export function projectEdges(blueprint: AgentBlueprint): Edge[] {
  const edges: Edge[] = [];
  for (const agent of blueprint.agents) {
    for (const toolId of agent.tool_ids ?? []) {
      edges.push({
        id: `binding:${toolId}:${agent.id}`,
        source: `tool:${toolId}`,
        target: `agent:${agent.id}`,
        label: 'FunctionTool',
        className: 'edge-tool',
        data: { relation: 'tool', owner: agent.id, toolId },
      });
    }
  }
  for (const relation of blueprint.handoffs ?? []) {
    edges.push({
      id: `handoff:${relation.id}`,
      source: `agent:${relation.source_agent_id}`,
      target: `agent:${relation.target_agent_id}`,
      label: relation.tool_name || 'Handoff',
      animated: true,
      className: 'edge-handoff',
      data: { relation: 'handoff', relationId: relation.id },
    });
  }
  for (const relation of blueprint.agent_tools ?? []) {
    edges.push({
      id: `agent-tool:${relation.id}`,
      source: `agent:${relation.delegate_agent_id}`,
      target: `agent:${relation.owner_agent_id}`,
      label: relation.tool_name,
      className: 'edge-agent-tool',
      data: { relation: 'agent_tool', relationId: relation.id },
    });
  }
  for (const agent of blueprint.agents) {
    for (const guardrailId of agent.input_guardrail_ids ?? []) {
      edges.push(guardrailEdge(guardrailId, `agent:${agent.id}`, 'input_guardrail', agent.id));
    }
    for (const guardrailId of agent.output_guardrail_ids ?? []) {
      edges.push(guardrailEdge(guardrailId, `agent:${agent.id}`, 'output_guardrail', agent.id));
    }
  }
  for (const tool of blueprint.tools ?? []) {
    if (tool.kind !== 'function') continue;
    for (const guardrailId of tool.input_guardrail_ids ?? []) {
      edges.push(guardrailEdge(guardrailId, `tool:${tool.id}`, 'tool_input_guardrail', tool.id));
    }
    for (const guardrailId of tool.output_guardrail_ids ?? []) {
      edges.push(guardrailEdge(guardrailId, `tool:${tool.id}`, 'tool_output_guardrail', tool.id));
    }
  }
  return edges;
}

function primitiveNode(
  id: string,
  entityId: string,
  kind: PrimitiveNodeData['kind'],
  title: string,
  subtitle: string,
  entry: boolean,
  position: { x: number; y: number },
): PrimitiveNode {
  return {
    id,
    type: 'primitive',
    position,
    data: { entityId, kind, title, subtitle, entry },
  };
}

function guardrailEdge(
  guardrailId: string,
  target: string,
  relation: string,
  owner: string,
): Edge {
  return {
    id: `${relation}:${guardrailId}:${owner}`,
    source: `guardrail:${guardrailId}`,
    target,
    label: relation.split('_').join(' '),
    className: 'edge-guardrail',
    data: { relation, guardrailId, owner },
  };
}

function positionFor(
  presentation: AgentPresentation,
  key: string,
  index: number,
  row: number,
) {
  return presentation.positions[key] ?? {
    x: 70 + index * 260,
    y: 80 + row * 210,
  };
}

function toolTitle(tool: ToolSpec): string {
  if (tool.kind === 'function') return tool.name || tool.catalog_id;
  if (tool.kind === 'web_search') return 'Web search';
  return 'File search';
}
