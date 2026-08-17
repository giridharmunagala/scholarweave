import type { components } from '../../api/schema.generated';
import { Icon } from '../../shared/components/Icons';
import type { Provider } from '../providers/api';
import type { ModelReference } from './api';

export type ReasoningEffort = NonNullable<
  components['schemas']['ConversationMessageRequest']['reasoning_effort']
>;

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

export function readStoredReasoningEffort(): ReasoningEffort | null {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    return EFFORTS.has(saved as ReasoningEffort) ? saved as ReasoningEffort : null;
  } catch {
    return null;
  }
}

export function storeReasoningEffort(value: ReasoningEffort | null): void {
  try {
    if (value) localStorage.setItem(STORAGE_KEY, value);
    else localStorage.removeItem(STORAGE_KEY);
  } catch {
    // The control still works for this session when browser storage is unavailable.
  }
}

export function ReasoningEffortSelect({
  value,
  supportedEfforts,
  disabled = false,
  onChange,
}: {
  value: ReasoningEffort | null;
  supportedEfforts: readonly ReasoningEffort[] | null;
  disabled?: boolean;
  onChange: (value: ReasoningEffort | null) => void;
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
        aria-label="Reasoning effort"
        value={supportedEfforts?.includes(value as ReasoningEffort) ? value ?? '' : ''}
        disabled={disabled || unavailable}
        onChange={(event) => {
          const effort = event.target.value as ReasoningEffort;
          onChange(effort || null);
        }}
      >
        <option value="">
          {supportedEfforts === null
            ? 'Not configured'
            : unavailable ? 'Not supported' : 'Provider default'}
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
  return model?.reasoning_efforts ?? null;
}
