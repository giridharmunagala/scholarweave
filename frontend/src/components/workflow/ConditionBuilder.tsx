import { useEffect, useState } from 'react';
import { Icon } from '../common/Icon';
import {
  CONDITION_OPERATORS,
  LIST_VALUE_OPERATORS,
  OPERATORS_WITHOUT_VALUE,
  conditionSummary,
  conditionValueKind,
  createConditionGroup,
  createConditionPredicate,
  type ConditionValueKind,
  isConditionGroup,
  isConditionPathValid,
} from '../../lib/conditions';
import type { ConditionGroup, ConditionPredicate, ConditionRule, JsonValue } from '../../types/api';

interface ConditionBuilderProps {
  value: ConditionRule | null | undefined;
  onChange: (value: ConditionRule | null) => void;
  label: string;
  description?: string;
}

function ValueControl({
  predicate,
  onChange,
}: {
  predicate: ConditionPredicate;
  onChange: (next: ConditionPredicate) => void;
}) {
  const isList = LIST_VALUE_OPERATORS.has(predicate.operator);
  const [kind, setKind] = useState<ConditionValueKind>(() => conditionValueKind(predicate.value, predicate.operator));
  const [text, setText] = useState(() => formatValue(predicate.value, conditionValueKind(predicate.value, predicate.operator), isList));
  const [error, setError] = useState('');

  useEffect(() => {
    const nextKind = conditionValueKind(predicate.value, predicate.operator);
    setKind(nextKind);
    setText(formatValue(predicate.value, nextKind, isList));
    setError('');
  }, [predicate.operator, predicate.value, isList]);

  if (OPERATORS_WITHOUT_VALUE.has(predicate.operator)) return null;

  const commit = (nextText: string, nextKind = kind) => {
    try {
      const value = parseValue(nextText, nextKind, isList);
      setError('');
      onChange({ ...predicate, value });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Enter valid JSON');
    }
  };

  return (
    <div className="condition-value-control">
      {!isList ? (
        <label className="field-stack">
          <span className="sr-only">Value type</span>
          <select
            className="input condition-value-kind"
            value={kind}
            onChange={(event) => {
              const nextKind = event.target.value as ConditionValueKind;
              setKind(nextKind);
              const nextText = formatValue(predicate.value, nextKind, false);
              setText(nextText);
              commit(nextText, nextKind);
            }}
          >
            <option value="text">Text</option>
            <option value="number">Number</option>
            <option value="boolean">Boolean</option>
            <option value="json">JSON</option>
          </select>
        </label>
      ) : null}
      {kind === 'boolean' && !isList ? (
        <select
          className="input"
          aria-label="Comparison value"
          value={text}
          onChange={(event) => {
            setText(event.target.value);
            commit(event.target.value);
          }}
        >
          <option value="true">true</option>
          <option value="false">false</option>
        </select>
      ) : kind === 'json' || isList ? (
        <textarea
          className="input json-inline condition-json-value"
          rows={2}
          aria-label={isList ? 'Comparison values as JSON list' : 'Comparison value as JSON'}
          value={text}
          onChange={(event) => setText(event.target.value)}
          onBlur={() => commit(text)}
          placeholder={isList ? '["value"]' : '{"key": "value"}'}
        />
      ) : (
        <input
          className="input"
          aria-label="Comparison value"
          type={kind === 'number' ? 'number' : 'text'}
          value={text}
          onChange={(event) => {
            setText(event.target.value);
            commit(event.target.value);
          }}
        />
      )}
      {error ? <span className="field-error">{error}</span> : null}
    </div>
  );
}

function formatValue(value: JsonValue | undefined, kind: ConditionValueKind, list: boolean): string {
  if (list || kind === 'json') return JSON.stringify(value ?? (list ? [] : null));
  if (kind === 'boolean') return value === true ? 'true' : 'false';
  return value == null ? '' : String(value);
}

function parseValue(value: string, kind: ConditionValueKind, list: boolean): JsonValue {
  if (list || kind === 'json') {
    const parsed = JSON.parse(value || (list ? '[]' : 'null')) as JsonValue;
    if (list && !Array.isArray(parsed)) throw new Error('Use a JSON list for this operator.');
    return parsed;
  }
  if (kind === 'number') {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) throw new Error('Enter a valid number.');
    return numeric;
  }
  if (kind === 'boolean') return value === 'true';
  return value;
}

function PredicateEditor({
  rule,
  onChange,
  onRemove,
}: {
  rule: ConditionPredicate;
  onChange: (next: ConditionPredicate) => void;
  onRemove: () => void;
}) {
  const pathInvalid = !isConditionPathValid(rule.path);
  return (
    <div className="condition-predicate">
      <div className="condition-predicate-fields">
        <label className="field-stack">
          <span className="sr-only">Input source</span>
          <select className="input" value={rule.source} onChange={(event) => onChange({ ...rule, source: event.target.value as ConditionPredicate['source'] })}>
            <option value="workflow">Workflow inputs</option>
            <option value="inputs">This node&apos;s inputs</option>
          </select>
        </label>
        <label className="field-stack">
          <span className="sr-only">Dotted input path</span>
          <input
            className={`input${pathInvalid ? ' invalid' : ''}`}
            aria-label="Dotted input path"
            value={rule.path}
            placeholder="document.status"
            onChange={(event) => onChange({ ...rule, path: event.target.value })}
          />
        </label>
        <label className="field-stack">
          <span className="sr-only">Operator</span>
          <select
            className="input"
            value={rule.operator}
            onChange={(event) => {
              const operator = event.target.value as ConditionPredicate['operator'];
              const next: ConditionPredicate = { ...rule, operator };
              if (OPERATORS_WITHOUT_VALUE.has(operator)) delete next.value;
              else if (LIST_VALUE_OPERATORS.has(operator) && !Array.isArray(next.value)) next.value = [];
              onChange(next);
            }}
          >
            {CONDITION_OPERATORS.map((operator) => (
              <option key={operator.id} value={operator.id}>
                {operator.label}
              </option>
            ))}
          </select>
        </label>
        <ValueControl predicate={rule} onChange={onChange} />
      </div>
      {pathInvalid ? <p className="field-error">Use a dotted path with no empty segments.</p> : null}
      <button type="button" className="button subtle icon-only sm condition-remove" onClick={onRemove} aria-label="Remove rule">
        <Icon name="trash" size={13} />
      </button>
    </div>
  );
}

function GroupEditor({
  group,
  onChange,
  onRemove,
  root = false,
}: {
  group: ConditionGroup;
  onChange: (next: ConditionGroup) => void;
  onRemove: () => void;
  root?: boolean;
}) {
  const replaceChild = (index: number, next: ConditionRule) => {
    onChange({ ...group, conditions: group.conditions.map((child, childIndex) => (childIndex === index ? next : child)) });
  };
  const removeChild = (index: number) => {
    if (group.conditions.length === 1) {
      onChange({ ...group, conditions: [createConditionPredicate()] });
      return;
    }
    onChange({ ...group, conditions: group.conditions.filter((_, childIndex) => childIndex !== index) });
  };

  return (
    <fieldset className={`condition-group${root ? ' root' : ''}`}>
      <legend className="sr-only">{root ? 'Condition rules' : 'Nested condition group'}</legend>
      <div className="condition-group-toolbar">
        <span className="condition-group-label">{root ? 'Match when' : 'Group'}</span>
        <select
          className="input condition-logic-select"
          aria-label="Group logic"
          value={group.operator}
          onChange={(event) => onChange({ ...group, operator: event.target.value as ConditionGroup['operator'] })}
        >
          <option value="and">All rules match (AND)</option>
          <option value="or">Any rule matches (OR)</option>
        </select>
        {!root ? (
          <button type="button" className="button subtle icon-only sm" aria-label="Remove group" onClick={onRemove}>
            <Icon name="trash" size={13} />
          </button>
        ) : null}
      </div>
      <div className="condition-children">
        {group.conditions.map((child, index) =>
          isConditionGroup(child) ? (
            <GroupEditor key={`group-${index}`} group={child} onChange={(next) => replaceChild(index, next)} onRemove={() => removeChild(index)} />
          ) : (
            <PredicateEditor key={`rule-${index}`} rule={child} onChange={(next) => replaceChild(index, next)} onRemove={() => removeChild(index)} />
          ),
        )}
      </div>
      <div className="button-row compact condition-add-row">
        <button type="button" className="button subtle sm" onClick={() => onChange({ ...group, conditions: [...group.conditions, createConditionPredicate()] })}>
          <Icon name="plus" size={12} />
          Add rule
        </button>
        <button type="button" className="button subtle sm" onClick={() => onChange({ ...group, conditions: [...group.conditions, createConditionGroup()] })}>
          <Icon name="layers" size={12} />
          Add group
        </button>
      </div>
    </fieldset>
  );
}

export function ConditionBuilder({ value, onChange, label, description }: ConditionBuilderProps) {
  if (!value) {
    return (
      <div className="condition-empty">
        <div>
          <strong>{label}</strong>
          {description ? <p>{description}</p> : null}
        </div>
        <button type="button" className="button subtle sm" onClick={() => onChange(createConditionGroup())}>
          <Icon name="plus" size={12} />
          Add condition
        </button>
      </div>
    );
  }
  const root = isConditionGroup(value) ? value : { type: 'group' as const, operator: 'and' as const, conditions: [value] };
  return (
    <div className="condition-builder">
      <div className="condition-builder-heading">
        <div>
          <strong>{label}</strong>
          {description ? <p>{description}</p> : null}
        </div>
        <button type="button" className="button subtle sm" onClick={() => onChange(null)}>
          <Icon name="close" size={12} />
          Clear
        </button>
      </div>
      <GroupEditor group={root} root onChange={onChange} onRemove={() => onChange(null)} />
      <p className="condition-summary" title={conditionSummary(root, 500)}>
        {conditionSummary(root)}
      </p>
    </div>
  );
}
