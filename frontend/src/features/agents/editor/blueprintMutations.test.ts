import { describe, expect, it } from 'vitest';
import { blankAgent, blankBlueprint } from '../types';
import {
  connectBlueprint,
  deleteBlueprintEdge,
  removeBlueprintSelection,
} from './blueprintMutations';

describe('blueprint relationship mutations', () => {
  it('binds SDK relationships idempotently and removes their references', () => {
    const initial = blankBlueprint();
    initial.agents = [blankAgent('owner', 'Owner'), blankAgent('delegate', 'Delegate')];
    initial.entry_agent_id = 'owner';
    initial.tools = [
      {
        id: 'search',
        kind: 'function',
        catalog_id: 'documents.search',
        needs_approval: false,
      },
    ];
    initial.guardrails = [
      {
        id: 'short-input',
        kind: 'input',
        catalog_id: 'content.max_characters',
      },
    ];

    const withTool = connectBlueprint(initial, {
      source: 'tool:search',
      target: 'agent:owner',
      sourceHandle: null,
      targetHandle: null,
    });
    const withHandoff = connectBlueprint(withTool, {
      source: 'agent:owner',
      target: 'agent:delegate',
      sourceHandle: null,
      targetHandle: null,
    });
    const withoutDuplicate = connectBlueprint(withHandoff, {
      source: 'agent:owner',
      target: 'agent:delegate',
      sourceHandle: null,
      targetHandle: null,
    });
    const guarded = connectBlueprint(withoutDuplicate, {
      source: 'guardrail:short-input',
      target: 'agent:owner',
      sourceHandle: null,
      targetHandle: null,
    });

    expect(guarded.agents[0].tool_ids).toEqual(['search']);
    expect(guarded.handoffs).toHaveLength(1);
    expect(guarded.agents[0].input_guardrail_ids).toEqual(['short-input']);

    const unbound = deleteBlueprintEdge(guarded, {
      id: 'binding:search:owner',
      source: 'tool:search',
      target: 'agent:owner',
      data: { relation: 'tool', owner: 'owner', toolId: 'search' },
    });
    const removed = removeBlueprintSelection(
      unbound,
      'input_guardrail:short-input:owner',
    );

    expect(removed.agents[0].tool_ids).toEqual([]);
    expect(removed.guardrails).toEqual([]);
    expect(removed.agents[0].input_guardrail_ids).toEqual([]);
  });
});
