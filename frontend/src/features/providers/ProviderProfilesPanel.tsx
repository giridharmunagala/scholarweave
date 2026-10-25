import { useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Icon } from '../../shared/components/Icons';
import { Panel, StatusPill } from '../../shared/components/Ui';
import {
  REASONING_EFFORTS,
  type ReasoningEffort,
} from '../chat/ReasoningEffortSelect';
import {
  providersApi,
  type Provider,
  type ProviderCreate,
  type ProviderUpdate,
  type ProviderVerification,
  type Settings,
} from './api';

const emptyProvider: ProviderCreate = {
  name: 'Local Ollama',
  kind: 'ollama',
  base_url: 'http://127.0.0.1:11434',
  api_key: null,
  api_mode: 'chat_completions',
  models: [],
};
const modelCapabilities = ['chat', 'tools', 'embedding', 'vision', 'decision'] as const;
type ModelCapability = (typeof modelCapabilities)[number];
const modelSwitchHelp =
  'One LLM call at a time across all providers, including chat, summaries, and context maintenance. Queued chat calls get priority between responses, with regular turns for background work. Your model server manages hot swaps; no manual confirmation is needed.';

function ModelContextField({
  model,
  disabled,
  onSave,
}: {
  model: Provider['models'][number];
  disabled: boolean;
  onSave: (value: number | null) => Promise<boolean>;
}) {
  const descriptionId = useId();
  const savedValue = String(model.context_window_tokens ?? '');
  const [value, setValue] = useState(savedValue);
  const [saveError, setSaveError] = useState(false);
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    setValue(savedValue);
    setSaveError(false);
  }, [savedValue]);
  const minimum = model.capabilities?.includes('decision')
    && !model.capabilities.some((capability) => ['chat', 'tools', 'vision'].includes(capability))
    ? 128 : 4096;
  const parsed = value.trim() ? Number(value) : null;
  const valid = parsed === null
    || (Number.isInteger(parsed) && parsed >= minimum && parsed <= 2000000);
  const dirty = value !== savedValue;
  const save = async () => {
    if (!valid || !dirty || disabled || saving) return;
    setSaving(true);
    setSaveError(false);
    const succeeded = await onSave(parsed);
    if (succeeded) setValue(String(parsed ?? ''));
    setSaveError(!succeeded);
    setSaving(false);
  };
  return (
    <div className="provider-model-context-window">
      <label className="field">
        Context (tokens)
        <input
          type="text"
          inputMode="numeric"
          value={value}
          placeholder="fallback"
          disabled={disabled || saving}
          aria-label={`${model.name} context window tokens`}
          aria-invalid={!valid}
          aria-describedby={descriptionId}
          onChange={(event) => {
            setValue(event.target.value);
            setSaveError(false);
          }}
          onBlur={() => void save()}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault();
              void save();
            }
          }}
        />
      </label>
      <small id={descriptionId} role={!valid || saveError ? 'alert' : 'status'}>
        {!valid ? `Enter a whole number from ${minimum} to 2000000, or leave blank for fallback.`
          : saving ? 'Saving context…'
            : saveError ? 'Context save failed or could not be confirmed. Retry or revert.'
              : dirty ? 'Unsaved context. Leave the field or press Enter to save.'
                : 'Saved context'}
      </small>
      {dirty ? (
        <button
          className="button secondary small"
          type="button"
          disabled={disabled || saving}
          onMouseDown={(event) => event.preventDefault()}
          onClick={() => {
            setValue(savedValue);
            setSaveError(false);
          }}
        >
          Revert context
        </button>
      ) : null}
    </div>
  );
}

export function ProviderProfilesPanel({
  providers,
  defaultReferences = [],
  onRefresh,
  onError,
}: {
  providers: Provider[];
  defaultReferences?: Settings['default_model_references'][];
  onRefresh: () => Promise<void>;
  onError: (error: unknown) => void;
}) {
  const [draft, setDraft] = useState<ProviderCreate>(emptyProvider);
  const [showCreate, setShowCreate] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [archiveProviderId, setArchiveProviderId] = useState<string | null>(null);
  const [endpointDraft, setEndpointDraft] = useState<{ providerId: string; baseUrl: string } | null>(null);
  const [verification, setVerification] = useState<
    Record<string, ProviderVerification>
  >({});
  const [verificationErrors, setVerificationErrors] = useState<Record<string, string>>({});
  const [manualName, setManualName] = useState('');
  const [manualCapabilities, setManualCapabilities] = useState<ModelCapability[]>(['chat']);
  const [manualError, setManualError] = useState<string | null>(null);
  const [modelQueries, setModelQueries] = useState<Record<string, string>>({});
  const [catalogProviderId, setCatalogProviderId] = useState<string | null>(null);
  const [modelFilter, setModelFilter] = useState<'all' | 'enabled' | 'disabled'>('all');
  const catalogSearchRef = useRef<HTMLInputElement>(null);
  const catalogProvider = providers.find((provider) => provider.id === catalogProviderId);
  const catalogQuery = catalogProvider ? modelQueries[catalogProvider.id] ?? '' : '';
  const normalizedCatalogQuery = catalogQuery.trim().toLocaleLowerCase();
  const visibleCatalogModels = catalogProvider
    ? catalogProvider.models.filter((model) => {
        const matchesQuery =
          !normalizedCatalogQuery
          || `${model.name} ${model.capabilities?.join(' ') ?? ''} ${model.reasoning_efforts?.join(' ') ?? ''}`
            .toLocaleLowerCase()
            .includes(normalizedCatalogQuery);
        const matchesState =
          modelFilter === 'all'
          || (modelFilter === 'enabled' ? model.enabled : !model.enabled);
        return matchesQuery && matchesState;
      })
    : [];
  const enabledModelCount = catalogProvider?.models.filter((model) => model.enabled).length ?? 0;
  const modelUpdateBusy = busy !== null;
  const activeProviders = providers.filter((provider) => provider.state !== 'archived');
  const fallbackProvider = activeProviders.find((provider) => provider.name === 'Default Ollama')
    ?? activeProviders[0];

  useEffect(() => {
    if (!catalogProviderId) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    catalogSearchRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setCatalogProviderId(null);
    };
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [catalogProviderId]);

  const create = async () => {
    setBusy('create');
    try {
      await providersApi.create(draft.kind === 'ollama' ? { ...draft, api_mode: 'chat_completions' } : draft);
      setDraft(emptyProvider);
      setShowCreate(false);
      await onRefresh();
    } catch (error) {
      onError(error);
    } finally {
      setBusy(null);
    }
  };
  const discover = async (provider: Provider) => {
    setBusy(`discover:${provider.id}`);
    try {
      const result = await providersApi.discover(provider.id);
      if (result.discovery_error) {
        throw new Error(result.discovery_error);
      }
      await onRefresh();
      setCatalogProviderId(provider.id);
    } catch (error) {
      onError(error);
    } finally {
      setBusy(null);
    }
  };
  const verify = async (provider: Provider, model: string) => {
    const key = `${provider.id}:${model}`;
    setBusy(`verify:${key}`);
    setVerification((current) => Object.fromEntries(
      Object.entries(current).filter(([entry]) => entry !== key),
    ));
    setVerificationErrors((current) => Object.fromEntries(
      Object.entries(current).filter(([entry]) => entry !== key),
    ));
    try {
      const result = await providersApi.verify(provider.id, model);
      setVerification((current) => ({ ...current, [key]: result }));
    } catch (error) {
      setVerificationErrors((current) => ({
        ...current,
        [key]: error instanceof Error ? error.message : String(error),
      }));
      onError(error);
    } finally {
      setBusy(null);
    }
  };
  const archive = async (provider: Provider) => {
    setBusy(`archive:${provider.id}`);
    try {
      await providersApi.archive(provider.id);
      await onRefresh();
      setArchiveProviderId(null);
    } catch (error) {
      onError(error);
    } finally {
      setBusy(null);
    }
  };
  const restore = async (provider: Provider) => {
    setBusy(`restore:${provider.id}`);
    try {
      await providersApi.restore(provider.id);
      await onRefresh();
    } catch (error) {
      onError(error);
    } finally {
      setBusy(null);
    }
  };
  const registerModel = async (provider: Provider) => {
    const name = manualName.trim();
    if (!name || name.length > 255) {
      setManualError('Enter a model or deployment name from 1 to 255 characters.');
      return;
    }
    if (provider.models.some((model) => model.name === name)) {
      setManualError('This model is already configured. Edit it in the catalogue below.');
      return;
    }
    if (!manualCapabilities.length) {
      setManualError('Select at least one capability for this model.');
      return;
    }
    setBusy(`register:${provider.id}`);
    setManualError(null);
    try {
      await providersApi.update(provider.id, {
        serialize_model_switches: provider.serialize_model_switches,
        models: [
          ...provider.models,
          { name, capabilities: manualCapabilities, enabled: true, preserve_thinking: false },
        ],
      });
      await onRefresh();
      setManualName('');
      setModelQueries((current) => ({ ...current, [provider.id]: '' }));
      setModelFilter('all');
    } catch (error) {
      setManualError(error instanceof Error ? error.message : String(error));
      onError(error);
    } finally {
      setBusy(null);
    }
  };
  const setModelEnabled = async (provider: Provider, modelName: string, enabled: boolean) => {
    const key = `${provider.id}:${modelName}`;
    setBusy(`enable:${key}`);
    try {
      await providersApi.update(provider.id, {
        serialize_model_switches: provider.serialize_model_switches,
        models: provider.models.map((model) =>
          model.name === modelName ? { ...model, enabled } : model,
        ),
      });
      await onRefresh();
    } catch (error) {
      onError(error);
    } finally {
      setBusy(null);
    }
  };
  const setVisibleModelsEnabled = async (provider: Provider, enabled: boolean) => {
    const visibleNames = new Set(visibleCatalogModels.map((model) => model.name));
    if (!visibleNames.size) return;
    setBusy(`enable-visible:${provider.id}`);
    try {
      await providersApi.update(provider.id, {
        serialize_model_switches: provider.serialize_model_switches,
        models: provider.models.map((model) =>
          visibleNames.has(model.name) ? { ...model, enabled } : model,
        ),
      });
      await onRefresh();
    } catch (error) {
      onError(error);
    } finally {
      setBusy(null);
    }
  };
  const setModelCapability = async (
    provider: Provider,
    modelName: string,
    capability: ModelCapability,
    enabled: boolean,
  ) => {
    const key = `${provider.id}:${modelName}`;
    setBusy(`configure:${key}`);
    try {
      await providersApi.update(provider.id, {
        serialize_model_switches: provider.serialize_model_switches,
        models: provider.models.map((model) => {
          if (model.name !== modelName) return model;
          const capabilities = new Set(model.capabilities ?? []);
          if (enabled) capabilities.add(capability);
          else capabilities.delete(capability);
          return { ...model, capabilities: [...capabilities] };
        }),
      });
      clearVerification(provider.id);
      await onRefresh();
    } catch (error) {
      onError(error);
    } finally {
      setBusy(null);
    }
  };
  const setModelReasoningEffort = async (
    provider: Provider,
    modelName: string,
    effort: ReasoningEffort,
    enabled: boolean,
  ) => {
    const key = `${provider.id}:${modelName}`;
    setBusy(`configure:${key}:reasoning`);
    try {
      await providersApi.update(provider.id, {
        serialize_model_switches: provider.serialize_model_switches,
        models: provider.models.map((model) => {
          if (model.name !== modelName) return model;
          const reasoningEfforts = new Set(model.reasoning_efforts ?? []);
          if (enabled) reasoningEfforts.add(effort);
          else reasoningEfforts.delete(effort);
          return {
            ...model,
            reasoning_efforts: REASONING_EFFORTS.filter((item) =>
              reasoningEfforts.has(item)
            ),
          };
        }),
      });
      clearVerification(provider.id);
      await onRefresh();
    } catch (error) {
      onError(error);
    } finally {
      setBusy(null);
    }
  };
  const setModelContextWindow = async (
    provider: Provider,
    modelName: string,
    contextWindowTokens: number | null,
  ) => {
    const key = `${provider.id}:${modelName}`;
    setBusy(`configure:${key}:context`);
    try {
      await providersApi.update(provider.id, {
        serialize_model_switches: provider.serialize_model_switches,
        models: provider.models.map((model) =>
          model.name === modelName
            ? { ...model, context_window_tokens: contextWindowTokens }
            : model
        ),
      });
      await onRefresh();
      return true;
    } catch (error) {
      onError(error);
      return false;
    } finally {
      setBusy(null);
    }
  };
  const setModelPreserveThinking = async (
    provider: Provider,
    modelName: string,
    preserveThinking: boolean,
  ) => {
    const key = `${provider.id}:${modelName}`;
    setBusy(`configure:${key}:preserve-thinking`);
    try {
      await providersApi.update(provider.id, {
        serialize_model_switches: provider.serialize_model_switches,
        models: provider.models.map((model) =>
          model.name === modelName
            ? { ...model, preserve_thinking: preserveThinking }
            : model
        ),
      });
      await onRefresh();
    } catch (error) {
      onError(error);
    } finally {
      setBusy(null);
    }
  };
  const openCatalog = (providerId: string) => {
    setModelFilter('all');
    setManualName('');
    setManualCapabilities(['chat']);
    setManualError(null);
    setCatalogProviderId(providerId);
  };
  const clearVerification = (providerId: string) => {
    setVerification((current) => Object.fromEntries(
      Object.entries(current).filter(([key]) => !key.startsWith(`${providerId}:`)),
    ));
    setVerificationErrors((current) => Object.fromEntries(
      Object.entries(current).filter(([key]) => !key.startsWith(`${providerId}:`)),
    ));
  };
  const setReasoningOverride = async (
    provider: Provider,
    key: 'default_reasoning_effort' | 'tool_reasoning_effort',
    value: string,
  ) => {
    const effort = REASONING_EFFORTS.find((item) => item === value) ?? null;
    const payload: ProviderUpdate = {
      serialize_model_switches: provider.serialize_model_switches,
      [key]: effort,
    };
    setBusy(`reasoning:${provider.id}`);
    try {
      await providersApi.update(provider.id, payload);
      clearVerification(provider.id);
      await onRefresh();
    } catch (error) {
      onError(error);
    } finally {
      setBusy(null);
    }
  };

  const setApiMode = async (provider: Provider, value: string) => {
    if (value !== 'chat_completions' && value !== 'responses') return;
    setBusy(`protocol:${provider.id}`);
    try {
      await providersApi.update(provider.id, {
        api_mode: value,
        serialize_model_switches: provider.serialize_model_switches,
      });
      clearVerification(provider.id);
      await onRefresh();
    } catch (error) {
      onError(error);
    } finally {
      setBusy(null);
    }
  };

  const saveEndpoint = async (provider: Provider) => {
    if (!endpointDraft || endpointDraft.providerId !== provider.id) return;
    setBusy(`endpoint:${provider.id}`);
    try {
      await providersApi.update(provider.id, {
        base_url: endpointDraft.baseUrl.trim(),
        serialize_model_switches: provider.serialize_model_switches,
      });
      clearVerification(provider.id);
      await onRefresh();
      setEndpointDraft(null);
    } catch (error) {
      onError(error);
    } finally {
      setBusy(null);
    }
  };

  return (
    <Panel
      title="Provider profiles"
      description="Choose Chat Completions for compatible servers, or Responses for providers that support it."
      actions={
        <button className="button secondary" type="button" onClick={() => setShowCreate((value) => !value)}>
          <Icon name={showCreate ? 'close' : 'plus'} />
          {showCreate ? 'Cancel' : 'Add provider'}
        </button>
      }
    >
      {showCreate ? (
        <div className="stack provider-form">
          <div className="field-row">
            <label className="field">Name<input value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} /></label>
            <label className="field">
              Kind
              <select value={draft.kind} onChange={(event) => setDraft({ ...draft, kind: event.target.value as ProviderCreate['kind'] })}>
                <option value="ollama">Ollama</option>
                <option value="openai">OpenAI</option>
                <option value="azure_openai">Azure OpenAI</option>
                <option value="azure_foundry">Azure Foundry</option>
                <option value="openai_compatible">OpenAI compatible</option>
              </select>
            </label>
          </div>
          <label className="field">Base URL<input value={draft.base_url} onChange={(event) => setDraft({ ...draft, base_url: event.target.value })} /></label>
          <label className="field">
            Generation API
            <select
              value={draft.kind === 'ollama' ? 'chat_completions' : draft.api_mode ?? 'chat_completions'}
              disabled={draft.kind === 'ollama'}
              onChange={(event) => {
                const value = event.target.value;
                if (value === 'chat_completions' || value === 'responses') setDraft({ ...draft, api_mode: value });
              }}
            >
              <option value="chat_completions">Chat Completions</option>
              <option value="responses">Responses (provider must support it)</option>
            </select>
          </label>
          {draft.kind !== 'ollama' ? (
            <label className="field">
              {draft.kind === 'openai_compatible' ? 'API key (optional)' : 'API key'}
              <input type="password" value={draft.api_key ?? ''} onChange={(event) => setDraft({ ...draft, api_key: event.target.value || null })} />
            </label>
          ) : null}
          <div><button className="button" type="button" disabled={busy === 'create'} onClick={() => void create()}>{busy === 'create' ? 'Creating…' : 'Create profile'}</button></div>
        </div>
      ) : null}
      {/* Scheduling is a property of the runtime, not of any one profile, so it
          is stated once here instead of repeated on every provider card. */}
      <p className="notice info provider-lane-note">{modelSwitchHelp}</p>
      <div className="card-grid">
        {activeProviders.map((provider) => {
          const referencedRoles = [...new Set(defaultReferences.flatMap((references) =>
            Object.entries(references)
              .filter(([, reference]) => reference.provider_profile_id === provider.id
                || (!reference.provider_profile_id && reference.model && fallbackProvider?.id === provider.id))
              .map(([role]) => role),
          ))];
          return (
            <article className="card stack" key={provider.id}>
              <div className="toolbar"><span className="eyebrow">{provider.kind}</span><StatusPill value={provider.state} /></div>
              <div><h2>{provider.name}</h2><code>{provider.base_url}</code></div>
              {endpointDraft?.providerId === provider.id ? (
                <form className="stack" onSubmit={(event) => {
                  event.preventDefault();
                  void saveEndpoint(provider);
                }}>
                  <label className="field">
                    Base URL
                    <input
                      aria-label={`Base URL for ${provider.name}`}
                      type="url"
                      required
                      maxLength={1024}
                      value={endpointDraft.baseUrl}
                      disabled={busy !== null}
                      onChange={(event) => setEndpointDraft({ providerId: provider.id, baseUrl: event.target.value })}
                    />
                  </label>
                  <p>Change the host or port without losing saved models, API keys, or settings. Keep the API path, such as /v1, if your server requires it. Verify your models after saving.</p>
                  <div className="button-row">
                    <button className="button small" type="submit" disabled={busy !== null || !endpointDraft.baseUrl.trim()}>
                      {busy === `endpoint:${provider.id}` ? 'Saving...' : 'Save URL'}
                    </button>
                    <button className="button secondary small" type="button" disabled={busy !== null} onClick={() => setEndpointDraft(null)}>Cancel</button>
                  </div>
                </form>
              ) : (
                <div>
                  <button className="button secondary small" type="button" disabled={busy !== null} onClick={() => setEndpointDraft({ providerId: provider.id, baseUrl: provider.base_url })}>Edit URL</button>
                </div>
              )}
              <label className="field">
                Generation API
                <select
                  aria-label={`Generation API for ${provider.name}`}
                  value={provider.api_mode ?? 'chat_completions'}
                  disabled={busy !== null || provider.kind === 'ollama'}
                  onChange={(event) => void setApiMode(provider, event.target.value)}
                >
                  <option value="chat_completions">Chat Completions</option>
                  <option value="responses">Responses (provider must support it)</option>
                </select>
              </label>
              <p>Responses keeps history locally and uses only ScholarWeave functions, not hosted search or code execution. Keep the API base URL (usually ending in /v1); do not append /responses. Use Verify after switching.</p>
              <details>
                <summary>Reasoning overrides</summary>
                <p>Applies to all models in this profile. Explicit per-message reasoning takes precedence over the default. The tool override takes precedence whenever function tools are sent, including verification and delegated agents.</p>
                {(['default_reasoning_effort', 'tool_reasoning_effort'] as const).map((key) => (
                  <label className="field" key={key}>
                    {key === 'default_reasoning_effort' ? 'Default reasoning effort' : 'Reasoning effort with tools'}
                    <select
                      value={provider[key] ?? ''}
                      disabled={busy !== null}
                      onChange={(event) => void setReasoningOverride(provider, key, event.target.value)}
                    >
                      <option value="">No override</option>
                      {REASONING_EFFORTS.map((effort) => <option value={effort} key={effort}>{effort}</option>)}
                    </select>
                  </label>
                ))}
                <p>For the OpenAI error &quot;Function tools with reasoning-effort are not supported&quot;, select Responses if your deployment supports it and clear the tool reasoning override to retain reasoning. Otherwise set reasoning effort with tools to none; it does not enable unsupported reasoning plus tools. Leave overrides unset for models that do not accept reasoning.</p>
              </details>
              <button
                  className="provider-model-catalog-trigger"
                  type="button"
                  aria-pressed={catalogProviderId === provider.id}
                  onClick={() => openCatalog(provider.id)}
                >
                  <span>Manage models</span>
                  <span className="provider-model-catalog-count">
                    {provider.models.filter((model) => model.enabled).length}/{provider.models.length}
                  </span>
                  <Icon name="arrowRight" size={14} />
              </button>
              {!provider.models.length ? <p>No configured models. Discover models or register a model manually in Manage models.</p> : null}
              <div className="button-row">
                <button className="button secondary small" type="button" disabled={busy !== null} onClick={() => void discover(provider)}>{busy === `discover:${provider.id}` ? 'Discovering…' : 'Discover models'}</button>
                <button className="button danger small" type="button" disabled={busy !== null} onClick={() => setArchiveProviderId(provider.id)}>Archive</button>
              </div>
              {archiveProviderId === provider.id ? (
                <div className="notice warning stack" role="alertdialog" aria-label={`Archive ${provider.name}?`}>
                  <strong>Archive {provider.name}?</strong>
                  {referencedRoles.length ? <p>This profile is referenced by saved or unsaved defaults: {referencedRoles.join(', ')}.</p> : null}
                  <p>Models in this profile will become unavailable, including in existing chats and research tasks. Defaults and history will not be cleared. Restore this profile below to recover the same identity, models, and credentials.</p>
                  <div className="button-row">
                    <button className="button danger small" type="button" disabled={busy !== null} onClick={() => void archive(provider)}>
                      {busy === `archive:${provider.id}` ? 'Archiving…' : 'Confirm archive'}
                    </button>
                    <button className="button secondary small" type="button" autoFocus disabled={busy !== null} onClick={() => setArchiveProviderId(null)}>Cancel archive</button>
                  </div>
                </div>
              ) : null}
            </article>
          );
        })}
      </div>
      {providers.some((provider) => provider.state === 'archived') ? (
        <section className="stack provider-archived" aria-label="Archived providers">
          <h2>Archived providers</h2>
          <p>Restore a profile to make its saved model references usable again. Its identity and credentials are retained.</p>
          {providers.filter((provider) => provider.state === 'archived').map((provider) => (
            <div className="toolbar" key={provider.id}>
              <strong>{provider.name}</strong>
              <StatusPill value="archived" />
              <button className="button secondary small" type="button" disabled={busy !== null} onClick={() => void restore(provider)}>
                {busy === `restore:${provider.id}` ? 'Restoring…' : `Restore ${provider.name}`}
              </button>
            </div>
          ))}
        </section>
      ) : null}
      {catalogProvider
        ? createPortal(
            <div
              className="provider-model-backdrop"
              role="presentation"
              onMouseDown={(event) => {
                if (event.target === event.currentTarget) setCatalogProviderId(null);
              }}
            >
              <section
                className="provider-model-dialog"
                role="dialog"
                aria-modal="true"
                aria-labelledby="provider-model-dialog-title"
              >
                <header className="provider-model-dialog-head">
                  <div>
                    <span className="eyebrow">{catalogProvider.kind}</span>
                    <h2 id="provider-model-dialog-title">{catalogProvider.name} models</h2>
                    <p>
                      Enable the models agents may use, then verify any model before
                      assigning it to an agent or workspace default.
                    </p>
                  </div>
                  <div className="provider-model-dialog-summary">
                    <span className="tag">
                      {enabledModelCount} of {catalogProvider.models.length} enabled
                    </span>
                    <button
                      className="button secondary small"
                      type="button"
                      onClick={() => setCatalogProviderId(null)}
                    >
                      <Icon name="close" size={14} />
                      Close
                    </button>
                  </div>
                </header>
                <div className="provider-model-dialog-toolbar">
                  <div className="provider-model-dialog-search">
                    <Icon name="search" size={16} />
                    <input
                      ref={catalogSearchRef}
                      type="search"
                      value={catalogQuery}
                      aria-label="Search models"
                      placeholder="Search models by name or capability…"
                      onChange={(event) =>
                        setModelQueries((current) => ({
                          ...current,
                          [catalogProvider.id]: event.target.value,
                        }))
                      }
                    />
                  </div>
                  <div className="segmented" role="group" aria-label="Filter models">
                    {(['all', 'enabled', 'disabled'] as const).map((filter) => (
                      <button
                        type="button"
                        key={filter}
                        aria-pressed={modelFilter === filter}
                        onClick={() => setModelFilter(filter)}
                      >
                        {filter[0].toUpperCase() + filter.slice(1)}
                      </button>
                    ))}
                  </div>
                  <div className="provider-model-dialog-actions">
                    <button
                      className="button secondary small"
                      type="button"
                      disabled={
                        !visibleCatalogModels.length
                        || modelUpdateBusy
                      }
                      onClick={() => void setVisibleModelsEnabled(catalogProvider, true)}
                    >
                      Enable visible
                    </button>
                    <button
                      className="button secondary small"
                      type="button"
                      disabled={
                        !visibleCatalogModels.length
                        || modelUpdateBusy
                      }
                      onClick={() => void setVisibleModelsEnabled(catalogProvider, false)}
                    >
                      Disable visible
                    </button>
                  </div>
                </div>
                <div className="provider-model-dialog-count">
                  Showing {visibleCatalogModels.length} of {catalogProvider.models.length} models
                </div>
                <div className="provider-models">
                  <form className="provider-model-manual stack" onSubmit={(event) => {
                    event.preventDefault();
                    void registerModel(catalogProvider);
                  }}>
                    <strong>Register a model manually</strong>
                    <p>Discovery is optional. Enter the exact model ID or deployment name accepted by this endpoint. Capability choices declare intended use, not verified support. Verify checks a response and tool calling, or embeddings or System One decisions for dedicated models. Image understanding is not tested.</p>
                    <label className="field">
                      Model or deployment name
                      <input
                        value={manualName}
                        maxLength={255}
                        required
                        disabled={modelUpdateBusy}
                        onChange={(event) => {
                          setManualName(event.target.value);
                          setManualError(null);
                        }}
                      />
                    </label>
                    <fieldset disabled={modelUpdateBusy}>
                      <legend>Model capabilities</legend>
                      <div className="button-row">
                        {modelCapabilities.map((capability) => (
                          <label key={capability}>
                            <input type="checkbox" checked={manualCapabilities.includes(capability)} onChange={(event) => {
                              setManualCapabilities((current) => event.target.checked
                                ? [...current, capability] : current.filter((item) => item !== capability));
                            }} />
                            {capability}
                          </label>
                        ))}
                      </div>
                    </fieldset>
                    {manualError ? <p role="alert">{manualError}</p> : null}
                    <div><button className="button secondary small" type="submit" disabled={modelUpdateBusy || !manualName.trim()}>
                      {busy === `register:${catalogProvider.id}` ? 'Registering…' : 'Register model'}
                    </button></div>
                  </form>
                  {visibleCatalogModels.map((model) => {
                    const key = `${catalogProvider.id}:${model.name}`;
                    const result = verification[key];
                    return (
                      <div className="provider-model-row" key={model.name}>
                        <span className="provider-model-identity">
                          <strong title={model.name}>{model.name}</strong>
                          <small>
                            {model.capabilities?.join(', ') || 'capabilities not declared'}
                          </small>
                        </span>
                        <span
                          className="provider-model-capabilities"
                          role="group"
                          aria-label={`${model.name} capabilities`}
                        >
                          {modelCapabilities.map((capability) => (
                            <label key={capability}>
                              <input
                                type="checkbox"
                                checked={model.capabilities?.includes(capability) ?? false}
                                disabled={modelUpdateBusy}
                                onChange={(event) =>
                                  void setModelCapability(
                                    catalogProvider,
                                    model.name,
                                    capability,
                                    event.target.checked,
                                  )
                                }
                              />
                              {capability}
                            </label>
                          ))}
                        </span>
                        <ModelContextField
                          model={model}
                          disabled={modelUpdateBusy}
                          onSave={(value) => setModelContextWindow(catalogProvider, model.name, value)}
                        />
                        <details className="provider-model-reasoning">
                          <summary>
                            Thinking: {
                              model.reasoning_efforts?.length
                                ? model.reasoning_efforts.join(', ')
                                : model.reasoning_efforts === null
                                  ? 'not configured'
                                  : 'off'
                            }
                          </summary>
                          <span
                            className="provider-model-reasoning-options"
                            role="group"
                            aria-label={`${model.name} supported reasoning levels`}
                          >
                            {REASONING_EFFORTS.map((effort) => (
                              <label key={effort}>
                                <input
                                  type="checkbox"
                                  checked={model.reasoning_efforts?.includes(effort) ?? false}
                                  disabled={modelUpdateBusy}
                                  onChange={(event) =>
                                    void setModelReasoningEffort(
                                      catalogProvider,
                                      model.name,
                                      effort,
                                      event.target.checked,
                                    )
                                  }
                                />
                                {effort}
                              </label>
                            ))}
                          </span>
                          {['ollama', 'openai_compatible'].includes(catalogProvider.kind) ? (
                            <label className="provider-model-preserve-thinking">
                              <input
                                type="checkbox"
                                checked={model.preserve_thinking}
                                disabled={modelUpdateBusy}
                                onChange={(event) =>
                                  void setModelPreserveThinking(
                                    catalogProvider,
                                    model.name,
                                    event.target.checked,
                                  )
                                }
                              />
                              Preserve thinking between model calls
                            </label>
                          ) : null}
                        </details>
                        <label className="provider-model-enabled">
                          <input
                            type="checkbox"
                            checked={model.enabled}
                            disabled={modelUpdateBusy}
                            onChange={(event) =>
                              void setModelEnabled(
                                catalogProvider,
                                model.name,
                                event.target.checked,
                              )
                            }
                          />
                          {model.enabled ? 'Enabled' : 'Disabled'}
                        </label>
                        <button
                          className="button secondary small"
                          type="button"
                          disabled={busy !== null}
                          onClick={() => void verify(catalogProvider, model.name)}
                        >
                          {busy === `verify:${key}` ? 'Verifying…' : 'Verify'}
                        </button>
                        {busy === `verify:${key}` ? <p role="status">Checking {model.name} capabilities…</p> : null}
                        {verificationErrors[key] ? <p className="notice danger provider-verification" role="alert">Verification failed: {verificationErrors[key]}</p> : null}
                        {result ? (
                          <div className="provider-verification" role="status">
                            <strong>
                              {!result.reachable ? 'Verification failed'
                                : model.capabilities?.includes('decision') && !model.capabilities.some((capability) => ['chat', 'tools', 'vision'].includes(capability))
                                  ? 'Decision response verified · tools not tested'
                                  : model.capabilities?.includes('embedding') && !model.capabilities.some((capability) => ['chat', 'tools'].includes(capability))
                                    ? 'Embedding response verified · tools not tested'
                                    : result.tool_calling ? 'Response and tool calling verified' : 'Response received · tool calling not verified'}
                            </strong>
                            <p>{result.detail}</p>
                          </div>
                        ) : null}
                      </div>
                    );
                  })}
                  {!visibleCatalogModels.length ? (
                    <p className="provider-model-empty">
                      No models match the current search and filter.
                    </p>
                  ) : null}
                </div>
              </section>
            </div>,
            document.body,
          )
        : null}
    </Panel>
  );
}
