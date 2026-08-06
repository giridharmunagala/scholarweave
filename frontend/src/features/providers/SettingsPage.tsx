import { useEffect, useState } from 'react';
import { ErrorNotice, Loading, PageHeader } from '../../shared/components/Ui';
import { ModelDefaultsPanel } from './ModelDefaultsPanel';
import { ProviderProfilesPanel } from './ProviderProfilesPanel';
import { providersApi, type Provider, type Settings } from './api';
import './providers.css';

export default function SettingsPage() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

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
    setProviders(discoveredProviders);
  };
  useEffect(() => {
    load().catch(setError).finally(() => setLoading(false));
  }, []);

  if (loading || !settings) return <Loading label="Loading settings…" />;

  const saveSettings = async () => {
    setSaving(true);
    setError(null);
    try {
      setSettings(
        await providersApi.updateSettings({
          default_model_references: settings.default_model_references,
          agent_tracing_enabled: settings.agent_tracing_enabled,
          agent_compaction_threshold_items: settings.agent_compaction_threshold_items,
          agent_compaction_recent_items: settings.agent_compaction_recent_items,
          python_tool_enabled: settings.python_tool_enabled,
          python_tool_timeout_seconds: settings.python_tool_timeout_seconds,
          python_tool_memory_mb: settings.python_tool_memory_mb,
          retrieval_max_context_chars: settings.retrieval_max_context_chars,
          ocr_engine: settings.ocr_engine,
          surya_model: settings.surya_model,
          surya_device: settings.surya_device,
          surya_max_new_tokens: settings.surya_max_new_tokens,
          surya_max_image_width: settings.surya_max_image_width,
          surya_timeout_seconds: settings.surya_timeout_seconds,
          surya_unload_ollama_models: settings.surya_unload_ollama_models,
          ocr_llm_enhancement_enabled: settings.ocr_llm_enhancement_enabled,
          ocr_llm_model: settings.ocr_llm_model,
          ocr_llm_triage_model: settings.ocr_llm_triage_model,
        }),
      );
    } catch (nextError) {
      setError(nextError);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="page">
      <PageHeader
        eyebrow="Runtime configuration"
        title="Settings"
        description="Provider profiles resolve to SDK Responses or Chat Completions models at compile time."
      />
      {error ? <ErrorNotice error={error} /> : null}
      <ModelDefaultsPanel
        settings={settings}
        providers={providers}
        onChange={setSettings}
        onSave={() => void saveSettings()}
        saving={saving}
      />
      <ProviderProfilesPanel
        providers={providers}
        onRefresh={load}
        onError={setError}
      />
    </div>
  );
}
