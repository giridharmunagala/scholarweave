import { useEffect, useState } from 'react';
import { ApiError } from '../../api/client';
import { useNavigationGuard } from '../../app/router';
import { LocalPathPicker } from '../../shared/components/LocalPathPicker';
import { tracksApi, type SectionContent } from './api';
import { RepositoryPdfPreview } from './RepositoryPdfPreview';
import { RepositoryTextPreview } from './RepositoryTextPreview';
import './linkedRepositories.css';
import {
  linkedRepositoryApi as api, type LinkedRepository, type RepositoryArtifact, type RepositoryListing, type RepositoryPreview,
} from './linkedRepositoryApi';

export type LinkedRepositoryPanelProps = {
  trackId: string;
  experiments: { id: string; experiment?: string }[];
  onResultsSaved?: (section: SectionContent) => void;
};

/** Keep this keyed by trackId so a draft can never cross track boundaries. */
export function LinkedRepositoryPanel({ trackId, experiments, onResultsSaved }: LinkedRepositoryPanelProps) {
  const [links, setLinks] = useState<LinkedRepository[]>([]);
  const [root, setRoot] = useState('');
  const [authorized, setAuthorized] = useState(false);
  const [selected, setSelected] = useState('');
  const [listing, setListing] = useState<RepositoryListing | null>(null);
  const [preview, setPreview] = useState<RepositoryPreview | null>(null);
  const [draft, setDraft] = useState('');
  const [editing, setEditing] = useState(false);
  const [comparison, setComparison] = useState<{ original: RepositoryPreview; current: RepositoryPreview } | null>(null);
  const [staleEdit, setStaleEdit] = useState(false);
  const [experiment, setExperiment] = useState('');
  const [revision, setRevision] = useState('');
  const [note, setNote] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [promotion, setPromotion] = useState<{ artifact: RepositoryArtifact; baseline: SectionContent } | null>(null);
  const [observation, setObservation] = useState('');
  const dirty = !!preview && editing && draft !== preview.content;
  const linked = links.find((link) => link.id === selected);
  useNavigationGuard(dirty || busy || !!promotion, 'Discard unsaved repository changes or Results observation?', !busy);
  useEffect(() => {
    let active = true;
    setLinks([]); setSelected(''); setListing(null); setPreview(null); setEditing(false); setPromotion(null);
    setComparison(null); setStaleEdit(false);
    api.list(trackId).then((value) => { if (active) setLinks(value); })
      .catch((reason: unknown) => { if (active) setError(String(reason)); });
    return () => { active = false; };
  }, [trackId]);
  const run = async (action: () => Promise<void>) => {
    setBusy(true); setError(''); setNotice('');
    try { await action(); } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(false); }
  };
  const discard = () => {
    if ((dirty || promotion) && !window.confirm('Discard unsaved repository changes or Results observation?')) return false;
    setPromotion(null);
    return true;
  };
  const update = (value: LinkedRepository) => setLinks((items) => items.map((link) => link.id === value.id ? value : link));
  const browse = async (link: string, path = '') => {
    const value = await api.browse(trackId, link, path);
    setSelected(link); setListing(value); setPreview(null); setEditing(false);
    setComparison(null); setStaleEdit(false);
  };
  const openFile = async (path: string) => {
    const value = await api.preview(trackId, selected, path);
    setPreview(value); setDraft(value.content || ''); setEditing(false);
    setComparison(null); setStaleEdit(false);
  };
  const compareFile = async () => {
    if (!preview) return;
    const current = await api.preview(trackId, selected, preview.path);
    setComparison((previous) => ({ original: previous?.original ?? preview, current }));
    if (current.sha256 !== preview.sha256) setStaleEdit(true);
    setNotice('Current file loaded for comparison only. Your draft and save baseline are unchanged.');
  };
  const downloadDraft = () => {
    if (!preview) return;
    const url = URL.createObjectURL(new Blob([draft], { type: 'text/plain;charset=utf-8' }));
    const anchor = document.createElement('a');
    anchor.href = url; anchor.download = `${preview.path.split('/').pop() || 'file'}.draft.txt`;
    document.body.append(anchor); anchor.click(); anchor.remove(); URL.revokeObjectURL(url);
  };
  return <section aria-label="Linked research repositories" className="card linked-repositories">
    <h2>Linked code &amp; artifacts</h2>
    <p>Browse an existing local repository and make small manual text edits. No code execution, terminal,
      coding agent, indexing, or automatic model context. Hidden files, secrets and dependencies are excluded.</p>
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    <fieldset disabled={busy}>
      <legend>Connect an existing repository</legend>
      <LocalPathPicker label="Absolute repository path" kind="folder" value={root}
        onBusyChange={setBusy} onChange={(path) => { setRoot(path); setAuthorized(false); }} />
      <label><input type="checkbox" checked={authorized} onChange={(event) => setAuthorized(event.target.checked)} />
        I authorize browsing and manual edits inside this root.</label>
      <button className="button" disabled={!authorized || !root.trim()} onClick={() => void run(async () => {
        const value = await api.link(trackId, root);
        setLinks((items) => [...items, value]); setRoot(''); setAuthorized(false);
      })}>Connect repository</button>
    </fieldset>
    <fieldset disabled={busy}>
      <legend>Repositories</legend>
      {links.length === 0 && <p>No repositories connected.</p>}
      {links.map((link) => <div className="repository-actions" key={link.id}>
        <button className="button secondary" aria-pressed={selected === link.id} onClick={() => {
          if (discard()) void run(() => browse(link.id));
        }}>{link.root}</button>
        <button className="button secondary" onClick={() => {
          if ((selected !== link.id || discard()) && window.confirm('Unlink repository and its artifact receipts? Files on disk will not be deleted.')) {
            void run(async () => {
              await api.unlink(trackId, link.id); setLinks((items) => items.filter((item) => item.id !== link.id));
              if (selected === link.id) { setSelected(''); setListing(null); setPreview(null); setEditing(false); }
            });
          }
        }}>Unlink {link.root}</button>
      </div>)}
    </fieldset>
    {linked && <fieldset disabled={busy}>
      <legend>Browse {linked.root}</legend>
      <p>Current revision (last observed): <code>{linked.current_revision || 'Unknown'}</code>.
        This does not identify which revision produced an artifact, or certify a clean working tree.</p>
      <button className="button secondary" onClick={() => void run(async () => {
        update(await api.refresh(trackId, selected));
        setListing(await api.browse(trackId, selected, listing?.path || ''));
        if (preview && editing) await compareFile();
      })}>Refresh files &amp; artifact hashes</button>
      <p>Folder: {listing?.path || '(root)'}</p>
      {listing?.path && <button className="button secondary" onClick={() => {
        if (discard()) void run(() => browse(selected, listing.path.split('/').slice(0, -1).join('/')));
      }}>Parent folder</button>}
      {listing?.truncated && <p role="status">Directory listing capped. Only the first bounded set of entries is shown.</p>}
      <ul className="repository-files">{listing?.entries.map((entry) => <li key={entry.path}>
        <button className="button secondary" onClick={() => {
          if (!discard()) return;
          void run(async () => {
            if (entry.directory) await browse(selected, entry.path);
            else await openFile(entry.path);
          });
        }}>{entry.directory ? 'Folder: ' : 'File: '}{entry.name}</button>
      </li>)}</ul>
      {preview && <section aria-label="Repository file preview" className="repository-preview">
        <h3>{preview.path}</h3>
        <p>{preview.size_bytes} bytes · SHA-256 <code>{preview.sha256}</code></p>
        {preview.kind === 'text' && !editing && <RepositoryTextPreview path={preview.path} content={preview.content || ''}
          onOpenRelative={(path) => { if (discard()) void run(() => openFile(path)); }} />}
        {preview.kind === 'image' && <img
          src={api.media(trackId, selected, preview.path, preview.sha256)} alt={preview.path}
          onError={() => setError('Image could not be loaded or changed since preview. Reopen it to retry.')} />}
        {preview.kind === 'pdf' && <RepositoryPdfPreview key={`${selected}:${preview.path}:${preview.sha256}`}
          url={api.media(trackId, selected, preview.path, preview.sha256)} title={preview.path} />}
        {preview.kind === 'binary' && <p>Binary or oversized text artifact: hash and metadata preview only.</p>}
        {preview.editable && !editing && <button className="button secondary" onClick={() => setEditing(true)}>Edit text manually</button>}
        {editing && <>
          <label>Manual file content<textarea rows={18} value={draft}
            onChange={(event) => setDraft(event.target.value)} /></label>
          <button className="button" disabled={!dirty || staleEdit} onClick={() => void run(async () => {
            try {
              const value = await api.edit(trackId, selected, preview, draft);
              setPreview(value); setDraft(value.content || ''); setEditing(false);
              setComparison(null); setStaleEdit(false);
            } catch (reason) {
              if (reason instanceof ApiError && reason.status === 409) setStaleEdit(true);
              setNotice('Your manual draft is retained. Compare the current file and reconcile before retrying, or download your draft.');
              throw reason;
            }
          })}>Save manual edit</button>
          <button className="button secondary" onClick={() => void run(compareFile)}>Compare current file</button>
          <button className="button secondary" onClick={downloadDraft}>Download manual draft</button>
          {comparison && <section aria-label="Reconcile repository file">
            <h4>Reconcile before saving</h4>
            <p>Your editable draft above is unchanged. Incorporate any external changes you want to keep.
              Accepting this baseline does not save; another external change will still reject the next save.</p>
            <details><summary>Original edit baseline</summary><pre className="repository-text-preview">{comparison.original.content}</pre></details>
            <h5>Current file on disk</h5>
            {comparison.current.kind === 'text'
              ? <pre className="repository-text-preview" tabIndex={0}>{comparison.current.content}</pre>
              : <p>The file is no longer editable text. Download your draft before leaving this editor.</p>}
            <p>Compared SHA-256 <code>{comparison.current.sha256}</code></p>
            <button className="button secondary" disabled={!comparison.current.editable || (!staleEdit && preview.sha256 === comparison.current.sha256)} onClick={() => {
              if (window.confirm('Use the compared file as the save baseline? Your draft is retained. Reconcile the displayed external text before saving.')) {
                setPreview(comparison.current); setStaleEdit(false); setError('');
                setNotice('Compared baseline accepted. Your draft is unchanged; save explicitly after reconciling.');
              }
            }}>Use compared file as save baseline</button>
          </section>}
          <button className="button secondary" onClick={() => {
            if (discard()) {
              setDraft(preview.content || ''); setEditing(false); setComparison(null); setStaleEdit(false);
            }
          }}>Discard draft</button>
        </>}
        <h4>Link this artifact to an experiment</h4>
        <p>A receipt records the selected file hash, not an executed experiment or verified result.</p>
        <label>Experiment<select value={experiment} onChange={(event) => setExperiment(event.target.value)}>
          <option value="">Select existing EXP ID</option>
          {experiments.map((item) => <option key={item.id} value={item.id}>{item.id}: {item.experiment}</option>)}
        </select></label>
        <label>Producing revision (optional; user-supplied, unverified)
          <input value={revision} onChange={(event) => setRevision(event.target.value)} placeholder="Full commit hash, if known" /></label>
        <label>Artifact note<input value={note} maxLength={2000} onChange={(event) => setNote(event.target.value)} /></label>
        <button className="button" disabled={dirty || !experiment} onClick={() => void run(async () => {
          update(await api.artifact(trackId, selected, preview, experiment, revision, note));
          setNote(''); setRevision('');
        })}>Link artifact receipt</button>
      </section>}
      <h3>Experiment artifact receipts</h3>
      <p>Status reflects the last explicit refresh. Receipts do not copy or ingest artifact contents.</p>
      <ul className="repository-receipts">{(linked.artifacts || []).map((artifact) => <li key={artifact.id}>
        <strong>{artifact.experiment_id}</strong>
        <button className="button secondary" onClick={() => { if (discard()) void run(() => openFile(artifact.path)); }}>
          Open artifact: {artifact.path}
        </button>
        <span>{artifact.status}</span>
        <p>Linked {artifact.linked_at} · recorded hash <code>{artifact.sha256}</code></p>
        <p>Observed revision: {artifact.observed_revision || 'Unknown'}.
          Producing revision: {artifact.producing_revision || 'Unknown'}. {artifact.provenance}</p>
        {artifact.note && <p>{artifact.note}</p>}
        <button className="button secondary" onClick={() => {
          if (discard()) void run(async () => {
            const baseline = await tracksApi.section(trackId, 'results');
            setPromotion({ artifact, baseline }); setObservation('');
          });
        }}>Promote {artifact.experiment_id} receipt to Results</button>
      </li>)}</ul>
      {promotion && <section className="repository-promotion" aria-label="Promote artifact observation">
        <h3>Save an observation in Results</h3>
        <p>Append the receipt for {promotion.artifact.experiment_id}: {promotion.artifact.path}.
          This explicitly adds its hash, provenance and your observation to the track Results.
          No report contents are copied, no claims are generated, and no provider is called.</p>
        <label>Your observation (optional; unverified)<textarea rows={4} maxLength={4000}
          value={observation} onChange={(event) => setObservation(event.target.value)} /></label>
        <details><summary>Existing Results baseline</summary><pre className="repository-text-preview">{promotion.baseline.content}</pre></details>
        <div className="repository-actions">
          <button className="button" onClick={() => void run(async () => {
            const result = await api.promote(trackId, selected, promotion.artifact.id, promotion.baseline.sha256, observation);
            setPromotion(null); setObservation(''); setNotice('Artifact receipt and your observation saved in Results.');
            onResultsSaved?.(result);
          })}>Append receipt and observation to Results</button>
          <button className="button secondary" onClick={() => void run(async () => {
            const baseline = await tracksApi.section(trackId, 'results');
            setPromotion({ ...promotion, baseline });
          })}>Reload Results baseline</button>
          <button className="button secondary" onClick={() => {
            if (!observation || window.confirm('Discard this unsaved Results observation?')) setPromotion(null);
          }}>Cancel promotion</button>
        </div>
      </section>}
    </fieldset>}
  </section>;
}
