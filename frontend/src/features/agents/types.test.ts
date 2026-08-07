import { describe, expect, it } from 'vitest';

import { blankAgent, blankBlueprint, presentationFrom, uniqueId } from './types';

describe('agent blueprint helpers', () => {
  it('creates SDK-native authoring defaults', () => {
    const blueprint = blankBlueprint();

    expect(blueprint.sdk_version).toBe('0.19.4');
    expect(blueprint.entry_agent_id).toBe('agent');
    expect(blueprint.agents).toEqual([blankAgent('agent', 'Agent')]);
    expect(blueprint.run).toMatchObject({ max_turns: 10 });
    expect(blueprint.session).toEqual({});
  });

  it('keeps presentation state separate and generates stable unique IDs', () => {
    expect(presentationFrom({ positions: { 'agent:a': { x: 10, y: 20 } } })).toEqual({
      positions: { 'agent:a': { x: 10, y: 20 } },
    });
    expect(presentationFrom({ positions: 'invalid' })).toEqual({ positions: {} });
    expect(uniqueId('agent', ['agent', 'agent-2'])).toBe('agent-3');
  });
});
