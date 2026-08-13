import { useEffect, useState } from 'react';
import { Icon } from '../../shared/components/Icons';
import { ModelSelect, type ModelOption } from '../../shared/components/ModelSelect';
import { Panel } from '../../shared/components/Ui';
import {
  modelIsEnabled,
  providersApi,
  type BuiltInSpeechStatus,
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
    key: 'speech',
    label: 'Speech recognition',
    icon: 'microphone',
    description: 'Transcribes microphone recordings into chat prompts.',
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
  const [builtInSpeech, setBuiltInSpeech] = useState<BuiltInSpeechStatus | null>(null);
  const [speechBusy, setSpeechBusy] = useState(false);
  const [confirmSpeechDelete, setConfirmSpeechDelete] = useState(false);
  const [speechError, setSpeechError] = useState<string | null>(null);

  useEffect(() => {
    providersApi.builtInSpeechStatus()
      .then(setBuiltInSpeech)
      .catch((error) => setSpeechError(String(error)));
  }, []);

  useEffect(() => {
    if (builtInSpeech?.state !== 'installing') return;
    const timer = window.setInterval(() => {
      void providersApi.builtInSpeechStatus()
        .then(setBuiltInSpeech)
        .catch((error) => setSpeechError(String(error)));
    }, 750);
    return () => window.clearInterval(timer);
  }, [builtInSpeech?.state]);

  const installSpeech = async () => {
    setSpeechBusy(true);
    setSpeechError(null);
    try {
      setBuiltInSpeech(await providersApi.installBuiltInSpeech());
    } catch (error) {
      setSpeechError(String(error));
    } finally {
      setSpeechBusy(false);
    }
  };

  const uninstallSpeech = async () => {
    setSpeechBusy(true);
    setSpeechError(null);
    try {
      setBuiltInSpeech(await providersApi.uninstallBuiltInSpeech());
      setConfirmSpeechDelete(false);
    } catch (error) {
      setSpeechError(String(error));
    } finally {
      setSpeechBusy(false);
    }
  };
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
      <div className="builtin-speech-card">
        <span className="default-model-icon">
          <Icon name="microphone" size={16} />
        </span>
        <div className="builtin-speech-copy">
          <strong>Built-in speech recognition</strong>
          <small>
            Nemotron ASR Streaming 0.6B runs locally with English-only transcription.
          </small>
          {builtInSpeech?.state === 'installing' ? (
            <progress
              max={builtInSpeech.total_bytes}
              value={builtInSpeech.downloaded_bytes}
              aria-label="Downloading built-in speech model"
            />
          ) : null}
          {speechError || builtInSpeech?.error ? (
            <small className="field-error">{speechError || builtInSpeech?.error}</small>
          ) : null}
        </div>
        <div className="button-row builtin-speech-actions">
          {builtInSpeech && !builtInSpeech.available ? (
            <span className="tag">Unavailable</span>
          ) : builtInSpeech?.state === 'error' ? (
            <button
              className="button secondary small"
              type="button"
              disabled={speechBusy}
              onClick={() => void installSpeech()}
            >
              Retry install
            </button>
          ) : builtInSpeech?.state === 'ready' || builtInSpeech?.state === 'running' ? (
            confirmSpeechDelete ? (
              <>
                <button
                  className="button danger small"
                  type="button"
                  disabled={speechBusy}
                  onClick={() => void uninstallSpeech()}
                >
                  Delete files
                </button>
                <button
                  className="button ghost small"
                  type="button"
                  disabled={speechBusy}
                  onClick={() => setConfirmSpeechDelete(false)}
                >
                  Cancel
                </button>
              </>
            ) : (
              <>
                <span className="tag">{builtInSpeech.running ? 'Running' : 'Installed'}</span>
                <button
                  className="button danger small"
                  type="button"
                  disabled={speechBusy}
                  onClick={() => setConfirmSpeechDelete(true)}
                >
                  Delete model
                </button>
              </>
            )
          ) : (
            <button
              className="button secondary small"
              type="button"
              disabled={
                speechBusy
                || builtInSpeech?.state === 'installing'
                || builtInSpeech?.available === false
              }
              onClick={() => void installSpeech()}
            >
              {builtInSpeech?.state === 'installing' ? 'Downloading…' : 'Install built-in model'}
            </button>
          )}
        </div>
      </div>
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

export type ModelCapability = 'chat' | 'embedding' | 'vision' | 'tools' | 'speech';

export function capabilityOptions(providers: Provider[], capability: ModelCapability): ModelOption[] {
  return providers.flatMap((provider) =>
    provider.models
      .filter(
        (model) =>
          modelIsEnabled(model)
          && (
            capability === 'speech'
              ? model.capabilities?.includes('speech')
              : !model.capabilities?.length || model.capabilities.includes(capability)
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
