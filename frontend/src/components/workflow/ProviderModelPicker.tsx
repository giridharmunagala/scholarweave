import { useId } from 'react';
import {
  modelReferenceLabel,
  modelSourceLabel,
  normalizeModelReference,
  resolveEffectiveModelReference,
  type ModelReferenceSource,
} from '../../lib/models';
import type {
  ModelCapability,
  ModelReference,
  ProviderProfileResponse,
  SettingsResponse,
  WorkflowModelDefaults,
} from '../../types/api';

interface ProviderModelPickerProps {
  id: string;
  label: string;
  capability: ModelCapability;
  value: ModelReference | null | undefined;
  onChange: (value: ModelReference | null) => void;
  profiles: ProviderProfileResponse[];
  settings?: Pick<SettingsResponse, 'default_model_references'> | null;
  workflowDefaults?: WorkflowModelDefaults | null;
  allowInherited?: boolean;
  hint?: string;
  className?: string;
  selectionSource?: ModelReferenceSource;
}

const capabilityLabel: Record<ModelCapability, string> = {
  chat: 'chat',
  embedding: 'embedding',
  vision: 'vision',
  tools: 'tool-capable',
};

export function ProviderModelPicker({
  id,
  label,
  capability,
  value,
  onChange,
  profiles,
  settings,
  workflowDefaults,
  allowInherited = true,
  hint,
  className = '',
  selectionSource,
}: ProviderModelPickerProps) {
  const generatedId = useId().replace(/:/g, '');
  const selected = normalizeModelReference(value);
  const effective = resolveEffectiveModelReference(capability, selected, workflowDefaults, settings, profiles);
  const selectedProfile = selected?.provider_profile_id
    ? profiles.find((profile) => profile.id === selected.provider_profile_id) ?? null
    : null;
  const matchingModels = (selectedProfile?.models || []).filter(
    (model) => model.capabilities.length === 0 || model.capabilities.includes(capability) || model.name === selected?.model,
  );
  const datalistId = `${id}-${generatedId}-models`;
  const currentProfileId = selected?.provider_profile_id || '';

  const setProfile = (profileId: string) => {
    if (!profileId) {
      onChange(allowInherited ? null : { model: selected?.model || null, provider_profile_id: null });
      return;
    }
    const profile = profiles.find((entry) => entry.id === profileId);
    const sameProfile = profileId === selected?.provider_profile_id;
    const firstModel = profile?.models.find((model) => model.capabilities.length === 0 || model.capabilities.includes(capability));
    onChange({
      provider_profile_id: profileId,
      model: sameProfile ? selected?.model || null : firstModel?.name || null,
    });
  };

  const issueText = effective.issue
    ? {
        'missing-profile': 'The selected profile no longer exists.',
        'archived-profile': 'This profile is archived and cannot run new work.',
        'missing-model': 'This model is not currently listed by the profile.',
        incompatible: `This model is not marked ${capabilityLabel[capability]}.`,
      }[effective.issue]
    : '';
  const displayedSource = selected && selectionSource ? selectionSource : effective.source;

  return (
    <div className={`provider-model-picker ${className}`}>
      <label className="field-label" htmlFor={`${id}-profile`}>
        {label}
      </label>
      <div className="provider-model-picker-fields">
        <select id={`${id}-profile`} className="input" value={currentProfileId} onChange={(event) => setProfile(event.target.value)}>
          {allowInherited ? <option value="">Inherit a model</option> : <option value="">Select a profile</option>}
          {profiles.map((profile) => (
            <option key={profile.id} value={profile.id} disabled={profile.state === 'archived' && profile.id !== currentProfileId}>
              {profile.name}
              {profile.state === 'archived' ? ' (archived)' : ''}
            </option>
          ))}
        </select>
        <input
          id={`${id}-model`}
          className="input"
          list={datalistId}
          value={selected?.model || ''}
          placeholder={selectedProfile ? 'Choose or enter a model' : allowInherited ? 'Enter a model name or inherit' : 'Model name'}
          onChange={(event) => {
            const model = event.target.value;
            const profileId = selected?.provider_profile_id || null;
            onChange(model || profileId ? { provider_profile_id: profileId, model: model || null } : null);
          }}
        />
        <datalist id={datalistId}>
          {matchingModels.map((model) => (
            <option key={model.name} value={model.name}>
              {model.capabilities.length ? model.capabilities.join(', ') : 'capability not tagged'}
            </option>
          ))}
        </datalist>
      </div>
      {hint ? <p className="field-hint">{hint}</p> : null}
      <p className={`model-effective${issueText ? ' warning' : ''}`} aria-live="polite">
        <strong>{selected ? `${modelSourceLabel(displayedSource)}:` : 'Effective:'}</strong> {modelReferenceLabel(effective.reference, profiles)}
        {effective.reference && !selected ? ` · ${modelSourceLabel(displayedSource)}` : ''}
        {issueText ? ` · ${issueText}` : ''}
      </p>
    </div>
  );
}
