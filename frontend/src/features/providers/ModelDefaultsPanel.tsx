import { Icon } from '../../shared/components/Icons';
import { Panel } from '../../shared/components/Ui';
import type { Provider, Settings } from './api';

const capabilities = [
  ['chat', 'Chat'],
  ['tools', 'Tool calling'],
  ['embedding', 'Embeddings'],
  ['vision', 'Vision enhancement'],
] as const;

export function ModelDefaultsPanel({
  settings,
  providers,
  onChange,
  onSave,
  saving,
}: {
  settings: Settings;
  providers: Provider[];
  onChange: (settings: Settings) => void;
  onSave: () => void;
  saving: boolean;
}) {
  const setReference = (capability: string, encoded: string) => {
    const references = { ...settings.default_model_references };
    if (!encoded) {
      delete references[capability];
    } else {
      const [provider_profile_id, model] = JSON.parse(encoded) as [string, string];
      references[capability] = { provider_profile_id, model };
    }
    onChange({ ...settings, default_model_references: references });
  };

  return (
    <>
      <Panel
        title="Agents SDK defaults"
        description="These models are inherited when an Agent does not select an explicit provider and model."
      >
      <div className="stack">
        <div className="field-row">
          {capabilities.map(([capability, label]) => {
            const current = settings.default_model_references[capability];
            const value =
              current?.provider_profile_id && current.model
                ? JSON.stringify([current.provider_profile_id, current.model])
                : '';
            const currentAvailable =
              !current ||
              providers.some(
                (provider) =>
                  provider.id === current.provider_profile_id &&
                  provider.models.some((model) => model.name === current.model),
              );
            return (
              <label className="field" key={capability}>
                {label}
                <select value={value} onChange={(event) => setReference(capability, event.target.value)}>
                  <option value="">Not configured</option>
                  {!currentAvailable && current ? (
                    <option value={value}>
                      Missing: {current.model}
                    </option>
                  ) : null}
                  {providers.flatMap((provider) =>
                    provider.models
                      .filter(
                        (model) =>
                          !model.capabilities?.length ||
                          model.capabilities.includes(capability),
                      )
                      .map((model) => (
                        <option
                          key={`${provider.id}:${model.name}`}
                          value={JSON.stringify([provider.id, model.name])}
                        >
                          {provider.name} / {model.name}
                        </option>
                      )),
                  )}
                </select>
              </label>
            );
          })}
        </div>
        <div className="field-row">
          <label className="field">
            Compaction threshold
            <input
              type="number"
              min={4}
              value={settings.agent_compaction_threshold_items}
              onChange={(event) =>
                onChange({
                  ...settings,
                  agent_compaction_threshold_items: Number(event.target.value),
                })
              }
            />
          </label>
          <label className="field">
            Recent items kept
            <input
              type="number"
              min={2}
              value={settings.agent_compaction_recent_items}
              onChange={(event) =>
                onChange({
                  ...settings,
                  agent_compaction_recent_items: Number(event.target.value),
                })
              }
            />
          </label>
          <label className="field">
            Retrieval context characters
            <input
              type="number"
              min={1000}
              value={settings.retrieval_max_context_chars}
              onChange={(event) =>
                onChange({
                  ...settings,
                  retrieval_max_context_chars: Number(event.target.value),
                })
              }
            />
          </label>
        </div>
        <div className="field-row">
          <label className="field">
            Python timeout
            <input
              type="number"
              min={1}
              value={settings.python_tool_timeout_seconds}
              onChange={(event) =>
                onChange({
                  ...settings,
                  python_tool_timeout_seconds: Number(event.target.value),
                })
              }
            />
          </label>
          <label className="field">
            Python memory MB
            <input
              type="number"
              min={32}
              value={settings.python_tool_memory_mb}
              onChange={(event) =>
                onChange({
                  ...settings,
                  python_tool_memory_mb: Number(event.target.value),
                })
              }
            />
          </label>
        </div>
        <div className="button-row">
          <label className="check-row">
            <input
              type="checkbox"
              checked={settings.agent_tracing_enabled}
              onChange={(event) =>
                onChange({ ...settings, agent_tracing_enabled: event.target.checked })
              }
            />
            SDK tracing
          </label>
          <label className="check-row">
            <input
              type="checkbox"
              checked={settings.python_tool_enabled}
              onChange={(event) =>
                onChange({ ...settings, python_tool_enabled: event.target.checked })
              }
            />
            Custom Python FunctionTools
          </label>
        </div>
      </div>
      </Panel>
      <Panel
        title="Document OCR"
        description="Choose the local OCR engine used for scanned or text-poor PDF pages."
      >
        <div className="stack">
          <div className="field-row">
            <label className="field">
              OCR engine
              <select
                value={settings.ocr_engine}
                onChange={(event) =>
                  onChange({
                    ...settings,
                    ocr_engine: event.target.value as Settings['ocr_engine'],
                  })
                }
              >
                <option value="tesseract">Tesseract</option>
                <option value="surya">Surya OCR 2</option>
              </select>
            </label>
            {settings.ocr_engine === 'surya' ? (
              <>
                <label className="field">
                  Hugging Face model
                  <input
                    value={settings.surya_model}
                    onChange={(event) =>
                      onChange({ ...settings, surya_model: event.target.value })
                    }
                  />
                </label>
                <label className="field">
                  Device
                  <select
                    value={settings.surya_device}
                    onChange={(event) =>
                      onChange({
                        ...settings,
                        surya_device: event.target.value as Settings['surya_device'],
                      })
                    }
                  >
                    <option value="auto">Auto</option>
                    <option value="cuda">CUDA</option>
                    <option value="cpu">CPU</option>
                  </select>
                </label>
              </>
            ) : null}
          </div>
          {settings.ocr_engine === 'surya' ? (
            <>
              <div className="field-row">
                <label className="field">
                  Maximum output tokens
                  <input
                    type="number"
                    min={512}
                    max={32768}
                    value={settings.surya_max_new_tokens}
                    onChange={(event) =>
                      onChange({
                        ...settings,
                        surya_max_new_tokens: Number(event.target.value),
                      })
                    }
                  />
                </label>
                <label className="field">
                  Maximum image width
                  <input
                    type="number"
                    min={512}
                    max={4096}
                    value={settings.surya_max_image_width}
                    onChange={(event) =>
                      onChange({
                        ...settings,
                        surya_max_image_width: Number(event.target.value),
                      })
                    }
                  />
                </label>
                <label className="field">
                  Worker timeout (seconds)
                  <input
                    type="number"
                    min={30}
                    max={7200}
                    value={settings.surya_timeout_seconds}
                    onChange={(event) =>
                      onChange({
                        ...settings,
                        surya_timeout_seconds: Number(event.target.value),
                      })
                    }
                  />
                </label>
              </div>
              <label className="check-row">
                <input
                  type="checkbox"
                  checked={settings.surya_unload_ollama_models}
                  onChange={(event) =>
                    onChange({
                      ...settings,
                      surya_unload_ollama_models: event.target.checked,
                    })
                  }
                />
                Unload resident Ollama models before Surya starts
              </label>
              <small className="field-hint">
                Surya runs in an isolated process and releases CUDA memory when each OCR
                job ends. Its model weights use the modified OpenRAIL-M license.
              </small>
            </>
          ) : null}
          <label className="check-row">
            <input
              type="checkbox"
              checked={settings.ocr_llm_enhancement_enabled}
              onChange={(event) =>
                onChange({
                  ...settings,
                  ocr_llm_enhancement_enabled: event.target.checked,
                })
              }
            />
            Run a vision-model enhancement pass after OCR
          </label>
          <div className="button-row">
            <span className="save-spacer" />
            <button className="button" type="button" disabled={saving} onClick={onSave}>
              <Icon name="check" />
              {saving ? 'Saving…' : 'Save settings'}
            </button>
          </div>
        </div>
      </Panel>
    </>
  );
}
