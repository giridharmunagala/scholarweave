import { useEffect, useRef, useState } from 'react';
import { ApiError, request } from '../../api/client';
import type { components } from '../../api/schema.generated';
import { Link, useLocation, useNavigate, useNavigationGuard } from '../../app/router';
import { MarkdownViewer } from '../../shared/components/MarkdownViewer';
import { ErrorNotice, Loading, PageHeader } from '../../shared/components/Ui';
import { SECTIONS, tracksApi, trackAssistUrl, type ImportPreview, type SectionContent, type Track, type TrackRevision, type TrackSection } from './api';
import './tracks.css';

type Paper = components['schemas']['DocumentSummaryResponse'];
type Draft = { base: SectionContent; content: string };
const label = (section: string) => section.charAt(0).toUpperCase() + section.slice(1);
const date = (value: string) => new Date(value).toLocaleString();
const draftKey = (id: string, section: TrackSection) => `${id}:${section}`;

export default function TracksPage() {
  const { searchParams } = useLocation();
  const navigate = useNavigate();
  const selectedId = searchParams.get('id');
  const [tracks, setTracks] = useState<Track[]>([]);
  const [track, setTrack] = useState<Track | null>(null);
  const [papers, setPapers] = useState<Paper[]>([]);
  const [title, setTitle] = useState('');
  const [explorationName, setExplorationName] = useState('');
  const [section, setSection] = useState<TrackSection>('problem');
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [editing, setEditing] = useState(false);
  const [history, setHistory] = useState<TrackRevision[]>([]);
  const [historical, setHistorical] = useState<SectionContent | null>(null);
  const [latest, setLatest] = useState<SectionContent | null>(null);
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [mergeDrafts, setMergeDrafts] = useState<Record<string, string>>({});
  const [importMode, setImportMode] = useState<'merge' | 'replace'>('merge');
  const [replaceConfirmed, setReplaceConfirmed] = useState(false);
  const [paperId, setPaperId] = useState('');
  const [skill, setSkill] = useState('');
  const [skills, setSkills] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [pendingTrack, setPendingTrack] = useState<string | null>(null);
  const [opening, setOpening] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState('');
  const fileInput = useRef<HTMLInputElement>(null);
  const key = selectedId ? draftKey(selectedId, section) : '';
  const draft = drafts[key];
  const dirty = Object.values(drafts).some((item) => item.content !== item.base.content);
  const activeDirty = !!draft && draft.content !== draft.base.content;
  useNavigationGuard(dirty || !!preview || busy, 'Navigate away? Section drafts remain while Research Tracks stays open, but leaving this page discards unsaved drafts and import previews.', !busy);

  const updateTrack = (value: Track) => {
    setTrack(value);
    setTracks((items) => items.some((item) => item.id === value.id)
      ? items.map((item) => item.id === value.id ? value : item) : [...items, value]);
  };
  useEffect(() => {
    let active = true;
    tracksApi.list().then((items) => { if (active) setTracks(items); }).catch(setError);
    request<Paper[]>('/documents').then((items) => { if (active) setPapers(items); }).catch(setError);
    request<components['schemas']['SkillResponse'][]>('/skills')
      .then((items) => { if (active) setSkills(items.map((item) => item.name)); }).catch(setError);
    return () => { active = false; };
  }, []);
  useEffect(() => {
    let active = true;
    setTrack(null);
    setPreview(null);
    setError(null);
    if (selectedId) tracksApi.get(selectedId).then((value) => { if (active) updateTrack(value); }).catch((reason) => { if (active) setError(reason); });
    return () => { active = false; };
  }, [selectedId]);
  useEffect(() => {
    let active = true;
    setLatest(null);
    setHistorical(null);
    setHistory([]);
    setNotice('');
    setError(null);
    setOpening(false);
    if (!selectedId) return;
    if (!drafts[key] || drafts[key].content === drafts[key].base.content) {
      setOpening(true);
      tracksApi.section(selectedId, section).then((value) => {
        if (active) setDrafts((items) => ({ ...items, [key]: { base: value, content: value.content } }));
      }).catch((reason) => { if (active) setError(reason); }).finally(() => { if (active) setOpening(false); });
    }
    return () => { active = false; };
  }, [selectedId, section]);

  const perform = async (action: () => Promise<void>) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try { await action(); } catch (reason) { setError(reason); } finally { setBusy(false); }
  };
  const create = () => perform(async () => {
    const value = await tracksApi.create(title.trim());
    setTracks((items) => [...items, value]);
    setTitle('');
    // Navigation is deferred until the mutation guard has been released.
    setPendingTrack(value.id);
  });
  const createExploration = () => perform(async () => {
    if (!track) return;
    const name = explorationName.trim().replace(/\.md$/, '');
    if (!/^[a-z0-9][a-z0-9_-]{0,79}$/.test(name)) {
      throw new Error('Use a filename of up to 80 lowercase letters, digits, hyphens or underscores.');
    }
    const document = `exploration/${name}`;
    const value = await tracksApi.save(track.id, document, `# ${name.replace(/[-_]/g, ' ')}\n\n`, null);
    setDrafts((items) => ({ ...items, [draftKey(track.id, document)]: { base: value, content: value.content } }));
    updateTrack(await tracksApi.get(track.id));
    setSection(document);
    setEditing(true);
    setExplorationName('');
  });
  useEffect(() => {
    if (!pendingTrack || busy) return;
    navigate(`/tracks?id=${encodeURIComponent(pendingTrack)}`);
    setPendingTrack(null);
  }, [pendingTrack, busy, navigate]);
  const save = () => perform(async () => {
    if (!selectedId || !draft) return;
    try {
      const value = await tracksApi.save(selectedId, section, draft.content, draft.base.sha256);
      setDrafts((items) => ({ ...items, [key]: { base: value, content: value.content } }));
      setLatest(null);
      setNotice('Saved locally. Previous content remains in version history.');
      updateTrack(await tracksApi.get(selectedId));
    } catch (reason) {
      setNotice(reason instanceof ApiError && reason.status === 409
        ? 'Conflict: the section changed. Your draft is retained. Load the latest version to compare and reconcile before saving.'
        : 'Save failed. Your draft is retained.');
      throw reason;
    }
  });
  const setContent = (content: string) => setDrafts((items) => ({ ...items, [key]: { ...items[key], content } }));
  const importFile = (file: File) => perform(async () => {
    if (!selectedId) return;
    if (!/\.(md|markdown|zip)$/i.test(file.name)) throw new Error('Choose an individual .md/.markdown file or a whole-track .zip archive.');
    if (Object.entries(drafts).some(([name, item]) => name.startsWith(`${selectedId}:`) && item.content !== item.base.content)) {
      throw new Error('Save or discard this track’s section drafts before previewing an import. No files were changed.');
    }
    const value = file.name.toLowerCase().endsWith('.zip')
      ? await tracksApi.previewZip(selectedId, file)
      : await tracksApi.preview(selectedId, section, await file.text());
    setPreview(value);
    setMergeDrafts(Object.fromEntries(value.sections.map((item) => [item.section,
      item.current_content === item.incoming_content ? item.current_content : `${item.current_content}\n\n${item.incoming_content}`,
    ])));
    setImportMode('merge');
    setReplaceConfirmed(false);
  });
  const applyImport = () => perform(async () => {
    if (!selectedId || !preview || (importMode === 'replace' && !replaceConfirmed)) return;
    const value = await tracksApi.applyImport(selectedId, preview, importMode, importMode === 'merge' ? mergeDrafts : {});
    const refreshed = await Promise.all(preview.sections.map((item) => tracksApi.section(selectedId, item.section)));
    setDrafts((items) => {
      const next = { ...items };
      for (const item of refreshed) next[draftKey(selectedId, item.section)] = { base: item, content: item.content };
      return next;
    });
    updateTrack(value);
    setPreview(null);
    setNotice('Import saved locally. Earlier content is retained in version history.');
  });

  return <div className="tracks-page">
    <PageHeader title="Research Tracks" description="Persistent research workspaces. Markdown is the source of truth; chat assistance never silently saves changes." />
    <ErrorNotice error={error} />
    <div className="tracks-layout">
      <aside className="tracks-list" aria-label="Research tracks">
        <form onSubmit={(event) => { event.preventDefault(); void create(); }}>
          <label htmlFor="track-title">New track title</label>
          <input id="track-title" value={title} maxLength={300} disabled={busy} onChange={(event) => setTitle(event.target.value)} />
          <button className="button" type="submit" disabled={busy || !title.trim()}>Create track</button>
        </form>
        {!tracks.length ? <p>No research tracks yet.</p> : null}
        {tracks.map((item) => <Link key={item.id} to={`/tracks?id=${encodeURIComponent(item.id)}`} className={`track-link${selectedId === item.id ? ' active' : ''}`}>
          <strong>{item.title}</strong><small>Updated {date(item.updated_at)}</small>
          {Object.entries(drafts).some(([name, value]) => name.startsWith(`${item.id}:`) && value.content !== value.base.content) ? <small>Unsaved draft</small> : null}
        </Link>)}
        {dirty ? <p role="status">Unsaved drafts are retained when switching sections. Leaving requires confirmation.</p> : null}
      </aside>
      <main className="track-detail">
        {!selectedId ? <p>Select or create a track to begin.</p> : !track ? <Loading label="Loading track…" /> : <>
          <header className="track-overview">
            <h2>{track.title}</h2><code>{track.folder_path}</code>
            <p>Updated {date(track.updated_at)} · {track.paper_ids.length} linked papers · {track.conversation_ids.length} linked chats</p>
            <div className="button-row">
              <button className="button secondary small" disabled={busy} onClick={() => void perform(() => tracksApi.download(track.id))}>Export track ZIP</button>
              <button className="button secondary small" disabled={busy || !!preview} onClick={() => fileInput.current?.click()}>Import Markdown or ZIP</button>
              <input ref={fileInput} hidden type="file" accept=".md,.markdown,.zip,text/markdown,application/zip" aria-label="Import track file" onChange={(event) => {
                const file = event.target.files?.[0];
                event.target.value = '';
                if (file) void importFile(file);
              }} />
            </div>
            {track.warnings.length ? <div className="track-warnings" role="status"><strong>Track warnings</strong><ul>{track.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul></div> : null}
            <details><summary>Recent section updates</summary><ul>{track.sections.map((item) => <li key={item.section}>{label(item.section)} · {date(item.modified_at)}</li>)}</ul></details>
            <details open>
              <summary>Exploration · {(track.exploration ?? []).length} files</summary>
              <p>Working ideas, questions, hypotheses, search leads and dead ends. These are exploratory notes, not established results. You and the assistant can create multiple Markdown files.</p>
              <code>{track.folder_path}/exploration/</code>
              <ul>{(track.exploration ?? []).map((item) => <li key={item.section}>
                <button id={`track-tab-${item.section}`} className="button secondary small" disabled={busy || !!preview}
                  aria-pressed={section === item.section} onClick={() => setSection(item.section)}>
                  {item.section.slice('exploration/'.length)}.md
                </button>
                {drafts[draftKey(track.id, item.section)]?.content !== drafts[draftKey(track.id, item.section)]?.base.content ? ' Unsaved draft' : ''}
              </li>)}</ul>
              <form onSubmit={(event) => { event.preventDefault(); void createExploration(); }}>
                <label>New exploration filename
                  <input aria-label="New exploration filename" placeholder="retrieval-ideas.md" value={explorationName}
                    maxLength={83} disabled={busy || !!preview} onChange={(event) => setExplorationName(event.target.value)} />
                </label>
                <button className="button secondary small" type="submit" disabled={busy || !!preview || !explorationName.trim()}>Create exploration file</button>
              </form>
            </details>
            <details><summary>Linked library papers</summary>
              <ul>{track.paper_ids.map((id) => <li key={id}><Link to={`/library?document=${encodeURIComponent(id)}`}>{papers.find((item) => item.id === id)?.title ?? id}</Link>
                <button className="button danger small" disabled={busy} onClick={() => void perform(async () => { updateTrack(await tracksApi.paper(track.id, id, false)); })}>Remove paper</button></li>)}</ul>
              <label>Library paper <select aria-label="Library paper" value={paperId} disabled={busy} onChange={(event) => setPaperId(event.target.value)}>
                <option value="">Choose an existing paper</option>{papers.filter((item) => !track.paper_ids.includes(item.id)).map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}
              </select></label>
              <button className="button secondary small" disabled={busy || !paperId} onClick={() => void perform(async () => { updateTrack(await tracksApi.paper(track.id, paperId, true)); setPaperId(''); })}>Link paper</button>
              <p>Links reuse library IDs. PDFs are not copied.</p>
            </details>
            <details><summary>Linked chats</summary><ul>{track.conversation_ids.map((id) => <li key={id}><Link to={`/?conversation=${encodeURIComponent(id)}`}>Open chat {id}</Link></li>)}</ul></details>
          </header>
          <div role="tablist" aria-label="Track sections" className="track-tabs">
            {SECTIONS.map((item) => <button className="button secondary" key={item} id={`track-tab-${item}`} role="tab" aria-selected={item === section} aria-controls="track-section-panel" tabIndex={item === section ? 0 : -1} disabled={busy || !!preview} onClick={() => setSection(item)} onKeyDown={(event) => {
              const index = SECTIONS.indexOf(item);
              const next = event.key === 'ArrowRight' ? SECTIONS[(index + 1) % SECTIONS.length]
                : event.key === 'ArrowLeft' ? SECTIONS[(index + SECTIONS.length - 1) % SECTIONS.length]
                  : event.key === 'Home' ? SECTIONS[0] : event.key === 'End' ? SECTIONS[SECTIONS.length - 1] : null;
              if (next) { event.preventDefault(); setSection(next); document.getElementById(`track-tab-${next}`)?.focus(); }
            }}>
              {label(item)}{drafts[draftKey(track.id, item)]?.content !== drafts[draftKey(track.id, item)]?.base.content ? ' •' : ''}
            </button>)}
          </div>
          <section id="track-section-panel" role="tabpanel" aria-labelledby={`track-tab-${section}`}>
            <div className="button-row track-actions">
              <button className="button secondary small" disabled={busy || opening || !draft || !!preview} onClick={() => setEditing((value) => !value)}>{editing ? 'Preview' : 'Edit'}</button>
              <button className="button small" disabled={busy || !activeDirty || !!preview} onClick={() => void save()}>Save section</button>
              <button className="button danger small" disabled={busy || !activeDirty || !!preview} onClick={() => {
                if (window.confirm('Discard this section’s unsaved draft?')) setContent(draft.base.content);
              }}>Discard draft</button>
              <button className="button secondary small" disabled={busy} onClick={() => void perform(() => tracksApi.download(track.id, section))}>Export section Markdown</button>
              <button className="button secondary small" disabled={busy || opening} onClick={() => void perform(async () => {
                const versions = await tracksApi.history(track.id, section);
                setHistory(versions);
                if (!versions.length) setNotice('No earlier versions yet. Explicit saves and imports preserve earlier content here.');
              })}>Version history</button>
            </div>
            <div className="track-assist">
              <label>Optional skill <select aria-label="Track assistance skill" value={skill} onChange={(event) => setSkill(event.target.value)}>
                <option value="">No specific skill</option>{skills.map((name) => <option key={name} value={name}>{name}</option>)}
              </select></label>
              <Link className="button secondary small" to={trackAssistUrl(track, section, skill)}>Discuss {label(section)} in new chat</Link>
              <small>Prepares an unsent prompt using your normal chat model. References saved content, not unsaved drafts.</small>
            </div>
            {notice ? <p role="status">{notice}</p> : null}
            {opening ? <Loading label="Loading section…" /> : draft ? <>
              <p className="track-revision">{draft.base.path} · {activeDirty ? 'Unsaved draft' : 'Saved'} · {date(draft.base.modified_at)}</p>
              {editing ? <textarea aria-label={`${label(section)} Markdown`} value={draft.content} disabled={busy || !!preview} onChange={(event) => setContent(event.target.value)} />
                : <MarkdownViewer content={draft.content} />}
              <button className="button secondary small" disabled={busy || !!preview} onClick={() => void perform(async () => { setLatest(await tracksApi.section(track.id, section)); })}>Compare latest saved version</button>
              {latest ? <div className="track-comparison"><h3>Latest saved content</h3><pre>{latest.content}</pre><h3>Your draft</h3><pre>{draft.content}</pre>
                <button className="button secondary small" disabled={busy} onClick={() => {
                  if (window.confirm('Use the displayed latest revision as the save base? Your draft stays unchanged. Review and reconcile all differences before saving.')) {
                    setDrafts((items) => ({ ...items, [key]: { base: latest, content: items[key].content } }));
                    setLatest(null);
                    setNotice('Latest revision loaded as save base. Review your draft before saving.');
                  }
                }}>Use latest revision as save base</button></div> : null}
              {history.length ? <div className="track-history"><h3>Version history</h3>{history.map((item) => <button className="button secondary small" key={item.revision} disabled={busy} onClick={() => void perform(async () => {
                setHistorical(await tracksApi.revision(track.id, section, item.revision));
              })}>View version {item.revision} · {date(item.created_at)}</button>)}</div> : null}
              {historical ? <div><h3>Prior version preview</h3><MarkdownViewer content={historical.content} />
                <button className="button secondary small" disabled={busy || !!preview} onClick={() => {
                  if (!activeDirty || window.confirm('Replace your unsaved draft with this prior version?')) {
                    setContent(historical.content); setEditing(true); setHistorical(null);
                    setNotice('Prior content recovered into an unsaved draft. Save explicitly to restore it.');
                  }
                }}>Recover into draft</button></div> : null}
            </> : null}
            {section === 'experiments' ? <section className="track-experiments" aria-label="Saved experiment status">
              <h3>Experiment status from saved Markdown</h3><p>Read-only projection. Edit the Markdown table to change statuses; unsaved drafts are not counted.</p>
              <div className="button-row">{Object.entries(track.status_counts).map(([status, count]) => <span key={status}>{status}: {count}</span>)}</div>
              <div className="track-table-scroll"><table><thead><tr><th>ID</th><th>Experiment</th><th>Status</th><th>Success criterion</th><th>Evidence</th></tr></thead>
                <tbody>{track.experiments.map((item) => <tr key={item.id}><td>{item.id}</td><td>{item.experiment}</td><td>{item.status}</td><td>{item.success_criterion}</td><td>{item.evidence}</td></tr>)}</tbody>
              </table></div>
            </section> : null}
          </section>
          {preview ? <section className="track-import" aria-label="Import preview">
            <h3>Import preview — no changes saved yet</h3>
            {preview.warnings.length ? <ul className="track-warnings">{preview.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul> : null}
            <p>{preview.paper_ids.length} incoming paper links. Existing local library papers are reused.</p>
            {preview.sections.map((item) => <div key={item.section}>
              <h4>{label(item.section)}</h4>
              <details open><summary>Current / incoming diff</summary><pre>{item.diff || 'No differences'}</pre></details>
              <div className="track-diff"><div><h5>Current content</h5><pre>{item.current_content}</pre></div><div><h5>Incoming content</h5><pre>{item.incoming_content}</pre></div></div>
              {importMode === 'merge' ? <label>Merged {label(item.section)} Markdown<textarea aria-label={`Merged ${label(item.section)} Markdown`} value={mergeDrafts[item.section] ?? ''} disabled={busy} onChange={(event) => setMergeDrafts((items) => ({ ...items, [item.section]: event.target.value }))} /></label> : null}
            </div>)}
            <label>Import mode <select aria-label="Import mode" value={importMode} disabled={busy} onChange={(event) => { setImportMode(event.target.value as 'merge' | 'replace'); setReplaceConfirmed(false); }}>
              <option value="merge">Merge (review combined content)</option><option value="replace">Replace affected sections</option>
            </select></label>
            {importMode === 'replace' ? <label><input type="checkbox" checked={replaceConfirmed} disabled={busy} onChange={(event) => setReplaceConfirmed(event.target.checked)} />I confirm replacing the displayed sections. Earlier content remains in history.</label> : null}
            <div className="button-row"><button className="button" disabled={busy || (importMode === 'replace' && !replaceConfirmed)} onClick={() => void applyImport()}>Apply import</button>
              <button className="button secondary" disabled={busy} onClick={() => { if (window.confirm('Discard this import preview and merged drafts?')) setPreview(null); }}>Cancel import</button></div>
          </section> : null}
        </>}
      </main>
    </div>
  </div>;
}
