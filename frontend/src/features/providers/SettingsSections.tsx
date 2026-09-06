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
      description="ScholarWeave uses embedded PDF text first, then Tesseract only for scanned or text-poor pages."
    >
      <div className="stack">
        <div className="setting-row">
          <div className="setting-label">
            <strong>Tesseract fallback</strong>
            <small>
              Native text is preserved whenever a page has enough readable content. OCR runs only
              when that text is missing or insufficient.
            </small>
          </div>
          <span className="status-pill neutral">Tesseract</span>
        </div>

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
              Enhancement model override
              <input
                value={settings.ocr_llm_model ?? ''}
                placeholder="Use the default vision model"
                onChange={(event) => onChange({ ...settings, ocr_llm_model: event.target.value || null })}
              />
            </label>
            <label className="field">
              Triage model override
              <input
                value={settings.ocr_llm_triage_model ?? ''}
                placeholder="Use the default vision model"
                onChange={(event) =>
                  onChange({ ...settings, ocr_llm_triage_model: event.target.value || null })
                }
              />
            </label>
          </div>
        ) : null}
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
