import { useEffect, useMemo, useState } from 'react';
import { Icon, type IconName } from '../../shared/components/Icons';
import { ErrorNotice, Loading, PageHeader } from '../../shared/components/Ui';
import { AppearancePanel } from './AppearancePanel';
import { ModelDefaultsPanel } from './ModelDefaultsPanel';
import { ProviderProfilesPanel } from './ProviderProfilesPanel';
import { DocumentsPanel, RuntimePanel } from './SettingsSections';
import { providersApi, type Provider, type Settings } from './api';
import './providers.css';

type SectionKey = 'models' | 'providers' | 'documents' | 'runtime' | 'appearance';

const SECTIONS: { key: SectionKey; label: string; icon: IconName; hint: string }[] = [
  { key: 'models', label: 'Models', icon: 'sparkle', hint: 'Defaults per capability' },
  { key: 'providers', label: 'Providers', icon: 'tools', hint: 'Profiles and catalogues' },
  { key: 'documents', label: 'Documents', icon: 'papers', hint: 'OCR and ingestion' },
  { key: 'runtime', label: 'Runtime', icon: 'sliders', hint: 'Limits and storage' },
  { key: 'appearance', label: 'Appearance', icon: 'palette', hint: 'Theme wallpaper' },
];

export default function SettingsPage() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [saved, setSaved] = useState<Settings | null>(null);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [section, setSection] = useState<SectionKey>('models');
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<number | null>(null);

  const load = async () => {
    const [nextSettings, nextProviders] = await Promise.all([
      providersApi.settings(),
      providersApi.list(),
    ]);
    const discoveredProviders = await Promise.all(
      nextProviders.map(async (provider) => {
        if (provider.kind !== 'ollama') return provider;
        const result = await providersApi.discover(provider.id);
        if (result.discovery_error) {
          setError(new Error(result.discovery_error));
          return provider;
        }
        return { ...provider, models: result.models };
      }),
    );
    setSettings(nextSettings);
    setSaved(nextSettings);
    setProviders(discoveredProviders);
  };

  useEffect(() => {
    load().catch(setError).finally(() => setLoading(false));
  }, []);

  const dirty = useMemo(
    () => Boolean(settings && saved && JSON.stringify(settings) !== JSON.stringify(saved)),
    [settings, saved],
  );

  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty]);

  if (loading || !settings) return <Loading label="Loading settings…" />;

  const saveSettings = async () => {
    setSaving(true);
    setError(null);
    try {
      const next = await providersApi.updateSettings({
        default_model_references: settings.default_model_references,
        agent_tracing_enabled: settings.agent_tracing_enabled,
        agent_context_window_tokens: settings.agent_context_window_tokens,
        agent_context_high_water_ratio: settings.agent_context_high_water_ratio,
        agent_context_compaction_target_tokens: settings.agent_context_compaction_target_tokens,
        tool_result_max_tokens: settings.tool_result_max_tokens,
        agent_epoch_max_turns: settings.agent_epoch_max_turns,
        agent_max_epochs: settings.agent_max_epochs,
        agent_run_timeout_seconds: settings.agent_run_timeout_seconds,
        tool_call_timeout_seconds: settings.tool_call_timeout_seconds,
        tool_read_retry_attempts: settings.tool_read_retry_attempts,
        python_tool_enabled: settings.python_tool_enabled,
        python_tool_timeout_seconds: settings.python_tool_timeout_seconds,
        python_tool_memory_mb: settings.python_tool_memory_mb,
        retrieval_max_context_chars: settings.retrieval_max_context_chars,
        ocr_llm_enhancement_enabled: settings.ocr_llm_enhancement_enabled,
        ocr_llm_model: settings.ocr_llm_model,
        ocr_llm_triage_model: settings.ocr_llm_triage_model,
      });
      setSettings(next);
      setSaved(next);
      setSavedAt(Date.now());
    } catch (nextError) {
      setError(nextError);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="page settings-page">
      <PageHeader
        title="Settings"
        description="Models, providers, and how documents and agent runs are processed."
      />
      {error ? <ErrorNotice error={error} /> : null}

      <div className="settings-layout">
        <nav className="settings-nav" aria-label="Settings sections">
          {SECTIONS.map((item) => (
            <button
              type="button"
              key={item.key}
              className={`settings-nav-item${section === item.key ? ' active' : ''}`}
              aria-current={section === item.key}
              onClick={() => setSection(item.key)}
            >
              <Icon name={item.icon} size={16} />
              <span>
                <strong>{item.label}</strong>
                <small>{item.hint}</small>
              </span>
            </button>
          ))}
        </nav>

        <div className="settings-sections">
          {section === 'models' ? (
            <ModelDefaultsPanel settings={settings} providers={providers} onChange={setSettings} />
          ) : null}
          {section === 'providers' ? (
            <ProviderProfilesPanel providers={providers} onRefresh={load} onError={setError} />
          ) : null}
          {section === 'documents' ? (
            <DocumentsPanel settings={settings} onChange={setSettings} />
          ) : null}
          {section === 'runtime' ? (
            <RuntimePanel settings={settings} onChange={setSettings} />
          ) : null}
          {section === 'appearance' ? <AppearancePanel /> : null}
        </div>
      </div>

      {section !== 'providers' ? (
        <div className={`settings-savebar${dirty ? ' dirty' : ''}`}>
          <span className="settings-savebar-status">
            {dirty ? (
              <>
                <span className="dot" aria-hidden="true" />
                Unsaved changes
              </>
            ) : savedAt ? (
              <>
                <Icon name="check" size={14} />
                Saved
              </>
            ) : (
              'All changes are stored locally in your workspace database.'
            )}
          </span>
          <button
            className="button secondary"
            type="button"
            disabled={!dirty || saving}
            onClick={() => setSettings(saved)}
          >
            Discard
          </button>
          <button
            className="button"
            type="button"
            disabled={!dirty || saving}
            onClick={() => void saveSettings()}
          >
            <Icon name="save" size={15} />
            {saving ? 'Saving…' : 'Save changes'}
          </button>
        </div>
      ) : null}
    </div>
  );
}
