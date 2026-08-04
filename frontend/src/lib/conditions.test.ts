import { describe, expect, it } from 'vitest';
import {
  conditionSummary,
  createConditionGroup,
  createConditionPredicate,
  parseConditionValue,
  serializeCondition,
} from './conditions';
import type { ConditionRule } from '../types/api';

describe('condition helpers', () => {
  it('creates a backend-compatible predicate and group', () => {
    expect(createConditionPredicate()).toEqual({
      type: 'predicate',
      source: 'workflow',
      path: 'value',
      operator: 'equals',
      value: '',
    });
    expect(createConditionGroup()).toMatchObject({ type: 'group', operator: 'and' });
  });

  it('round-trips recursive groups and removes meaningless operands', () => {
    const rule: ConditionRule = {
      type: 'group',
      operator: 'or',
      conditions: [
        { type: 'predicate', source: 'workflow', path: 'paper.title', operator: 'contains', value: 'agents' },
        {
          type: 'group',
          operator: 'and',
          conditions: [
            { type: 'predicate', source: 'inputs', path: 'enabled', operator: 'truthy', value: true },
            { type: 'predicate', source: 'inputs', path: 'score', operator: 'in', value: [1, 2] },
          ],
        },
      ],
    };

    expect(serializeCondition(rule)).toEqual({
      type: 'group',
      operator: 'or',
      conditions: [
        { type: 'predicate', source: 'workflow', path: 'paper.title', operator: 'contains', value: 'agents' },
        {
          type: 'group',
          operator: 'and',
          conditions: [
            { type: 'predicate', source: 'inputs', path: 'enabled', operator: 'truthy' },
            { type: 'predicate', source: 'inputs', path: 'score', operator: 'in', value: [1, 2] },
          ],
        },
      ],
    });
  });

  it('parses typed operands and gives a compact readable summary', () => {
    expect(parseConditionValue('42', 'number')).toBe(42);
    expect(parseConditionValue('["a", "b"]', 'json', true)).toEqual(['a', 'b']);
    expect(conditionSummary({ type: 'predicate', source: 'inputs', path: 'approved', operator: 'truthy' })).toBe(
      'inputs.approved is true',
    );
  });
});
