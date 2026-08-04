import { useEffect, useState } from 'react';
import { useConfirm } from '../common/ConfirmDialog';
import { ErrorNotice } from '../common/ErrorNotice';
import { Icon } from '../common/Icon';
import { Modal } from '../common/Modal';
import { toMessage, useToast } from '../common/Toast';
import { ProviderModelPicker } from '../workflow/ProviderModelPicker';
import { api } from '../../lib/api';
import { MODEL_CAPABILITIES } from '../../lib/models';
import type {
  ModelCapability,
  ModelReference,
  ProviderCheckResponse,
  ProviderKind,
  ProviderModelEntry,
  ProviderProfileResponse,
  SettingsResponse,
} from '../../types/api';

const PROFILE_KINDS: Array<{ id: ProviderKind; label: string; hint: string }> = [
  { id: 'ollama', label: 'Ollama', hint: 'Local models and the Ollama API.' },
  { id: 'openai', label: 'OpenAI', hint: 'OpenAI API-key authentication.' },
  { id: 'azure_openai', label: 'Azure OpenAI', hint: 'Azure endpoint, API key, and API version.' },
  { id: 'azure_foundry', label: 'Azure AI Foundry', hint: 'Foundry-compatible OpenAI endpoint and API key.' },
  { id: 'openai_compatible', label: 'OpenAI-compatible', hint: 'A gateway or compatible service using an API key.' },
];

interface ProfileForm {
  name: string;
  kind: ProviderKind;
  base_url: string;
  api_version: string;
  api_key: string;
  models: ProviderModelEntry[];
}

function emptyProfile(): ProfileForm {
  return {
    name: '',
    kind: 'ollama',
    base_url: 'http://127.0.0.1:11434',
    api_version: '',
    api_key: '',
    models: [],
  };
}

function toForm(profile?: ProviderProfileResponse | null): ProfileForm {
  if (!profile) return emptyProfile();
  return {
    name: profile.name,
    kind: profile.kind,
    base_url: profile.base_url,
    api_version: profile.api_version || '',
    api_key: '',
    models: profile.models.map((model) => ({ name: model.name, capabilities: [...model.capabilities] })),
  };
}

function kindLabel(kind: ProviderKind): string {
  return PROFILE_KINDS.find((entry) => entry.id === kind)?.label || kind;
}

function ProviderProfileModal({
  profile,
  onClose,
  onSaved,
}: {
  profile?: ProviderProfileResponse | null;
  onClose: () => void;
  onSaved: (profile: ProviderProfileResponse) => void;
}) {
  const toast = useToast();
  const [form, setForm] = useState(() => toForm(profile));
  const [discovered, setDiscovered] = useState<ProviderModelEntry[]>([]);
  const [discoveryError, setDiscoveryError] = useState('');
  const [verifyResult, setVerifyResult] = useState<ProviderCheckResponse | null>(null);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');

  useEffect(() => {
    setForm(toForm(profile));
    setDiscovered([]);
    setDiscoveryError('');
    setVerifyResult(null);
    setError('');
  }, [profile?.id]);

  const isAzure = form.kind === 'azure_openai' || form.kind === 'azure_foundry';
  const valid = form.name.trim() && form.base_url.trim();

  const patchModel = (index: number, patch: Partial<ProviderModelEntry>) =>
    setForm((current) => ({
      ...current,
      models: current.models.map((model, modelIndex) => (modelIndex === index ? { ...model, ...patch } : model)),
    }));

  const addDiscovered = () => {
    setForm((current) => {
      const capabilities = new Map(current.models.map((model) => [model.name, model.capabilities]));
      discovered.forEach((model) => {
        if (!capabilities.has(model.name)) capabilities.set(model.name, model.capabilities);
      });
      return {
        ...current,
        models: [...capabilities.entries()].map(([name, capabilities]) => ({ name, capabilities })),
      };
    });
  };

  const discover = async () => {
    if (!profile) return;
    setBusy('discover');
    setDiscoveryError('');
    try {
      const result = await api.discoverProviderModels(profile.id);
      setDiscovered(result.models);
      setDiscoveryError(result.discovery_error || '');
      if (result.discovery_error) {
        toast.info('Discovery could not reach the provider', 'You can still edit model names and save this profile.');
      } else {
        toast.success('Models discovered', `${result.models.length} model${result.models.length === 1 ? '' : 's'} returned.`);
      }
    } catch (err) {
      const message = toMessage(err, 'Could not discover models');
      setDiscoveryError(message);
      toast.info('Discovery unavailable', 'Manual model entries can still be saved.');
    } finally {
      setBusy('');
    }
  };

  const verify = async (model: string) => {
    if (!profile || !model) return;
    setBusy(`verify:${model}`);
    setVerifyResult(null);
    try {
      const result = await api.verifyProviderProfile(profile.id, { model });
      setVerifyResult(result);
      if (result.reachable) toast.success('Provider verified', `${model} is reachable.`);
      else toast.failure('Provider is unreachable', result.detail || 'Check the profile connection details.');
    } catch (err) {
      const message = toMessage(err, 'Could not verify this model');
      setError(message);
      toast.failure('Verification failed', message);
    } finally {
      setBusy('');
    }
  };

  const save = async () => {
    if (!valid) {
      setError('Name and base URL are required.');
      return;
    }
    const duplicate = form.models.find((entry, index) => entry.name.trim() && form.models.findIndex((other) => other.name.trim() === entry.name.trim()) !== index);
    if (duplicate) {
      setError(`Model "${duplicate.name}" is listed more than once.`);
      return;
    }
    setBusy('save');
    setError('');
    const payload = {
      name: form.name.trim(),
      kind: form.kind,
      base_url: form.base_url.trim(),
      api_version: form.api_version.trim() || null,
      ...(form.api_key ? { api_key: form.api_key } : {}),
      models: form.models.filter((model) => model.name.trim()).map((model) => ({ ...model, name: model.name.trim() })),
    };
    try {
      const saved = profile
        ? await api.updateProviderProfile(profile.id, payload)
        : await api.createProviderProfile(payload);
      toast.success(profile ? 'Provider profile updated' : 'Provider profile created', saved.name);
      onSaved(saved);
    } catch (err) {
      const message = toMessage(err, 'Could not save provider profile');
      setError(message);
      toast.failure('Profile not saved', message);
    } finally {
      setBusy('');
    }
  };

  return (
    <Modal
      title={profile ? `Edit ${profile.name}` : 'Add provider profile'}
      description="Profiles use API-key authentication only. Stored keys are never shown again."
      onClose={onClose}
      className="provider-profile-modal"
    >
      <div className="provider-profile-scroll">
        {error ? <ErrorNotice message={error} /> : null}
        <div className="form-grid two-col">
          <div className="field-stack">
            <label className="field-label" htmlFor="provider-profile-name">Profile name</label>
            <input id="provider-profile-name" className="input" value={form.name} placeholder="Production OpenAI" onChange={(event) => setForm((current) => ({ ...current, name: event.target.value }))} />
          </div>
          <div className="field-stack">
            <label className="field-label" htmlFor="provider-profile-kind">Provider kind</label>
            <select id="provider-profile-kind" className="input" value={form.kind} onChange={(event) => setForm((current) => ({ ...current, kind: event.target.value as ProviderKind }))}>
              {PROFILE_KINDS.map((kind) => <option key={kind.id} value={kind.id}>{kind.label}</option>)}
            </select>
            <p className="field-hint">{PROFILE_KINDS.find((kind) => kind.id === form.kind)?.hint}</p>
          </div>
          <div className="field-stack">
            <label className="field-label" htmlFor="provider-profile-url">Base URL</label>
            <input id="provider-profile-url" className="input" value={form.base_url} placeholder="https://api.openai.com/v1" onChange={(event) => setForm((current) => ({ ...current, base_url: event.target.value }))} />
          </div>
          {isAzure ? (
            <div className="field-stack">
              <label className="field-label" htmlFor="provider-profile-version">API version</label>
              <input id="provider-profile-version" className="input" value={form.api_version} placeholder="2024-10-21" onChange={(event) => setForm((current) => ({ ...current, api_version: event.target.value }))} />
              <p className="field-hint">{form.kind === 'azure_openai' ? 'Required by Azure OpenAI.' : 'Add when required by your Foundry endpoint.'}</p>
            </div>
          ) : null}
          <div className={`field-stack${isAzure ? '' : ' span-2'}`}>
            <label className="field-label" htmlFor="provider-profile-key">API key</label>
            <input
              id="provider-profile-key"
              className="input"
              type="password"
              autoComplete="off"
              value={form.api_key}
              placeholder={profile?.api_key_set ? 'Stored — type to replace' : form.kind === 'ollama' ? 'Optional for this Ollama endpoint' : 'Paste API key'}
              onChange={(event) => setForm((current) => ({ ...current, api_key: event.target.value }))}
            />
            <p className="field-hint">Leave this blank to keep the stored key unchanged. It is not returned by the API.</p>
          </div>
        </div>

        <section className="provider-model-editor">
          <div className="provider-model-editor-head">
            <div>
              <h3>Models and capabilities</h3>
              <p>Capability tags let model pickers avoid an incompatible default. Untagged discovered models remain available.</p>
            </div>
            <div className="button-row compact">
              {profile ? (
                <button type="button" className="button subtle sm" disabled={busy === 'discover'} onClick={() => void discover()}>
                  <Icon name="refresh" size={12} />
                  {busy === 'discover' ? 'Discovering…' : 'Discover'}
                </button>
              ) : null}
              <button type="button" className="button subtle sm" onClick={() => setForm((current) => ({ ...current, models: [...current.models, { name: '', capabilities: [] } ] }))}>
                <Icon name="plus" size={12} />
                Add model
              </button>
            </div>
          </div>
          {!profile ? <p className="field-hint">Save the profile first to discover models. You can add manual names now.</p> : null}
          {discoveryError ? <div className="inline-notice danger">{discoveryError}</div> : null}
          {discovered.length ? (
            <div className="provider-discovery-result">
              <span>{discovered.length} discovered</span>
              <button type="button" className="link-button" onClick={addDiscovered}>Add discovered models</button>
            </div>
          ) : null}
          <div className="provider-model-rows">
            {form.models.map((model, index) => (
              <div className="provider-model-row" key={`model-${index}`}>
                <input className="input" aria-label={`Model ${index + 1} name`} value={model.name} placeholder="Model name" onChange={(event) => patchModel(index, { name: event.target.value })} />
                <div className="provider-capabilities" aria-label={`Capabilities for ${model.name || `model ${index + 1}`}`}>
                  {MODEL_CAPABILITIES.map((capability) => (
                    <label key={capability.id} title={capability.description}>
                      <input
                        type="checkbox"
                        checked={model.capabilities.includes(capability.id)}
                        onChange={(event) => {
                          const capabilities = event.target.checked
                            ? [...model.capabilities, capability.id]
                            : model.capabilities.filter((value) => value !== capability.id);
                          patchModel(index, { capabilities });
                        }}
                      />
                      <span>{capability.label}</span>
                    </label>
                  ))}
                </div>
                {profile && model.name.trim() ? (
                  <button type="button" className="button subtle sm" disabled={busy === `verify:${model.name}`} onClick={() => void verify(model.name.trim())}>
                    <Icon name="check" size={12} />
                    {busy === `verify:${model.name}` ? 'Checking…' : 'Verify'}
                  </button>
                ) : null}
                <button type="button" className="button subtle icon-only sm" aria-label={`Remove ${model.name || 'model'}`} onClick={() => setForm((current) => ({ ...current, models: current.models.filter((_, modelIndex) => modelIndex !== index) }))}>
                  <Icon name="trash" size={13} />
                </button>
              </div>
            ))}
            {!form.models.length ? <p className="empty-state">No model names yet. Add one manually or discover models after saving.</p> : null}
          </div>
          {verifyResult ? (
            <div className={`inline-notice ${verifyResult.reachable ? 'success' : 'danger'}`}>
              <strong>{verifyResult.model || 'Provider'}</strong> — {verifyResult.reachable ? 'reachable' : 'unreachable'}{verifyResult.detail ? `: ${verifyResult.detail}` : ''}
            </div>
          ) : null}
        </section>
      </div>
      <footer className="button-row end provider-profile-footer">
        <button type="button" className="button subtle" onClick={onClose}>Cancel</button>
        <button type="button" className="button primary" disabled={!valid || busy === 'save'} onClick={() => void save()}>
          <Icon name="save" size={13} />
          {busy === 'save' ? 'Saving…' : profile ? 'Save profile' : 'Create profile'}
        </button>
      </footer>
    </Modal>
  );
}

export function ProviderManagement({
  defaults,
  onDefaultsChange,
}: {
  defaults: Record<string, ModelReference>;
  onDefaultsChange: (defaults: Record<string, ModelReference>) => void;
}) {
  const toast = useToast();
  const confirm = useConfirm();
  const [profiles, setProfiles] = useState<ProviderProfileResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [editor, setEditor] = useState<ProviderProfileResponse | null | undefined>(undefined);
  const [archiving, setArchiving] = useState('');

  const refresh = async () => {
    setLoading(true);
    try {
      setProfiles(await api.listProviderProfiles(true));
      setError('');
    } catch (err) {
      setError(toMessage(err, 'Could not load provider profiles'));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void refresh();
  }, []);

  const updateDefault = (capability: ModelCapability, reference: ModelReference | null) => {
    const next = { ...defaults };
    if (reference?.provider_profile_id || reference?.model) next[capability] = reference;
    else delete next[capability];
    onDefaultsChange(next);
  };

  const archive = async (profile: ProviderProfileResponse) => {
    const approved = await confirm({
      title: `Archive “${profile.name}”?`,
      description: 'Existing workflows keep their saved references, but this profile will not appear for new selections.',
      confirmLabel: 'Archive profile',
    });
    if (!approved) return;
    setArchiving(profile.id);
    try {
      await api.archiveProviderProfile(profile.id);
      await refresh();
      toast.success('Provider profile archived', profile.name);
    } catch (err) {
      const message = toMessage(err, 'Could not archive provider profile');
      setError(message);
      toast.failure('Archive failed', message);
    } finally {
      setArchiving('');
    }
  };

  return (
    <section className="panel stack gap-md provider-management">
      <div className="panel-subheader">
        <div>
          <h3>Provider profiles</h3>
          <p className="muted-text small">Name each connection once, keep API keys masked, and choose capability-aware defaults.</p>
        </div>
        <div className="button-row compact">
          <button type="button" className="button subtle icon-only sm" aria-label="Refresh provider profiles" title="Refresh provider profiles" onClick={() => void refresh()}>
            <Icon name="refresh" size={13} />
          </button>
          <button type="button" className="button primary sm" onClick={() => setEditor(null)}>
            <Icon name="plus" size={13} />
            Add profile
          </button>
        </div>
      </div>
      {error ? <ErrorNotice message={error} /> : null}
      <div className="provider-profile-list">
        {profiles.map((profile) => (
          <article className={`provider-profile-card${profile.state === 'archived' ? ' archived' : ''}`} key={profile.id}>
            <div className="provider-profile-card-main">
              <div className="provider-profile-title">
                <strong>{profile.name}</strong>
                <span className="tiny-tag">{kindLabel(profile.kind)}</span>
                {profile.state === 'archived' ? <span className="tiny-tag danger">Archived</span> : null}
              </div>
              <p>{profile.base_url}</p>
              <div className="provider-profile-models">
                {profile.models.map((model) => (
                  <span key={model.name} title={model.capabilities.join(', ') || 'Capabilities not tagged'}>
                    {model.name}
                    {model.capabilities.length ? ` · ${model.capabilities.join(', ')}` : ''}
                  </span>
                ))}
                {!profile.models.length ? <span>No models tagged</span> : null}
              </div>
            </div>
            <div className="button-row compact provider-profile-buttons">
              <button type="button" className="button subtle sm" onClick={() => setEditor(profile)}>Edit</button>
              {profile.state === 'active' ? (
                <button type="button" className="button subtle icon-only sm danger-text" aria-label={`Archive ${profile.name}`} disabled={archiving === profile.id} onClick={() => void archive(profile)}>
                  <Icon name="trash" size={13} />
                </button>
              ) : null}
            </div>
          </article>
        ))}
        {!loading && !profiles.length ? <p className="empty-state">Add an Ollama, OpenAI, Azure, Foundry, or compatible profile to start choosing models.</p> : null}
        {loading ? <p className="empty-state">Loading provider profiles…</p> : null}
      </div>

      <section className="provider-global-defaults">
        <div>
          <strong>Global model defaults</strong>
          <p>Workflows inherit these unless they set their own defaults or node overrides.</p>
        </div>
        {(['chat', 'tools', 'embedding', 'vision'] as const).map((capability) => (
          <ProviderModelPicker
            key={capability}
            id={`settings-default-${capability}`}
            label={`${capability[0].toUpperCase()}${capability.slice(1)} default`}
            capability={capability}
            value={defaults[capability] || null}
            onChange={(reference) => updateDefault(capability, reference)}
            profiles={profiles}
            settings={{ default_model_references: defaults } as Pick<SettingsResponse, 'default_model_references'>}
            allowInherited
            selectionSource="settings"
          />
        ))}
      </section>

      {editor !== undefined ? (
        <ProviderProfileModal
          profile={editor}
          onClose={() => setEditor(undefined)}
          onSaved={(profile) => {
            setEditor(undefined);
            void refresh();
            toast.success('Profile ready', `${profile.name} can now be selected by workflows.`);
          }}
        />
      ) : null}
    </section>
  );
}
