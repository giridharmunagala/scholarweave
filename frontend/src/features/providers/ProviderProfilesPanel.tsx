import { useEffect, useRef, useState } from 'react';
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
  type ProviderVerification,
} from './api';

const emptyProvider: ProviderCreate = {
  name: 'Local Ollama',
  kind: 'ollama',
  base_url: 'http://127.0.0.1:11434',
  api_key: null,
  models: [],
};
const modelCapabilities = ['chat', 'tools', 'embedding', 'vision'] as const;
type ModelCapability = (typeof modelCapabilities)[number];
const modelSwitchHelp =
  'One LLM call at a time across all providers, including chat, summaries, and context maintenance. Queued chat calls get priority between responses, with regular turns for background work. Your model server manages hot swaps; no manual confirmation is needed.';

export function ProviderProfilesPanel({
  providers,
  onRefresh,
  onError,
}: {
  providers: Provider[];
  onRefresh: () => Promise<void>;
  onError: (error: unknown) => void;
}) {
  const [draft, setDraft] = useState<ProviderCreate>(emptyProvider);
  const [showCreate, setShowCreate] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [verification, setVerification] = useState<
    Record<string, ProviderVerification>
  >({});
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
  const modelUpdateBusy =
    busy?.startsWith('enable:')
    || busy?.startsWith('enable-visible:')
    || busy?.startsWith('configure:');

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
      await providersApi.create(draft);
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
    try {
      const result = await providersApi.verify(provider.id, model);
      setVerification((current) => ({ ...current, [key]: result }));
    } catch (error) {
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
    } catch (error) {
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
    } catch (error) {
      onError(error);
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
    setCatalogProviderId(providerId);
  };

  return (
    <Panel
      title="Provider profiles"
      description="All chat providers use the OpenAI-compatible Chat Completions protocol."
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
        {providers.map((provider) => {
          return (
            <article className="card stack" key={provider.id}>
              <div className="toolbar"><span className="eyebrow">{provider.kind}</span><StatusPill value={provider.state} /></div>
              <div><h2>{provider.name}</h2><code>{provider.base_url}</code></div>
              {provider.models.length ? (
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
              ) : <p>No configured models. Discover models before assigning defaults.</p>}
              <div className="button-row">
                <button className="button secondary small" type="button" disabled={busy === `discover:${provider.id}`} onClick={() => void discover(provider)}>{busy === `discover:${provider.id}` ? 'Discovering…' : 'Discover models'}</button>
                <button className="button danger small" type="button" disabled={busy === `archive:${provider.id}`} onClick={() => void archive(provider)}>Archive</button>
              </div>
            </article>
          );
        })}
      </div>
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
                        <label className="provider-model-context-window">
                          Context
                          <input
                            type="number"
                            min={4096}
                            max={2000000}
                            step={1024}
                            defaultValue={model.context_window_tokens ?? ''}
                            placeholder="fallback"
                            disabled={modelUpdateBusy}
                            aria-label={`${model.name} context window tokens`}
                            onBlur={(event) => {
                              const nextValue = event.target.value
                                ? Number(event.target.value)
                                : null;
                              if (
                                nextValue !== (model.context_window_tokens ?? null)
                                && (
                                  nextValue === null
                                  || (
                                    Number.isInteger(nextValue)
                                    && nextValue >= 4096
                                    && nextValue <= 2000000
                                  )
                                )
                              ) {
                                void setModelContextWindow(
                                  catalogProvider,
                                  model.name,
                                  nextValue,
                                );
                              }
                            }}
                          />
                          tokens
                        </label>
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
                          disabled={busy === `verify:${key}`}
                          onClick={() => void verify(catalogProvider, model.name)}
                        >
                          Verify
                        </button>
                        {result ? (
                          <StatusPill value={result.reachable ? 'reachable' : 'failed'} />
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
