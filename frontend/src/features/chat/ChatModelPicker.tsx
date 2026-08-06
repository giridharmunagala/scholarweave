import { Link } from '../../app/router';
import type { Provider, Settings } from '../providers/api';
import type { ModelReference } from './api';

export function ChatModelPicker({
  providers,
  settings,
  value,
  disabled,
  onChange,
}: {
  providers: Provider[];
  settings: Settings;
  value: ModelReference;
  disabled: boolean;
  onChange: (reference: ModelReference) => void;
}) {
  const encoded = encodeModelReference(value);
  const options = chatModelOptions(providers);
  const available = !encoded || options.some((option) => option.value === encoded);
  const defaultReference =
    settings.default_model_references.tools
    ?? settings.default_model_references.chat
    ?? {};

  return (
    <label className="chat-model-picker">
      <span>Model</span>
      <select
        value={encoded}
        disabled={disabled}
        onChange={(event) => onChange(decodeModelReference(event.target.value))}
      >
        <option value="">Default{modelReferenceLabel(defaultReference, providers)}</option>
        {!available ? <option value={encoded}>Unavailable: {value.model}</option> : null}
        {options.map((option) => (
          <option value={option.value} key={option.value}>{option.label}</option>
        ))}
      </select>
      <small>
        {options.length ? (
          'Changing model starts a new chat.'
        ) : (
          <>No discovered tool-capable models. <Link to="/settings">Configure providers</Link>.</>
        )}
      </small>
    </label>
  );
}

export function chatModelOptions(providers: Provider[]) {
  return providers.flatMap((provider) =>
    provider.models
      .filter(
        (model) =>
          !model.capabilities?.length
          || model.capabilities.includes('tools'),
      )
      .map((model) => ({
        value: encodeModelReference({
          provider_profile_id: provider.id,
          model: model.name,
        }),
        label: `${provider.name} / ${model.name}`,
      })),
  );
}

export function encodeModelReference(reference: ModelReference): string {
  return reference.provider_profile_id && reference.model
    ? JSON.stringify([reference.provider_profile_id, reference.model])
    : '';
}

export function decodeModelReference(encoded: string): ModelReference {
  if (!encoded) return {};
  const [provider_profile_id, model] = JSON.parse(encoded) as [string, string];
  return { provider_profile_id, model };
}

function modelReferenceLabel(reference: ModelReference, providers: Provider[]): string {
  if (!reference.provider_profile_id || !reference.model) return '';
  const provider = providers.find((item) => item.id === reference.provider_profile_id);
  return ` · ${provider?.name ?? 'Unknown provider'} / ${reference.model}`;
}
