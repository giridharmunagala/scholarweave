import { useState } from 'react';
import { Icon } from '../../shared/components/Icons';
import { Panel, StatusPill } from '../../shared/components/Ui';
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

  return (
    <Panel
      title="Provider profiles"
      description="OpenAI uses Responses; Ollama and compatible profiles use Chat Completions."
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
      <div className="card-grid">
        {providers.map((provider) => (
          <article className="card stack" key={provider.id}>
            <div className="toolbar"><span className="eyebrow">{provider.kind}</span><StatusPill value={provider.state} /></div>
            <div><h2>{provider.name}</h2><code>{provider.base_url}</code></div>
            {provider.models.length ? (
              <div className="provider-models">
                {provider.models.map((model) => {
                  const key = `${provider.id}:${model.name}`;
                  const result = verification[key];
                  return (
                    <div className="provider-model-row" key={model.name}>
                      <span><strong>{model.name}</strong><small>{model.capabilities?.join(', ') || 'capabilities not declared'}</small></span>
                      <button className="button secondary small" type="button" disabled={busy === `verify:${key}`} onClick={() => void verify(provider, model.name)}>Verify</button>
                      {result ? <StatusPill value={result.reachable ? 'reachable' : 'failed'} /> : null}
                    </div>
                  );
                })}
              </div>
            ) : <p>No configured models. Discover models before assigning defaults.</p>}
            <div className="button-row">
              <button className="button secondary small" type="button" disabled={busy === `discover:${provider.id}`} onClick={() => void discover(provider)}>{busy === `discover:${provider.id}` ? 'Discovering…' : 'Discover models'}</button>
              <button className="button danger small" type="button" disabled={busy === `archive:${provider.id}`} onClick={() => void archive(provider)}>Archive</button>
            </div>
          </article>
        ))}
      </div>
    </Panel>
  );
}
