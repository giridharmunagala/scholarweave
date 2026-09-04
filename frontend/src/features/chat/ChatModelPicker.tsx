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
      <span className="chat-model-picker-label">Main</span>
      <ModelSelect
        id="chat-model"
        ariaLabel="Main model"
        options={options}
        value={value}
        disabled={disabled}
        emptyOptionLabel="Workspace default"
        emptyOptionHint={modelReferenceLabel(defaultReference, providers) || 'No default configured'}
        onChange={(reference) => onChange(reference as ModelReference)}
      />
      {!options.length ? (
        <small className="chat-model-picker-hint">
          No enabled models. <Link to="/settings">Configure providers</Link>.
        </small>
      ) : null}
    </div>
  );
}

export function chatModelSelectOptions(providers: Provider[]): ModelOption[] {
  return providers
    .filter((provider) => provider.state !== 'archived')
    .flatMap((provider) =>
      provider.models
        .filter(modelIsEnabled)
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

export function preferredChatModel(
  settings: Pick<Settings, 'default_model_references' | 'last_chat_model_reference'>,
): ModelReference {
  const preferred = settings.last_chat_model_reference ?? {};
  return preferred.provider_profile_id && preferred.model
    ? preferred
    : settings.default_model_references.chat ?? {};
}

/**
 * The reference the run will actually use. An empty picker means "workspace
 * default", so anything that inspects the model — reasoning levels, for
 * instance — has to look at the default rather than the blank selection.
 */
export function resolveModelReference(
  reference: ModelReference,
  settings: Pick<Settings, 'default_model_references'> | null,
): ModelReference {
  if (reference.provider_profile_id && reference.model) return reference;
  return settings?.default_model_references.chat ?? {};
}
