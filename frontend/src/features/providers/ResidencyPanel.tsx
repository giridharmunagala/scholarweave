import { useEffect, useState } from 'react';
import { providersApi, type Provider, type ResidencyStatus } from './api';

export function ResidencyPanel({ providers }: { providers: Provider[] }) {
  const [status, setStatus] = useState<ResidencyStatus | null>(null);
  const [profileId, setProfileId] = useState('');
  const [model, setModel] = useState('');
  const [mode, setMode] = useState<'interactive' | 'batch'>('interactive');
  const [attested, setAttested] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    let live = true;
    const refresh = () => providersApi.residency()
      .then((value) => { if (live) setStatus(value); })
      .catch((reason: unknown) => { if (live) setError(String(reason)); });
    void refresh();
    const timer = setInterval(() => void refresh(), 2000);
    return () => { live = false; clearInterval(timer); };
  }, []);

  const operate = async (action: () => Promise<ResidencyStatus>) => {
    setBusy(true);
    setError('');
    setAttested(false);
    try { setStatus(await action()); }
    catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  };
  const selectedId = status?.profile_id ?? profileId;
  return (
    <section className="card stack" aria-label="Single GPU residency">
      <h2>Single GPU residency</h2>
      <p>
        Opt-in protection for a manually managed local, single-model OpenAI-compatible server.
        ScholarWeave never loads, unloads, or downgrades models. The request model field is not a load command.
      </p>
      {error && <p role="alert">{error}</p>}
      {!status?.enabled ? (
        <div className="field-row">
          <label className="field">Local server profile
            <select value={profileId} onChange={(event) => setProfileId(event.target.value)}>
              <option value="">Choose profile</option>
              {providers.filter((provider) => provider.kind === 'openai_compatible').map((provider) =>
                <option key={provider.id} value={provider.id}>{provider.name}</option>)}
            </select>
          </label>
          <button className="button secondary" disabled={busy || !status || !profileId}
            onClick={() => void operate(() => providersApi.configureResidency(profileId, true))}>
            Enable protection
          </button>
        </div>
      ) : (
        <>
          <p>
            Resident: <strong>{status.resident_model ?? 'Unknown'}</strong>
            {' — '}{status.confirmed ? `${status.session_mode} session` : 'confirmation required'}
            {status.paused ? ' — paused' : ''}
          </p>
          <p aria-live="polite">
            Active: {status.active_requests}; queued interactive: {status.interactive_queued};
            {' '}background: {status.background_queued}
          </p>
          <ul>{status.queue.map((entry, index) =>
            <li key={index}>{entry.model ?? 'Unknown model'} ({entry.priority})
              {entry.blocked_by_residency ? ' — waiting for residency confirmation' : ' — ready in queue'}
            </li>)}</ul>
          <p>
            First drain and pause. Wait for active requests to finish, then load the desired weights
            in your server yourself. Confirm only after it is ready. Other models remain queued.
            Background jobs use the same resident-model lock; interactive requests have bounded priority.
          </p>
          <button className="button secondary" disabled={busy}
            onClick={() => void operate(() => providersApi.drainResidency(selectedId))}>
            {busy ? 'Working…' : 'Drain and pause for external switch'}
          </button>
          {status.paused && status.active_requests === 0 && (
            <>
              <label className="field">Exact model ID reported by /v1/models
                <input value={model} onChange={(event) => { setModel(event.target.value); setAttested(false); }} />
              </label>
              <label className="field">Session
                <select value={mode} onChange={(event) => setMode(event.target.value as typeof mode)}>
                  <option value="interactive">Interactive session</option>
                  <option value="batch">Same-model batch session</option>
                </select>
              </label>
              <label>
                <input type="checkbox" checked={attested} onChange={(event) => setAttested(event.target.checked)} />
                I loaded these weights externally and the server is ready.
              </label>
              <p>
                Confirmation probes /v1/models and requires exactly this one model ID.
                A model catalog cannot prove loaded weights; your confirmation is required.
                Restarting ScholarWeave requires confirmation again.
              </p>
              <button className="button" disabled={busy || !model.trim() || !attested}
                onClick={() => void operate(() => providersApi.confirmResidency(selectedId, model.trim(), mode))}>
                Confirm external load and resume
              </button>
            </>
          )}
          <button className="button secondary" disabled={busy || status.active_requests > 0 || status.queue.length > 0}
            onClick={() => void operate(() => providersApi.configureResidency(selectedId, false))}>
            Disable protection (requires empty queue)
          </button>
        </>
      )}
    </section>
  );
}
