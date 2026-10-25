import { Panel } from '../../shared/components/Ui';
import type { Settings } from './api';

export function ProfilePanel({
  settings,
  onChange,
}: {
  settings: Settings;
  onChange: (settings: Settings) => void;
}) {
  return (
    <Panel
      title="User context"
      description="Included in every agent, planner, and worker prompt together with the current localized time."
    >
      <div className="stack">
        <label className="field">
          Timezone
          <input
            value={settings.user_timezone}
            placeholder="Asia/Kolkata"
            onChange={(event) => onChange({ ...settings, user_timezone: event.target.value })}
          />
          <small>Use an IANA timezone such as Asia/Kolkata.</small>
        </label>
        <label className="field">
          Profile
          <textarea
            rows={5}
            value={settings.user_profile}
            placeholder="Location, preferences, background, and recurring constraints."
            onChange={(event) => onChange({ ...settings, user_profile: event.target.value })}
          />
        </label>
      </div>
    </Panel>
  );
}

export function DocumentsPanel({
  settings,
  onChange,
}: {
  settings: Settings;
  onChange: (settings: Settings) => void;
}) {
  return (
    <Panel
      title="Document OCR"
      description="Use Surya for batch extraction, optional Indra CPU recovery and quick reading, or Tesseract for scanned pages."
    >
      <div className="stack">
        <label className="field">
          OCR engine
          <select value={settings.ocr_engine} onChange={(event) => {
            const engine = event.target.value;
            if (engine === 'surya' || engine === 'tesseract') onChange({ ...settings, ocr_engine: engine });
          }}>
            <option value="tesseract">Tesseract (scanned-page fallback)</option>
            <option value="surya">Surya 2 (layout, Markdown and figures)</option>
          </select>
        </label>
        {settings.ocr_engine === 'surya' ? <>
          <p>Automatic ingestion reads every page with Surya, including selectable PDFs. Use embedded text in the paper's extraction controls to opt out. Headings, reading order, tables, equations and figure crops are retained; page headers and footers stay in the layout manifest.</p>
          <label className="field">
            Surya server URL
            <input value={settings.ocr_surya_base_url} onChange={(event) => onChange({ ...settings, ocr_surya_base_url: event.target.value })} />
            <small>Loopback OpenAI-compatible endpoint ending in /v1. No document data leaves this machine.</small>
          </label>
          <label className="field">
            Surya model ID
            <input value={settings.ocr_surya_model} onChange={(event) => onChange({ ...settings, ocr_surya_model: event.target.value })} />
            <small>Use the exact alias reported by the server's /v1/models endpoint.</small>
          </label>
          <div className="field-row">
            <label className="field">
              Parallel OCR pages
              <input type="number" min={1} value={settings.ocr_surya_parallel} onChange={(event) => onChange({ ...settings, ocr_surya_parallel: Number(event.target.value) })} />
              <small>Shared across papers, with no fixed app ceiling. Keep at or below llama-server --parallel and within available memory; 10 is a starting point, not a measured throughput guarantee.</small>
            </label>
            <label className="field">
              OCR output tokens
              <input type="number" min={512} max={32768} value={settings.ocr_surya_max_tokens} onChange={(event) => onChange({ ...settings, ocr_surya_max_tokens: Number(event.target.value) })} />
              <small>8192 leaves image/prompt headroom in a 16k slot. Dense or truncated pages retry with layout and block OCR.</small>
            </label>
          </div>
          <div className="setting-row">
            <div className="setting-label">
              <strong>Indra CPU recovery</strong>
              <small>Recover only pages Surya cannot read. Experimental: verify equations, table values and reading order against the PDF. Models must be downloaded locally first.</small>
            </div>
            <Toggle
              checked={settings.ocr_indra_fallback_enabled}
              label="Indra CPU recovery"
              onChange={(checked) => onChange({ ...settings, ocr_indra_fallback_enabled: checked })}
            />
          </div>
        </> : <p>Native text is preserved when readable; Tesseract runs on scanned or text-poor pages. Choose Run OCR on a paper to process every page.</p>}
        <p>Use Quick CPU OCR in the paper reader for fast local Indra extraction without changing the batch engine. Indra is best for prose, not exact equations or complex tables.</p>
        <p className="field-hint">Download CPU models once with <code>hf download datalab-to/indra-ocr --revision v1.1.0</code>. Processing is local; no automatic model downloads run during ingestion.</p>

        {settings.ocr_engine !== 'surya' ? <>
        <div className="setting-row">
          <div className="setting-label">
            <strong>Vision enhancement pass</strong>
            <small>Re-reads low-confidence OCR pages with the default vision model before indexing.</small>
          </div>
          <Toggle
            checked={settings.ocr_llm_enhancement_enabled}
            label="Vision enhancement pass"
            onChange={(checked) => onChange({ ...settings, ocr_llm_enhancement_enabled: checked })}
          />
        </div>

        {settings.ocr_llm_enhancement_enabled ? (
          <div className="field-row setting-nested">
            <label className="field">
              Legacy enhancement fallback
              <input
                value={settings.ocr_llm_model ?? ''}
                placeholder="No legacy fallback"
                onChange={(event) => onChange({ ...settings, ocr_llm_model: event.target.value || null })}
              />
              <small>
                Used only when the request, agent, and settings have no vision model selected.
                The name resolves on Default Ollama, or the first active provider if that profile is unavailable.
                {settings.default_model_references.vision?.model ? ' A vision default is selected, so this fallback will not be used.' : ''}
                {' '}Choose a provider and model in Models to set the normal vision default.
              </small>
            </label>
            <label className="field">
              Ollama triage model (same provider)
              <input
                value={settings.ocr_llm_triage_model ?? ''}
                placeholder="Use the enhancement model"
                onChange={(event) =>
                  onChange({ ...settings, ocr_llm_triage_model: event.target.value || null })
                }
              />
              <small>
                Used only when enhancement runs on Ollama, on that same provider.
                Other providers use the enhancement model for triage and validation.
                An explicit per-document triage choice takes precedence.
              </small>
            </label>
          </div>
        ) : null}
        </> : null}
      </div>
    </Panel>
  );
}

export function RuntimePanel({
  settings,
  onChange,
}: {
  settings: Settings;
  onChange: (settings: Settings) => void;
}) {
  return (
    <>
      <Panel title="Agent runtime" description="Runs have no automatic turn, epoch, or elapsed-time ceiling. The model context window governs memory; cancellation and stalled-request detection remain available.">
        <div className="stack">
          <div className="field-row setting-nested">
            <label className="field">
              Checkpoint interval (model turns)
              <input
                type="number"
                min={2}
                max={100}
                value={settings.agent_epoch_max_turns}
                onChange={(event) =>
                  onChange({ ...settings, agent_epoch_max_turns: Number(event.target.value) })
                }
              />
              <small>Turns between epoch checkpoints, not a total turn limit for chat or Deep Work.</small>
            </label>
          </div>
          <div className="field-row setting-nested">
            <label className="field">
              Safe-read attempts
              <input
                type="number"
                min={1}
                max={5}
                value={settings.tool_read_retry_attempts}
                onChange={(event) =>
                  onChange({ ...settings, tool_read_retry_attempts: Number(event.target.value) })
                }
              />
            </label>
          </div>
          <div className="setting-row">
            <div className="setting-label">
              <strong>Model-context memory management</strong>
              <small>
                Uses the selected model&apos;s configured context capacity. Tool outputs remain intact
                while they fit; older evidence is archived and summarized when space is needed.
                Exact archived content stays readable.
              </small>
            </div>
          </div>
          <div className="field-row setting-nested">
            <label className="field">
              Initial response allowance (tokens)
              <input
                type="number"
                min={256}
                max={128000}
                value={settings.agent_context_response_reserve_tokens}
                onChange={(event) =>
                  onChange({ ...settings, agent_context_response_reserve_tokens: Number(event.target.value) })
                }
              />
              <small>Starting allowance, including reasoning. Automatically grows on truncation within available model context; not a hard output cap.</small>
            </label>
          </div>
          <div className="setting-row">
            <div className="setting-label">
              <strong>Summarize older conversation with the model</strong>
              <small>Last resort after older cached tool output is evicted; disabling uses deterministic excerpts with less detail.</small>
            </div>
            <Toggle
              checked={settings.agent_context_model_summary_enabled}
              label="Summarize older conversation with the model"
              onChange={(checked) => onChange({ ...settings, agent_context_model_summary_enabled: checked })}
            />
          </div>
          <div className="field-row setting-nested">
            <label className="field">
              Context window fallback
              <input
                type="number"
                min={4096}
                value={settings.agent_context_window_tokens}
                onChange={(event) =>
                  onChange({ ...settings, agent_context_window_tokens: Number(event.target.value) })
                }
              />
              <small>Used only when the selected model&apos;s context window is unknown.</small>
            </label>
            <label className="field">
              Compaction high-water ratio
              <input
                type="number"
                min={0.5}
                max={0.95}
                step={0.05}
                value={settings.agent_context_high_water_ratio}
                onChange={(event) =>
                  onChange({ ...settings, agent_context_high_water_ratio: Number(event.target.value) })
                }
              />
              <small>Fraction of the available input budget that triggers context cleanup; 0.85 means 85%.</small>
            </label>
          </div>
          <div className="field-row setting-nested">
            <label className="field">
              Retrieval context characters
              <input
                type="number"
                min={1000}
                step={1000}
                value={settings.retrieval_max_context_chars}
                onChange={(event) =>
                  onChange({ ...settings, retrieval_max_context_chars: Number(event.target.value) })
                }
              />
            </label>
          </div>
        </div>
      </Panel>
      <Panel title="Storage" description="Read-only paths resolved at startup.">
        <dl className="path-list">
          {[
            ['Data directory', settings.data_dir],
            ['Database', settings.database_path],
            ['Documents', settings.documents_dir],
            ['Workspace', settings.workspace_dir],
            ['Artifacts', settings.artifacts_dir],
          ].map(([label, value]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd><code>{value}</code></dd>
            </div>
          ))}
        </dl>
      </Panel>
    </>
  );
}

function Toggle({
  checked,
  label,
  onChange,
}: {
  checked: boolean;
  label: string;
  onChange: (checked: boolean) => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      className="toggle"
      onClick={() => onChange(!checked)}
    >
      <span />
    </button>
  );
}
