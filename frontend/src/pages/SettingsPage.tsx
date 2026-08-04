import { useEffect, useState } from 'react';
import { EmptyState } from '../components/common/EmptyState';
import { ErrorNotice } from '../components/common/ErrorNotice';
import { Icon } from '../components/common/Icon';
import { toMessage, useToast } from '../components/common/Toast';
import { ProviderManagement } from '../components/settings/ProviderManagement';
import { api } from '../lib/api';
import { parseImportList } from '../lib/agentConfig';
import { formatBytes, formatDateTime } from '../lib/format';
import type { ModelInfo, SettingsResponse, SettingsUpdate } from '../types/api';

const limitFields: { key: keyof SettingsUpdate; label: string; hint: string }[] = [
  { key: 'max_context_chars', label: 'Max context characters', hint: 'Upper bound on prompt context assembled per node.' },
  { key: 'max_chunk_chars', label: 'Max chunk characters', hint: 'Size of each chunk produced during ingestion.' },
  { key: 'max_map_items', label: 'Max map items', hint: 'Fan-out limit for map nodes.' },
  { key: 'max_repeat_iterations', label: 'Max repeat iterations', hint: 'Safety limit for repeat/loop nodes.' },
  { key: 'max_concurrent_nodes', label: 'Max concurrent nodes', hint: 'How many nodes may execute in parallel.' },
  { key: 'max_subworkflow_depth', label: 'Max subworkflow depth', hint: 'Maximum nesting depth for reusable workflow nodes.' },
];

export function SettingsPage() {
  const toast = useToast();
  const [settings, setSettings] = useState<SettingsResponse | null>(null);
  const [form, setForm] = useState<SettingsUpdate>({});
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [modelError, setModelError] = useState('');

  const refresh = async () => {
    const next = await api.getSettings();
    setSettings(next);
    setForm({
      ollama_base_url: next.ollama_base_url,
      default_generation_model: next.default_generation_model,
      default_embedding_model: next.default_embedding_model,
      default_model_references: next.default_model_references,
      request_timeout_seconds: next.request_timeout_seconds,
      max_context_chars: next.max_context_chars,
      max_chunk_chars: next.max_chunk_chars,
      max_map_items: next.max_map_items,
      max_repeat_iterations: next.max_repeat_iterations,
      max_concurrent_nodes: next.max_concurrent_nodes,
      ocr_llm_enhancement_enabled: next.ocr_llm_enhancement_enabled,
      ocr_llm_model: next.ocr_llm_model,
      ocr_llm_triage_model: next.ocr_llm_triage_model,
      agent_provider: next.agent_provider,
      openai_base_url: next.openai_base_url,
      agent_max_turns: next.agent_max_turns,
      agent_tracing_enabled: next.agent_tracing_enabled,
      python_node_enabled: next.python_node_enabled,
      python_node_timeout_seconds: next.python_node_timeout_seconds,
      python_node_memory_mb: next.python_node_memory_mb,
      python_node_allowed_imports: next.python_node_allowed_imports,
    });
  };

  const refreshModels = async () => {
    try {
      setModelError('');
      setModels(await api.listModels());
    } catch (err) {
      setModelError(toMessage(err, 'Could not discover Ollama models'));
    }
  };

  useEffect(() => {
    let active = true;
    Promise.allSettled([refresh(), refreshModels()])
      .then((results) => {
        if (!active) return;
        const settingsResult = results[0];
        if (settingsResult.status === 'rejected') {
          setError(toMessage(settingsResult.reason, 'Failed to load settings'));
        }
      })
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, []);

  const save = async () => {
    setBusy('save');
    setError('');
    try {
      setSettings(await api.updateSettings(form));
      toast.success('Settings saved', 'New defaults apply to the next agent run.');
    } catch (err) {
      const message = toMessage(err, 'Failed to save settings');
      setError(message);
      toast.failure('Could not save settings', message);
    } finally {
      setBusy('');
    }
  };

  const modelOptions = models.map((model) => (
    <option key={model.name} value={model.name}>
      {model.name}
    </option>
  ));

  const ocrEnabled = form.ocr_llm_enhancement_enabled ?? false;

  return (
    <div className="page-stack settings-grid">
      <section className="stack gap-lg">
        <section className="panel stack gap-md">
          <div className="panel-header">
            <div>
              <p className="eyebrow">Ollama discovery</p>
              <h3>Available models</h3>
            </div>
            <button type="button" className="button subtle icon-only sm" title="Refresh models" aria-label="Refresh models" onClick={refreshModels}>
              <Icon name="refresh" size={14} />
            </button>
          </div>
          {modelError ? <ErrorNotice title="Model discovery failed" message={modelError} /> : null}
          <div className="stack gap-sm scroll-area">
            {models.map((model) => (
              <article key={model.name} className="list-item static">
                <div className="list-item-main">
                  <strong className="truncate">{model.name}</strong>
                  <p className="truncate mono">{model.digest ? model.digest.slice(0, 20) : 'No digest reported'}</p>
                </div>
                <div className="stack align-end gap-xs">
                  <span className="tiny-tag">{formatBytes(model.size)}</span>
                  <span className="muted-text small">{formatDateTime(model.modified_at)}</span>
                </div>
              </article>
            ))}
            {models.length === 0 && !modelError ? (
              <EmptyState
                icon="database"
                title="No models found"
                description="Start Ollama and pull at least one generation model, for example: ollama pull llama3.1:8b"
              />
            ) : null}
          </div>
        </section>

        <section className="panel">
          <div className="panel-subheader">
            <h3>Local storage paths</h3>
          </div>
          {settings ? (
            <dl className="definition-grid">
              <div>
                <dt>Database</dt>
                <dd className="path">{settings.database_path}</dd>
              </div>
              <div>
                <dt>Data</dt>
                <dd className="path">{settings.data_dir}</dd>
              </div>
              <div>
                <dt>Documents</dt>
                <dd className="path">{settings.documents_dir}</dd>
              </div>
              <div>
                <dt>Artifacts</dt>
                <dd className="path">{settings.artifacts_dir}</dd>
              </div>
              <div>
                <dt>Workspace</dt>
                <dd className="path">{settings.workspace_dir}</dd>
              </div>
            </dl>
          ) : null}
        </section>
      </section>

      <section className="stack gap-lg">
        {error ? <ErrorNotice message={error} /> : null}

        {loading ? <p className="empty-state">Loading settings…</p> : null}
        {settings ? (
          <>
            <ProviderManagement
              defaults={form.default_model_references ?? {}}
              onDefaultsChange={(default_model_references) => setForm((prev) => ({ ...prev, default_model_references }))}
            />
            <section className="panel stack gap-md">
              <div className="panel-subheader">
                <div>
                  <h3>Legacy Ollama compatibility</h3>
                  <p className="muted-text small">These existing values continue to support saved workflows that use legacy model fields.</p>
                </div>
              </div>
              <div className="form-grid two-col">
                <div className="field-stack">
                  <label className="field-label" htmlFor="ollama-url">Ollama base URL</label>
                  <input id="ollama-url" className="input" value={form.ollama_base_url ?? ''} onChange={(event) => setForm((prev) => ({ ...prev, ollama_base_url: event.target.value }))} />
                  <p className="field-hint">Usually http://127.0.0.1:11434. It also updates the seeded Default Ollama profile.</p>
                </div>
                <div className="field-stack">
                  <label className="field-label" htmlFor="timeout">Request timeout (seconds)</label>
                  <input id="timeout" className="input" type="number" value={form.request_timeout_seconds ?? 0} onChange={(event) => setForm((prev) => ({ ...prev, request_timeout_seconds: Number(event.target.value) }))} />
                  <p className="field-hint">Raise this for large vision models.</p>
                </div>
                <div className="field-stack">
                  <label className="field-label" htmlFor="gen-model">Legacy generation model</label>
                  <select id="gen-model" className="input" value={form.default_generation_model ?? ''} onChange={(event) => setForm((prev) => ({ ...prev, default_generation_model: event.target.value || null }))}>
                    <option value="">Select model</option>
                    {modelOptions}
                  </select>
                </div>
                <div className="field-stack">
                  <label className="field-label" htmlFor="embed-model">Legacy embedding model</label>
                  <select id="embed-model" className="input" value={form.default_embedding_model ?? ''} onChange={(event) => setForm((prev) => ({ ...prev, default_embedding_model: event.target.value || null }))}>
                    <option value="">Select model</option>
                    {modelOptions}
                  </select>
                  <p className="field-hint">Used only when no named-profile default is selected.</p>
                </div>
              </div>
            </section>
          </>
        ) : null}

        {settings ? (
          <section className="panel stack gap-md">
            <div className="panel-subheader">
              <div>
                <h3>Agent execution</h3>
                <p className="muted-text small">
                  Provider connections and model defaults are managed above. These controls apply to every Agent node.
                </p>
              </div>
            </div>
            <div className="form-grid two-col">
              <div className="field-stack">
                <label className="field-label" htmlFor="agent-max-turns">
                  Max turns per agent
                </label>
                <input
                  id="agent-max-turns"
                  className="input"
                  type="number"
                  min={1}
                  max={100}
                  value={form.agent_max_turns ?? 8}
                  onChange={(event) => setForm((prev) => ({ ...prev, agent_max_turns: Number(event.target.value) }))}
                />
                <p className="field-hint">How many tool-calling rounds before an agent is stopped.</p>
              </div>
            </div>
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={form.agent_tracing_enabled ?? false}
                onChange={(event) => setForm((prev) => ({ ...prev, agent_tracing_enabled: event.target.checked }))}
              />
              <span>
                Send traces to OpenAI
                <span className="field-hint">
                  Off by default. Turning this on uploads agent traces even when the models are local.
                </span>
              </span>
            </label>
          </section>
        ) : null}

        {settings ? (
          <section className="panel stack gap-md">
            <div className="panel-subheader">
              <div>
                <h3>Python node sandbox</h3>
                <p className="muted-text small">
                  Python nodes run your code in a separate process with no network, an import allowlist, and CPU, memory
                  and time limits. That stops accidents and casual misuse — it is not a jail against someone who already
                  has access to this machine.
                </p>
              </div>
            </div>
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={form.python_node_enabled ?? true}
                onChange={(event) => setForm((prev) => ({ ...prev, python_node_enabled: event.target.checked }))}
              />
              <span>
                Allow Python nodes to run
                <span className="field-hint">Turn this off to block every Python node without editing agents.</span>
              </span>
            </label>
            <div className="form-grid two-col">
              <div className="field-stack">
                <label className="field-label" htmlFor="py-timeout">
                  Timeout (seconds)
                </label>
                <input
                  id="py-timeout"
                  className="input"
                  type="number"
                  min={1}
                  max={300}
                  value={form.python_node_timeout_seconds ?? 10}
                  onChange={(event) =>
                    setForm((prev) => ({ ...prev, python_node_timeout_seconds: Number(event.target.value) }))
                  }
                />
              </div>
              <div className="field-stack">
                <label className="field-label" htmlFor="py-memory">
                  Memory limit (MB)
                </label>
                <input
                  id="py-memory"
                  className="input"
                  type="number"
                  min={32}
                  max={8192}
                  value={form.python_node_memory_mb ?? 256}
                  onChange={(event) => setForm((prev) => ({ ...prev, python_node_memory_mb: Number(event.target.value) }))}
                />
              </div>
            </div>
            <div className="field-stack">
              <label className="field-label" htmlFor="py-imports">
                Allowed imports
              </label>
              <input
                id="py-imports"
                className="input mono"
                value={(form.python_node_allowed_imports ?? []).join(', ')}
                onChange={(event) =>
                  setForm((prev) => ({
                    ...prev,
                    python_node_allowed_imports: parseImportList(event.target.value),
                  }))
                }
              />
              <p className="field-hint">Comma separated. Anything not listed raises an ImportError inside the sandbox.</p>
            </div>
          </section>
        ) : null}

        {settings ? (
          <section className="panel stack gap-md">
            <div className="panel-subheader">
              <div>
                <h3>LLM-enhanced OCR</h3>
                <p className="muted-text small">
                  Every page is OCR&apos;d, then triaged. The Vision default in Model providers handles image understanding.
                </p>
              </div>
            </div>
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={ocrEnabled}
                onChange={(event) => setForm((prev) => ({ ...prev, ocr_llm_enhancement_enabled: event.target.checked }))}
              />
              <span>Enable OCR triage and rewriting</span>
            </label>
            <div className="form-grid two-col" style={ocrEnabled ? undefined : { opacity: 0.5, pointerEvents: 'none' }}>
              <div className="field-stack">
                <label className="field-label" htmlFor="ocr-triage">
                  Ollama triage override
                </label>
                <select
                  id="ocr-triage"
                  className="input"
                  value={form.ocr_llm_triage_model ?? ''}
                  onChange={(event) => setForm((prev) => ({ ...prev, ocr_llm_triage_model: event.target.value || null }))}
                >
                  <option value="">Use the enhancement model</option>
                  {modelOptions}
                </select>
                <p className="field-hint">Optional local model name. Cloud profiles reuse the selected Vision model.</p>
              </div>
              <div className="field-stack">
                <label className="field-label" htmlFor="ocr-model">
                  Legacy Ollama override
                </label>
                <select
                  id="ocr-model"
                  className="input"
                  value={form.ocr_llm_model ?? ''}
                  onChange={(event) => setForm((prev) => ({ ...prev, ocr_llm_model: event.target.value || null }))}
                >
                  <option value="">Use the Vision default above</option>
                  {modelOptions}
                </select>
                <p className="field-hint">Used only when no provider-aware Vision default is configured.</p>
              </div>
            </div>
          </section>
        ) : null}

        {settings ? (
          <section className="panel stack gap-md">
            <div className="panel-subheader">
              <div>
                <h3>Execution limits</h3>
                <p className="muted-text small">Validated before every run so an agent can never exhaust your machine.</p>
              </div>
            </div>
            <div className="form-grid two-col">
              {limitFields.map(({ key, label, hint }) => (
                <div className="field-stack" key={key}>
                  <label className="field-label" htmlFor={key}>
                    {label}
                  </label>
                  <input
                    id={key}
                    className="input"
                    type="number"
                    value={String(form[key] ?? '')}
                    onChange={(event) => setForm((prev) => ({ ...prev, [key]: Number(event.target.value) }))}
                  />
                  <p className="field-hint">{hint}</p>
                </div>
              ))}
            </div>
          </section>
        ) : null}

        <div className="button-row">
          <button type="button" className="button primary" disabled={busy === 'save' || !settings} onClick={save}>
            <Icon name="save" size={14} />
            {busy === 'save' ? 'Saving…' : 'Save settings'}
          </button>
          <button type="button" className="button subtle" disabled={!settings} onClick={refresh}>
            <Icon name="refresh" size={14} />
            Discard changes
          </button>
        </div>
      </section>
    </div>
  );
}
