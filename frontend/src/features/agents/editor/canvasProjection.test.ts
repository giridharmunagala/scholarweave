import { describe, expect, it } from 'vitest';

import { blankAgent, blankBlueprint } from '../types';
import { projectEdges, projectNodes } from './canvasProjection';

describe('SDK blueprint canvas projection', () => {
  it('projects primitives and direct SDK relationships without generic graph ports', () => {
    const blueprint = blankBlueprint();
    blueprint.agents = [
      {
        ...blankAgent('owner', 'Owner'),
        tool_ids: ['search'],
        input_guardrail_ids: ['short-input'],
      },
      blankAgent('delegate', 'Delegate'),
    ];
    blueprint.entry_agent_id = 'owner';
    blueprint.tools = [
      {
        id: 'search',
        kind: 'function',
        catalog_id: 'documents.search',
        name: 'search_papers',
        needs_approval: false,
        input_guardrail_ids: [],
        output_guardrail_ids: [],
      },
    ];
    blueprint.guardrails = [
      {
        id: 'short-input',
        kind: 'input',
        catalog_id: 'content.max_characters',
        config: { max_characters: 200 },
      },
    ];
    blueprint.handoffs = [
      {
        id: 'delegate-handoff',
        source_agent_id: 'owner',
        target_agent_id: 'delegate',
        tool_name: 'delegate',
      },
    ];
    blueprint.agent_tools = [
      {
        id: 'delegate-tool',
        owner_agent_id: 'owner',
        delegate_agent_id: 'delegate',
        tool_name: 'ask_delegate',
        tool_description: 'Ask the delegate.',
        needs_approval: false,
      },
    ];

    const nodes = projectNodes(blueprint, {
      positions: { 'agent:owner': { x: 12, y: 34 } },
    });
    const edges = projectEdges(blueprint);

    expect(nodes.find((node) => node.id === 'agent:owner')).toMatchObject({
      position: { x: 12, y: 34 },
      data: { kind: 'agent', entry: true },
    });
    expect(nodes.map((node) => node.data.kind)).toEqual([
      'agent',
      'agent',
      'function_tool',
      'guardrail',
    ]);
    expect(edges.map((edge) => edge.data?.relation)).toEqual([
      'tool',
      'handoff',
      'agent_tool',
      'input_guardrail',
    ]);
  });
});
