import type { Connection, Edge } from '@xyflow/react';
import { uniqueId, type AgentBlueprint } from '../types';

export function connectBlueprint(
  current: AgentBlueprint,
  connection: Connection,
): AgentBlueprint {
  if (!connection.source || !connection.target) return current;
  const [sourceKind, sourceId] = connection.source.split(':', 2);
  const [targetKind, targetId] = connection.target.split(':', 2);
  if (sourceKind === 'tool' && targetKind === 'agent') {
    return {
      ...current,
      agents: current.agents.map((agent) =>
        agent.id === targetId
          ? { ...agent, tool_ids: [...new Set([...(agent.tool_ids ?? []), sourceId])] }
          : agent,
      ),
    };
  }
  if (sourceKind === 'agent' && targetKind === 'agent' && sourceId !== targetId) {
    const existing = (current.handoffs ?? []).some(
      (relation) =>
        relation.source_agent_id === sourceId &&
        relation.target_agent_id === targetId,
    );
    if (existing) return current;
    const id = uniqueId('handoff', (current.handoffs ?? []).map((item) => item.id));
    return {
      ...current,
      handoffs: [
        ...(current.handoffs ?? []),
        {
          id,
          source_agent_id: sourceId,
          target_agent_id: targetId,
          tool_name: null,
          tool_description: null,
          nest_handoff_history: null,
        },
      ],
    };
  }
  if (sourceKind !== 'guardrail') return current;
  const guardrail = (current.guardrails ?? []).find((item) => item.id === sourceId);
  if (!guardrail) return current;
  if (targetKind === 'agent' && ['input', 'output'].includes(guardrail.kind)) {
    return {
      ...current,
      agents: current.agents.map((agent) =>
        agent.id === targetId
          ? {
              ...agent,
              [guardrail.kind === 'input'
                ? 'input_guardrail_ids'
                : 'output_guardrail_ids']: [
                ...new Set([
                  ...(guardrail.kind === 'input'
                    ? agent.input_guardrail_ids ?? []
                    : agent.output_guardrail_ids ?? []),
                  sourceId,
                ]),
              ],
            }
          : agent,
      ),
    };
  }
  if (
    targetKind === 'tool' &&
    ['tool_input', 'tool_output'].includes(guardrail.kind)
  ) {
    return {
      ...current,
      tools: (current.tools ?? []).map((tool) =>
        tool.id === targetId && tool.kind === 'function'
          ? {
              ...tool,
              [guardrail.kind === 'tool_input'
                ? 'input_guardrail_ids'
                : 'output_guardrail_ids']: [
                ...new Set([
                  ...(guardrail.kind === 'tool_input'
                    ? tool.input_guardrail_ids ?? []
                    : tool.output_guardrail_ids ?? []),
                  sourceId,
                ]),
              ],
            }
          : tool,
      ),
    };
  }
  return current;
}

export function deleteBlueprintEdge(
  current: AgentBlueprint,
  edge: Edge,
): AgentBlueprint {
  const relation = edge.data?.relation;
  if (relation === 'tool') {
    const owner = String(edge.data?.owner);
    const toolId = String(edge.data?.toolId);
    return {
      ...current,
      agents: current.agents.map((agent) =>
        agent.id === owner
          ? { ...agent, tool_ids: (agent.tool_ids ?? []).filter((id) => id !== toolId) }
          : agent,
      ),
    };
  }
  if (relation === 'handoff') {
    return {
      ...current,
      handoffs: (current.handoffs ?? []).filter(
        (item) => item.id !== edge.data?.relationId,
      ),
    };
  }
  if (relation === 'agent_tool') {
    return {
      ...current,
      agent_tools: (current.agent_tools ?? []).filter(
        (item) => item.id !== edge.data?.relationId,
      ),
    };
  }
  if (typeof relation !== 'string' || !relation.includes('guardrail')) return current;
  const owner = String(edge.data?.owner);
  const guardrailId = String(edge.data?.guardrailId);
  if (relation.startsWith('tool_')) {
    return {
      ...current,
      tools: (current.tools ?? []).map((tool) =>
        tool.id === owner && tool.kind === 'function'
          ? {
              ...tool,
              [relation === 'tool_input_guardrail'
                ? 'input_guardrail_ids'
                : 'output_guardrail_ids']: (
                relation === 'tool_input_guardrail'
                  ? tool.input_guardrail_ids ?? []
                  : tool.output_guardrail_ids ?? []
              ).filter((id) => id !== guardrailId),
            }
          : tool,
      ),
    };
  }
  return {
    ...current,
    agents: current.agents.map((agent) =>
      agent.id === owner
        ? {
            ...agent,
            [relation === 'input_guardrail'
              ? 'input_guardrail_ids'
              : 'output_guardrail_ids']: (
              relation === 'input_guardrail'
                ? agent.input_guardrail_ids ?? []
                : agent.output_guardrail_ids ?? []
            ).filter((id) => id !== guardrailId),
          }
        : agent,
    ),
  };
}

export function removeBlueprintSelection(
  current: AgentBlueprint,
  selectedId: string,
): AgentBlueprint {
  const [kind, id] = selectedId.split(':', 2);
  if (kind === 'handoff') {
    return {
      ...current,
      handoffs: (current.handoffs ?? []).filter((item) => item.id !== id),
    };
  }
  if (kind === 'agent-tool') {
    return {
      ...current,
      agent_tools: (current.agent_tools ?? []).filter((item) => item.id !== id),
    };
  }
  if (kind.includes('guardrail')) {
    return {
      ...current,
      guardrails: (current.guardrails ?? []).filter((item) => item.id !== id),
      agents: current.agents.map((agent) => ({
        ...agent,
        input_guardrail_ids: (agent.input_guardrail_ids ?? []).filter(
          (value) => value !== id,
        ),
        output_guardrail_ids: (agent.output_guardrail_ids ?? []).filter(
          (value) => value !== id,
        ),
      })),
      tools: (current.tools ?? []).map((tool) =>
        tool.kind === 'function'
          ? {
              ...tool,
              input_guardrail_ids: (tool.input_guardrail_ids ?? []).filter(
                (value) => value !== id,
              ),
              output_guardrail_ids: (tool.output_guardrail_ids ?? []).filter(
                (value) => value !== id,
              ),
            }
          : tool,
      ),
    };
  }
  if (kind === 'tool') {
    return {
      ...current,
      tools: (current.tools ?? []).filter((item) => item.id !== id),
      agents: current.agents.map((agent) => ({
        ...agent,
        tool_ids: (agent.tool_ids ?? []).filter((toolId) => toolId !== id),
      })),
    };
  }
  if (current.agents.length === 1) return current;
  const agents = current.agents.filter((item) => item.id !== id);
  return {
    ...current,
    agents,
    entry_agent_id: current.entry_agent_id === id ? agents[0].id : current.entry_agent_id,
    handoffs: (current.handoffs ?? []).filter(
      (item) => item.source_agent_id !== id && item.target_agent_id !== id,
    ),
    agent_tools: (current.agent_tools ?? []).filter(
      (item) => item.owner_agent_id !== id && item.delegate_agent_id !== id,
    ),
  };
}
