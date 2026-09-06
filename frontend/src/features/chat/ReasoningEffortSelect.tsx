import { useState } from 'react';

import { Icon } from '../../shared/components/Icons';
import type { Provider } from '../providers/api';
import type { ModelReference } from './api';

export type ReasoningEffort =
  | 'none'
  | 'minimal'
  | 'low'
  | 'medium'
  | 'high'
  | 'xhigh'
  | 'max';

const STORAGE_KEY = 'scholarweave-reasoning-effort';
export const REASONING_EFFORTS: readonly ReasoningEffort[] = [
  'none',
  'minimal',
  'low',
  'medium',
  'high',
  'xhigh',
  'max',
];
const EFFORTS = new Set(REASONING_EFFORTS);
const EFFORT_LABELS: Record<ReasoningEffort, string> = {
  none: 'Off',
  minimal: 'Minimal',
  low: 'Low',
  medium: 'Medium',
  high: 'High',
  xhigh: 'Extra high',
  max: 'Maximum',
};

function modelStorageKey(reference: ModelReference): string | null {
  return reference.provider_profile_id && reference.model
    ? `${STORAGE_KEY}:${JSON.stringify([reference.provider_profile_id, reference.model])}`
    : null;
}

export function readStoredReasoningEffort(reference: ModelReference = {}): ReasoningEffort | null {
  const key = modelStorageKey(reference);
  if (!key) return null;
  try {
    const saved = localStorage.getItem(key);
    return EFFORTS.has(saved as ReasoningEffort) ? saved as ReasoningEffort : null;
  } catch {
    return null;
  }
}

export function storeReasoningEffort(
  value: ReasoningEffort | null,
  reference: ModelReference = {},
): void {
  const key = modelStorageKey(reference);
  if (!key) return;
  try {
    if (value) localStorage.setItem(key, value);
    else localStorage.removeItem(key);
  } catch {
    // The control still works for this session when browser storage is unavailable.
  }
}

export function useModelReasoningEffort(
  reference: ModelReference,
  supportedEfforts: readonly ReasoningEffort[] | null,
): readonly [ReasoningEffort | null, (value: ReasoningEffort | null) => void] {
  const [selections, setSelections] = useState<Record<string, ReasoningEffort | null>>({});
  const key = modelStorageKey(reference);
  const selected = key && Object.prototype.hasOwnProperty.call(selections, key)
    ? selections[key]
    : readStoredReasoningEffort(reference);
  const value = selected && supportedEfforts?.includes(selected) ? selected : null;

  const select = (effort: ReasoningEffort | null) => {
    if (!key) return;
    const next = effort && supportedEfforts?.includes(effort) ? effort : null;
    setSelections((previous) => ({ ...previous, [key]: next }));
    storeReasoningEffort(next, reference);
  };
  return [value, select];
}

export function ReasoningEffortSelect({
  value,
  supportedEfforts,
  disabled = false,
  onChange,
  defaultLabel,
  ariaLabel = 'Reasoning effort',
}: {
  value: ReasoningEffort | null;
  supportedEfforts: readonly ReasoningEffort[] | null;
  disabled?: boolean;
  onChange: (value: ReasoningEffort | null) => void;
  defaultLabel?: string;
  ariaLabel?: string;
}) {
  const unavailable = !supportedEfforts?.length;
  const hint = supportedEfforts === null
    ? 'Reasoning levels are not configured for this model. Configure them in Settings.'
    : unavailable
      ? 'This model is configured without reasoning-effort overrides.'
      : 'Only levels declared for the selected model are shown.';
  return (
    <label
      className="composer-reasoning"
      title={hint}
    >
      <span>
        <Icon name="sparkle" size={13} />
        Thinking
      </span>
      <select
        aria-label={ariaLabel}
        value={supportedEfforts?.includes(value as ReasoningEffort) ? value ?? '' : ''}
        disabled={disabled || unavailable}
        onChange={(event) => {
          const effort = event.target.value as ReasoningEffort;
          onChange(effort || null);
        }}
      >
        <option value="">
          {defaultLabel ?? (supportedEfforts === null
            ? 'Not configured'
            : unavailable ? 'Not supported' : 'Provider default')}
        </option>
        {supportedEfforts?.map((effort) => (
          <option value={effort} key={effort}>{EFFORT_LABELS[effort]}</option>
        ))}
      </select>
    </label>
  );
}

export function reasoningEffortsForModel(
  providers: Provider[],
  reference: ModelReference,
): ReasoningEffort[] | null {
  if (!reference.provider_profile_id || !reference.model) return null;
  const provider = providers.find((item) => item.id === reference.provider_profile_id);
  const model = provider?.models.find((item) => item.name === reference.model);
  const configured = model as (typeof model & {
    reasoning_efforts?: ReasoningEffort[] | null;
  });
  return configured?.reasoning_efforts ?? null;
}
