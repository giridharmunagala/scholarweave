import type { ConditionGroup, ConditionOperator, ConditionPredicate, ConditionRule, JsonValue } from '../types/api';

export const CONDITION_OPERATORS: Array<{ id: ConditionOperator; label: string }> = [
  { id: 'equals', label: 'equals' },
  { id: 'not_equals', label: 'does not equal' },
  { id: 'exists', label: 'exists' },
  { id: 'not_exists', label: 'does not exist' },
  { id: 'truthy', label: 'is true' },
  { id: 'falsy', label: 'is false' },
  { id: 'empty', label: 'is empty' },
  { id: 'not_empty', label: 'is not empty' },
  { id: 'greater_than', label: 'is greater than' },
  { id: 'greater_than_or_equal', label: 'is at least' },
  { id: 'less_than', label: 'is less than' },
  { id: 'less_than_or_equal', label: 'is at most' },
  { id: 'starts_with', label: 'starts with' },
  { id: 'ends_with', label: 'ends with' },
  { id: 'contains', label: 'contains' },
  { id: 'not_contains', label: 'does not contain' },
  { id: 'in', label: 'is one of' },
  { id: 'not_in', label: 'is not one of' },
];

export const OPERATORS_WITHOUT_VALUE = new Set<ConditionOperator>([
  'exists',
  'not_exists',
  'truthy',
  'falsy',
  'empty',
  'not_empty',
]);

export const LIST_VALUE_OPERATORS = new Set<ConditionOperator>(['in', 'not_in']);

export function createConditionPredicate(): ConditionPredicate {
  return {
    type: 'predicate',
    source: 'workflow',
    path: 'value',
    operator: 'equals',
    value: '',
  };
}

export function createConditionGroup(operator: ConditionGroup['operator'] = 'and'): ConditionGroup {
  return {
    type: 'group',
    operator,
    conditions: [createConditionPredicate()],
  };
}

export function isConditionGroup(rule: ConditionRule): rule is ConditionGroup {
  return rule.type === 'group';
}

/**
 * Produces the exact backend shape: value is absent for no-operand predicates,
 * while list operators always receive an array.
 */
export function serializeCondition(rule: ConditionRule): ConditionRule {
  if (isConditionGroup(rule)) {
    return {
      type: 'group',
      operator: rule.operator,
      conditions: rule.conditions.map(serializeCondition),
    };
  }
  const base: ConditionPredicate = {
    type: 'predicate',
    source: rule.source,
    path: rule.path.trim() || 'value',
    operator: rule.operator,
  };
  if (!OPERATORS_WITHOUT_VALUE.has(rule.operator)) {
    base.value = LIST_VALUE_OPERATORS.has(rule.operator)
      ? (Array.isArray(rule.value) ? rule.value : [])
      : (rule.value ?? null);
  }
  return base;
}

export function conditionSummary(rule: ConditionRule | null | undefined, maxLength = 72): string {
  if (!rule) return '';
  const summary = isConditionGroup(rule)
    ? `${rule.operator.toUpperCase()} (${rule.conditions.map((child) => conditionSummary(child, maxLength)).join(', ')})`
    : `${rule.source}.${rule.path} ${operatorLabel(rule.operator)}${
        OPERATORS_WITHOUT_VALUE.has(rule.operator) ? '' : ` ${conditionValueLabel(rule.value)}`
      }`;
  return summary.length > maxLength ? `${summary.slice(0, Math.max(0, maxLength - 1))}…` : summary;
}

export function operatorLabel(operator: ConditionOperator): string {
  return CONDITION_OPERATORS.find((entry) => entry.id === operator)?.label || operator;
}

export function conditionValueLabel(value: JsonValue | undefined): string {
  if (typeof value === 'string') return JSON.stringify(value);
  const serialized = JSON.stringify(value);
  return serialized === undefined ? 'null' : serialized;
}

export type ConditionValueKind = 'text' | 'number' | 'boolean' | 'json';

export function conditionValueKind(value: JsonValue | undefined, operator: ConditionOperator): ConditionValueKind {
  if (LIST_VALUE_OPERATORS.has(operator) || Array.isArray(value) || (value !== null && typeof value === 'object')) return 'json';
  if (typeof value === 'number') return 'number';
  if (typeof value === 'boolean') return 'boolean';
  return 'text';
}

export function parseConditionValue(text: string, kind: ConditionValueKind, listValue = false): JsonValue {
  if (listValue || kind === 'json') {
    try {
      return JSON.parse(text || (listValue ? '[]' : 'null')) as JsonValue;
    } catch {
      return listValue ? [] : null;
    }
  }
  if (kind === 'number') {
    const numeric = Number(text);
    return Number.isFinite(numeric) ? numeric : 0;
  }
  if (kind === 'boolean') return text === 'true';
  return text;
}

export function isConditionPathValid(path: string): boolean {
  return /^[^.]+(?:\.[^.]+)*$/.test(path.trim());
}
