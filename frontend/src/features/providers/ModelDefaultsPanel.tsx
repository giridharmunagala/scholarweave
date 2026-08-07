import { Icon } from '../../shared/components/Icons';
import { ModelSelect, type ModelOption } from '../../shared/components/ModelSelect';
import { Panel } from '../../shared/components/Ui';
import { modelIsEnabled, type Provider, type Settings } from './api';

const CAPABILITIES = [
  {
    key: 'chat',
    label: 'Chat & reasoning',
    icon: 'builder',
    description: 'Drives agent conversations, tool calling, and generated research notes.',
  },
  {
    key: 'embedding',
    label: 'Embeddings',
    icon: 'search',
    description: 'Indexes documents and powers retrieval over your library.',
  },
  {
    key: 'vision',
    label: 'Vision enhancement',
    icon: 'scan',
    description: 'Cleans up OCR output on scanned or text-poor pages.',
  },
] as const;

export function ModelDefaultsPanel({
  settings,
  providers,
  onChange,
}: {
  settings: Settings;
  providers: Provider[];
  onChange: (settings: Settings) => void;
}) {
  const setReference = (capability: string, reference: { provider_profile_id?: string | null; model?: string | null }) => {
    const references = { ...settings.default_model_references };
    if (!reference.provider_profile_id || !reference.model) delete references[capability];
    else references[capability] = { provider_profile_id: reference.provider_profile_id, model: reference.model };
    onChange({ ...settings, default_model_references: references });
  };

  const totalModels = providers.reduce(
    (total, provider) => total + provider.models.filter(modelIsEnabled).length,
    0,
  );

  return (
    <Panel
      title="Default models"
      description="Inherited whenever an agent, chat, or pipeline does not select an explicit provider and model."
      actions={<span className="tag">{totalModels} enabled models</span>}
    >
      <div className="default-model-grid">
        {CAPABILITIES.map((capability) => {
          const current = settings.default_model_references[capability.key] ?? {};
          const options = capabilityOptions(providers, capability.key);
          return (
            <div className="default-model-card" key={capability.key}>
              <div className="default-model-head">
                <span className="default-model-icon">
                  <Icon name={capability.icon} size={16} />
                </span>
                <div>
                  <strong>{capability.label}</strong>
                  <small>{capability.description}</small>
                </div>
              </div>
              <ModelSelect
                options={options}
                value={current}
                emptyOptionLabel="Not configured"
                emptyOptionHint="Agents must then pick a model explicitly"
                placeholder="Not configured"
                inline
                onChange={(reference) => setReference(capability.key, reference)}
              />
              {!options.length ? (
                <small className="field-hint">
                  No enabled {capability.key} models. Discover and enable models below.
                </small>
              ) : null}
            </div>
          );
        })}
      </div>
    </Panel>
  );
}

export type ModelCapability = 'chat' | 'embedding' | 'vision' | 'tools';

export function capabilityOptions(providers: Provider[], capability: ModelCapability): ModelOption[] {
  return providers.flatMap((provider) =>
    provider.models
      .filter(
        (model) =>
          modelIsEnabled(model)
          && (!model.capabilities?.length || model.capabilities.includes(capability)),
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
