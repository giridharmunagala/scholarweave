import { Icon } from '../../shared/components/Icons';
import { ModelSelect, type ModelOption } from '../../shared/components/ModelSelect';
import { Panel } from '../../shared/components/Ui';
import {
  modelIsEnabled,
  type Provider,
  type Settings,
} from './api';

const CAPABILITIES = [
  {
    key: 'chat',
    label: 'Chat & reasoning',
    icon: 'builder',
    description: 'Drives agent conversations, tool calling, and generated research notes.',
  },
  {
    key: 'small_tasks',
    label: 'Small tasks',
    icon: 'sparkle',
    description: 'Optional smaller model for topic names, paper summaries, and conversation-context summaries. Main chat, research planning, comparisons, and final answers stay on the larger model. Your llama router handles switching.',
  },
  {
    key: 'summary',
    label: 'Paper summaries',
    icon: 'builder',
    description: 'Optional override for saved paper summaries and new research paper briefs. Otherwise uses the small tasks model, or the main model when unset.',
  },
  {
    key: 'decision',
    label: 'System One decisions',
    icon: 'search',
    description: 'Screens Deep Work papers with parallel native decisions. Choose an enabled System One model.',
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
  {
    key: 'compaction',
    label: 'Context maintenance',
    icon: 'builder',
    description: 'Optional override for older conversation summaries. Otherwise uses the small tasks model. Uses the main agent if no helper is configured, or if the helper fails or cannot fit the history.',
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
          const helperOverride = capability.key === 'compaction' || capability.key === 'summary';
          const smallTasks = settings.default_model_references.small_tasks;
          const emptyLabel = helperOverride && (smallTasks?.model || smallTasks?.provider_profile_id)
            ? 'Use small tasks model'
            : helperOverride || capability.key === 'small_tasks' ? 'Use main agent model' : 'Not configured';
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
                ariaLabel={`Default ${capability.label} model`}
                options={options}
                value={current}
                emptyOptionLabel={emptyLabel}
                emptyOptionHint={helperOverride ? 'Inherit the small tasks model when configured' : capability.key === 'small_tasks'
                  ? 'Keep all tasks on the main model' : 'Agents must then pick a model explicitly'}
                placeholder={emptyLabel}
                inline
                onChange={(reference) => setReference(capability.key, reference)}
              />
              {!options.length ? (
                <small className="field-hint">
                  No enabled {capability.key} models. Discover or register models in Providers.
                </small>
              ) : null}
              {capability.key === 'decision' ? <label className="field-hint">
                Parallel decision calls
                <input aria-label="Parallel decision calls" type="number" min={1} max={32} step={1}
                  value={settings.decision_parallel_requests} onChange={(event) => {
                    const value = Number(event.target.value);
                    if (Number.isInteger(value) && value >= 1 && value <= 32) {
                      onChange({ ...settings, decision_parallel_requests: value });
                    }
                  }} />
                <small>1–32 concurrent calls. Default: 8. Changes apply to subsequent requests without restarting.</small>
              </label> : null}
            </div>
          );
        })}
      </div>
    </Panel>
  );
}

export type ModelCapability =
  | 'chat'
  | 'small_tasks'
  | 'summary'
  | 'embedding'
  | 'vision'
  | 'tools'
  | 'decision'
  | 'compaction';

export function capabilityOptions(providers: Provider[], capability: ModelCapability): ModelOption[] {
  return providers.filter((provider) => capability !== 'decision' || provider.kind === 'openai_compatible').flatMap((provider) =>
    provider.models
      .filter(
        (model) =>
          modelIsEnabled(model)
          && ((capability !== 'decision' && !model.capabilities?.length) || model.capabilities?.includes(
            capability === 'compaction' || capability === 'summary' || capability === 'small_tasks' ? 'chat' : capability,
          )),
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
