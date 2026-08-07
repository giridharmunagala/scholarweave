import { Link } from '../../app/router';
import { ModelSelect, type ModelOption } from '../../shared/components/ModelSelect';
import { modelIsEnabled, type Provider, type Settings } from '../providers/api';
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
  const options = chatModelSelectOptions(providers);
  const defaultReference = settings.default_model_references.chat ?? {};

  return (
    <div className="chat-model-picker">
      <ModelSelect
        id="chat-model"
        options={options}
        value={value}
        disabled={disabled}
        emptyOptionLabel="Workspace default"
        emptyOptionHint={modelReferenceLabel(defaultReference, providers) || 'No default configured'}
        onChange={(reference) => onChange(reference as ModelReference)}
      />
      {!options.length ? (
        <small className="chat-model-picker-hint">
          No tool-capable models. <Link to="/settings">Configure providers</Link>.
        </small>
      ) : null}
    </div>
  );
}

export function chatModelSelectOptions(providers: Provider[]): ModelOption[] {
  return providers.flatMap((provider) =>
    provider.models
      .filter(
        (model) =>
          modelIsEnabled(model)
          && (
            !model.capabilities?.length
            || model.capabilities.includes('tools')
          ),
      )
      .map((model) => ({
        providerId: provider.id,
        providerName: provider.name,
        providerKind: provider.kind,
        model: model.name,
        capabilities: model.capabilities ?? [],
      })),
  );
}

export function chatModelOptions(providers: Provider[]) {
  return chatModelSelectOptions(providers).map((option) => ({
    value: encodeModelReference({
      provider_profile_id: option.providerId,
      model: option.model,
    }),
    label: `${option.providerName} / ${option.model}`,
  }));
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

export function modelReferenceLabel(reference: ModelReference, providers: Provider[]): string {
  if (!reference.provider_profile_id || !reference.model) return '';
  const provider = providers.find((item) => item.id === reference.provider_profile_id);
  return `${provider?.name ?? 'Unknown provider'} / ${reference.model}`;
}
