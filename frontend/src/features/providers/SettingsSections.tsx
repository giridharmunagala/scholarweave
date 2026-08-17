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
  const docling = settings.ocr_engine === 'docling';
  return (
    <Panel
      title="Document OCR"
      description="Choose the local OCR engine used for scanned or text-poor PDF pages."
    >
      <div className="stack">
        <div className="setting-row">
          <div className="setting-label">
            <strong>OCR engine</strong>
            <small>
              {docling
                ? 'Docling parses every page to preserve layout and reading order, reconstructs tables, and applies RapidOCR where embedded text is insufficient.'
                : 'Tesseract is lighter and faster, but loses complex layout and table structure.'}
            </small>
          </div>
          <div className="segmented" role="group" aria-label="OCR engine">
            <button
              type="button"
              aria-pressed={!docling}
              onClick={() => onChange({ ...settings, ocr_engine: 'tesseract' })}
            >
              Tesseract
            </button>
            <button
              type="button"
              aria-pressed={docling}
              onClick={() => onChange({ ...settings, ocr_engine: 'docling' })}
            >
              Docling
            </button>
          </div>
        </div>

        {docling ? (
          <div className="field-row setting-nested">
            <label className="field">
              Device
              <select
                value={settings.docling_device}
                onChange={(event) =>
                  onChange({ ...settings, docling_device: event.target.value as Settings['docling_device'] })
                }
              >
                <option value="auto">Auto</option>
                <option value="cuda">CUDA</option>
                <option value="cpu">CPU</option>
              </select>
            </label>
            <label className="field">
              RapidOCR backend
              <select
                value={settings.docling_ocr_backend}
                onChange={(event) =>
                  onChange({
                    ...settings,
                    docling_ocr_backend: event.target.value as Settings['docling_ocr_backend'],
                  })
                }
              >
                <option value="onnxruntime">ONNX Runtime</option>
                <option value="torch">PyTorch</option>
              </select>
            </label>
            <label className="field">
              Page batch size
              <input
                type="number"
                min={1}
                max={32}
                value={settings.docling_batch_size}
                onChange={(event) =>
                  onChange({ ...settings, docling_batch_size: Number(event.target.value) })
                }
              />
            </label>
            <label className="field">
              CPU threads
              <input
                type="number"
                min={1}
                max={64}
                value={settings.docling_num_threads}
                onChange={(event) =>
                  onChange({ ...settings, docling_num_threads: Number(event.target.value) })
                }
              />
            </label>
          </div>
        ) : null}

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
      <Panel title="Agent runtime" description="Execution limits applied to every agent run.">
        <div className="stack">
          <div className="setting-row">
            <div className="setting-label">
              <strong>SDK tracing</strong>
              <small>Records detailed OpenAI Agents SDK traces for every run.</small>
            </div>
            <Toggle
              checked={settings.agent_tracing_enabled}
              label="SDK tracing"
              onChange={(checked) => onChange({ ...settings, agent_tracing_enabled: checked })}
            />
          </div>
          <div className="setting-row">
            <div className="setting-label">
              <strong>Custom Python FunctionTools</strong>
              <small>Lets agents execute sandboxed Python tools you define.</small>
            </div>
            <Toggle
              checked={settings.python_tool_enabled}
              label="Custom Python FunctionTools"
              onChange={(checked) => onChange({ ...settings, python_tool_enabled: checked })}
            />
          </div>
          <div className="field-row setting-nested">
            <label className="field">
              Python timeout (seconds)
              <input
                type="number"
                min={1}
                value={settings.python_tool_timeout_seconds}
                onChange={(event) =>
                  onChange({ ...settings, python_tool_timeout_seconds: Number(event.target.value) })
                }
              />
            </label>
            <label className="field">
              Python memory (MB)
              <input
                type="number"
                min={32}
                value={settings.python_tool_memory_mb}
                onChange={(event) =>
                  onChange({ ...settings, python_tool_memory_mb: Number(event.target.value) })
                }
              />
            </label>
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
