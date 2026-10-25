import { useEffect, useMemo, useState } from 'react';
import { Icon, type IconName } from '../../shared/components/Icons';
import { ErrorNotice, Loading, PageHeader } from '../../shared/components/Ui';
import { ModelDefaultsPanel } from './ModelDefaultsPanel';
import { ProviderProfilesPanel } from './ProviderProfilesPanel';
import { DocumentsPanel, RuntimePanel } from './SettingsSections';
import { providersApi, type Provider, type Settings } from './api';
import './providers.css';

type SectionKey = 'models' | 'providers' | 'documents' | 'runtime';

const SECTIONS: { key: SectionKey; label: string; icon: IconName; hint: string }[] = [
  { key: 'models', label: 'Models', icon: 'sparkle', hint: 'Defaults per capability' },
  { key: 'providers', label: 'Providers', icon: 'tools', hint: 'Profiles and catalogues' },
  { key: 'documents', label: 'Documents', icon: 'papers', hint: 'OCR and ingestion' },
  { key: 'runtime', label: 'Runtime', icon: 'sliders', hint: 'Context and storage' },
];

export default function SettingsPage() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [saved, setSaved] = useState<Settings | null>(null);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [section, setSection] = useState<SectionKey>('models');
  const [error, setError] = useState<unknown>(null);
  const [settingsError, setSettingsError] = useState<unknown>(null);
  const [providersError, setProvidersError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [providersLoading, setProvidersLoading] = useState(true);
  const [discovering, setDiscovering] = useState(false);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<number | null>(null);

  const loadSettings = async () => {
    setLoading(true);
    setSettingsError(null);
    try {
      const nextSettings = await providersApi.settings();
      setSettings(nextSettings);
      setSaved(nextSettings);
    } catch (nextError) {
      setSettingsError(nextError);
    } finally {
      setLoading(false);
    }
  };

  const loadProviders = async (discoverModels = false) => {
    setProvidersLoading(true);
    setProvidersError(null);
    let nextProviders: Provider[];
    try {
      nextProviders = await providersApi.list(true);
      setProviders(nextProviders);
    } catch (nextError) {
      setProvidersError(nextError);
      throw nextError;
    } finally {
      setProvidersLoading(false);
    }
    if (!discoverModels) return;
    setDiscovering(true);
    try {
      const results = await Promise.all(nextProviders.map(async (provider) => {
        if (provider.kind !== 'ollama' || provider.state !== 'active') return null;
        try {
          const result = await providersApi.discover(provider.id);
          if (result.discovery_error) throw new Error(result.discovery_error);
          return { id: provider.id, models: result.models };
        } catch (nextError) {
          setProvidersError(nextError);
          return null;
        }
      }));
      setProviders((current) => current.map((provider) => {
        const result = results.find((item) => item?.id === provider.id);
        const original = nextProviders.find((item) => item.id === provider.id);
        return result && provider === original ? { ...provider, models: result.models } : provider;
      }));
    } finally {
      setDiscovering(false);
    }
  };

  useEffect(() => {
    void loadSettings();
    void loadProviders(true).catch(() => {});
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

  if (loading || !settings) {
    return (
      <div className="page settings-page">
        <PageHeader title="Settings" description="Models, providers, and document processing." />
        {settingsError ? (
          <>
            <ErrorNotice error={settingsError} />
            <button className="button secondary" type="button" onClick={() => void loadSettings()}>
              Retry settings
            </button>
          </>
        ) : <Loading label="Loading settings…" />}
      </div>
    );
  }

  const saveSettings = async () => {
    setSaving(true);
    setError(null);
    try {
      const next = await providersApi.updateSettings({
        default_model_references: settings.default_model_references,
        decision_parallel_requests: settings.decision_parallel_requests,
        agent_context_window_tokens: settings.agent_context_window_tokens,
        agent_context_response_reserve_tokens: settings.agent_context_response_reserve_tokens,
        agent_context_model_summary_enabled: settings.agent_context_model_summary_enabled,
        agent_context_high_water_ratio: settings.agent_context_high_water_ratio,
        agent_epoch_max_turns: settings.agent_epoch_max_turns,
        tool_read_retry_attempts: settings.tool_read_retry_attempts,
        retrieval_max_context_chars: settings.retrieval_max_context_chars,
        ocr_engine: settings.ocr_engine,
        ocr_surya_base_url: settings.ocr_surya_base_url,
        ocr_surya_model: settings.ocr_surya_model,
        ocr_surya_parallel: settings.ocr_surya_parallel,
        ocr_surya_max_tokens: settings.ocr_surya_max_tokens,
        ocr_indra_fallback_enabled: settings.ocr_indra_fallback_enabled,
        ocr_llm_enhancement_enabled: settings.ocr_llm_enhancement_enabled,
        ocr_llm_model: settings.ocr_llm_model,
        ocr_llm_triage_model: settings.ocr_llm_triage_model,
      });
      setSettings((current) => current === settings ? next : current);
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
      {providersError ? (
        <div className="stack">
          <ErrorNotice error={providersError} />
          <p>Saved settings are available. Retry provider loading and discovery, or manage models manually.</p>
          <div>
            <button
              className="button secondary"
              type="button"
              disabled={providersLoading || discovering}
              onClick={() => void loadProviders(true).catch(() => {})}
            >
              Retry providers and discovery
            </button>
          </div>
        </div>
      ) : null}
      {providersLoading ? <p role="status">Loading provider profiles…</p> : null}
      {discovering ? <p role="status">Discovering Ollama models… Saved settings remain editable.</p> : null}

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
                <strong title={item.hint}>{item.label}</strong>
              </span>
            </button>
          ))}
        </nav>

        <div className="settings-sections">
          {section === 'models' ? (
            <ModelDefaultsPanel settings={settings} providers={providers.filter((provider) => provider.state === 'active')} onChange={setSettings} />
          ) : null}
          {section === 'providers' ? (
            <ProviderProfilesPanel
              providers={providers}
              defaultReferences={[saved?.default_model_references ?? {}, settings.default_model_references]}
              onRefresh={() => loadProviders()}
              onError={setError}
            />
          ) : null}
          {section === 'documents' ? (
            <DocumentsPanel settings={settings} onChange={setSettings} />
          ) : null}
          {section === 'runtime' ? (
            <RuntimePanel settings={settings} onChange={setSettings} />
          ) : null}
        </div>
      </div>

      {dirty || saving || savedAt ? (
        <div className={`settings-savebar${dirty ? ' dirty' : ''}`}>
          <span className="settings-savebar-status">
            {dirty ? (
              <>
                <span className="dot" aria-hidden="true" />
                Unsaved changes
              </>
            ) : (
              <>
                <Icon name="check" size={14} />
                Saved
              </>
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
