import { useEffect, useRef, useState } from 'react';
import { ApiError, request } from '../../api/client';
import type { components } from '../../api/schema.generated';
import { Link, useLocation, useNavigate, useNavigationGuard } from '../../app/router';
import { MarkdownViewer } from '../../shared/components/MarkdownViewer';
import { Icon } from '../../shared/components/Icons';
import { EmptyState, ErrorNotice, Loading, PageHeader } from '../../shared/components/Ui';
import { SECTIONS, tracksApi, trackAssistUrl, type ImportPreview, type SectionContent, type Track, type TrackRevision, type TrackSection } from './api';
import './tracks.css';

type Paper = components['schemas']['DocumentSummaryResponse'];
type Draft = { base: SectionContent; content: string };
const label = (section: string) => section.charAt(0).toUpperCase() + section.slice(1);
const date = (value: string) => new Date(value).toLocaleString();
const shortDate = (value: string) => new Date(value).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
const draftKey = (id: string, section: TrackSection) => `${id}:${section}`;

export default function TracksPage() {
  const { searchParams, search } = useLocation();
  const navigate = useNavigate();
  const selectedId = searchParams.get('id');
  const linkedSection = searchParams.get('section');
  const linkedReview = searchParams.get('review');
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
    if (linkedSection && /^(problem|literature|experiments|results|exploration\/[a-z0-9][a-z0-9_-]{0,79})$/.test(linkedSection)) {
      setSection(linkedSection);
    } else if (linkedReview) {
      setSection('literature');
    }
  }, [selectedId, linkedSection, linkedReview]);
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
  }, [selectedId, search]);
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
        if (active) setDrafts((items) => {
          const current = items[key];
          if (current && current.content !== current.base.content) return items;
          return { ...items, [key]: { base: value, content: value.content } };
        });
      }).catch((reason) => { if (active) setError(reason); }).finally(() => { if (active) setOpening(false); });
    }
    return () => { active = false; };
  }, [selectedId, section, search]);

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

  const sectionDirty = (id: string, name: TrackSection) => {
    const item = drafts[draftKey(id, name)];
    return !!item && item.content !== item.base.content;
  };
  const exploration = track?.exploration ?? [];
  const isExploration = section.startsWith('exploration/');
  const documentName = isExploration ? `${section.slice('exploration/'.length)}.md` : label(section);
  const importPanel = useRef<HTMLElement>(null);
  const previewOpen = !!preview;
  useEffect(() => {
    if (previewOpen) importPanel.current?.scrollIntoView?.({ behavior: 'smooth', block: 'start' });
  }, [previewOpen]);

  return <div className="page page-wide tracks-page">
    <PageHeader eyebrow="Research" title="Research Tracks" description="Long-running investigations kept as Markdown in your workspace. Chat assistance never saves changes without you." />
    {error ? <ErrorNotice error={error} /> : null}
    <div className="tracks-layout">
      <aside className="tracks-sidebar" aria-label="Research tracks">
        <form className="track-create" onSubmit={(event) => { event.preventDefault(); void create(); }}>
          <label htmlFor="track-title">New track</label>
          <input id="track-title" placeholder="Working title" value={title} maxLength={300} disabled={busy} onChange={(event) => setTitle(event.target.value)} />
          <button className="button small" type="submit" disabled={busy || !title.trim()}><Icon name="plus" size={13} />Create track</button>
        </form>
        <div className="track-list">
          <span className="track-list-label">Tracks{tracks.length ? ` · ${tracks.length}` : ''}</span>
          {!tracks.length ? <p className="track-list-empty">No research tracks yet. Name one above to begin.</p> : null}
          {tracks.map((item) => <Link key={item.id} to={`/tracks?id=${encodeURIComponent(item.id)}`} className={`track-item${selectedId === item.id ? ' active' : ''}`}>
            <span className="track-item-title">{item.title}</span>
            <span className="track-item-meta">Updated {shortDate(item.updated_at)}</span>
            {Object.entries(drafts).some(([name, value]) => name.startsWith(`${item.id}:`) && value.content !== value.base.content) ? <span className="track-item-draft">Unsaved draft</span> : null}
          </Link>)}
        </div>
        {dirty ? <p className="track-list-note" role="status">Unsaved drafts are kept while you switch sections. Leaving this page asks for confirmation.</p> : null}
      </aside>

      <main className="track-main">
        {!selectedId ? <EmptyState icon="workspace" title="Select a research track"
          description="A track keeps the problem statement, literature, experiments and results of one investigation together as Markdown files. Create a track or open an existing one." />
          : !track ? <Loading label="Loading track…" /> : <>
            <header className="track-header">
              <div className="track-heading">
                <h2>{track.title}</h2>
                <div className="track-meta">
                  <span><Icon name="clock" size={13} />Updated {date(track.updated_at)}</span>
                  <span><Icon name="papers" size={13} />{track.paper_ids.length} linked papers</span>
                  <span><Icon name="chat" size={13} />{track.conversation_ids.length} linked chats</span>
                </div>
                <code className="track-path" title="Workspace folder">{track.folder_path}</code>
              </div>
              <div className="button-row">
                <Link className="button secondary small" to={`/deep-work?track=${encodeURIComponent(track.id)}`}>Deep Work</Link>
                <button className="button secondary small" disabled={busy} onClick={() => void perform(() => tracksApi.download(track.id))}><Icon name="download" size={13} />Export track ZIP</button>
                <button className="button secondary small" disabled={busy || !!preview} onClick={() => fileInput.current?.click()}><Icon name="upload" size={13} />Import Markdown or ZIP</button>
                <input ref={fileInput} hidden type="file" accept=".md,.markdown,.zip,text/markdown,application/zip" aria-label="Import track file" onChange={(event) => {
                  const file = event.target.files?.[0];
                  event.target.value = '';
                  if (file) void importFile(file);
                }} />
              </div>
            </header>
            {track.warnings.length ? <div className="notice track-warnings" role="status"><div><strong>Track warnings</strong>
              <ul>{track.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul></div></div> : null}

            <div className="track-workspace">
              <section className="track-editor" aria-label="Track document">
                <div role="tablist" aria-label="Track sections" className="track-tabs">
                  {SECTIONS.map((item) => <button type="button" className="track-tab" key={item} id={`track-tab-${item}`} role="tab" aria-selected={item === section} aria-controls="track-section-panel" tabIndex={item === section ? 0 : -1} disabled={busy || !!preview} onClick={() => setSection(item)} onKeyDown={(event) => {
                    const index = SECTIONS.indexOf(item);
                    const next = event.key === 'ArrowRight' ? SECTIONS[(index + 1) % SECTIONS.length]
                      : event.key === 'ArrowLeft' ? SECTIONS[(index + SECTIONS.length - 1) % SECTIONS.length]
                        : event.key === 'Home' ? SECTIONS[0] : event.key === 'End' ? SECTIONS[SECTIONS.length - 1] : null;
                    if (next) { event.preventDefault(); setSection(next); document.getElementById(`track-tab-${next}`)?.focus(); }
                  }}>
                    {label(item)}{sectionDirty(track.id, item) ? ' •' : ''}
                  </button>)}
                </div>
                <section id="track-section-panel" className="track-panel" role="tabpanel" aria-labelledby={`track-tab-${section}`}>
                  <div className="track-toolbar">
                    <div className="track-doc">
                      <strong>{isExploration ? <><span className="track-doc-kind">Exploration /</span> {documentName}</> : documentName}</strong>
                      {draft && !opening ? <small className="track-revision">
                        <span className={`track-save-state${activeDirty ? ' dirty' : ''}`}>{activeDirty ? 'Unsaved draft' : 'Saved'}</span>
                        {' · '}{draft.base.path} · {date(draft.base.modified_at)}
                      </small> : null}
                    </div>
                    <div className="button-row">
                      <div className="segmented" aria-label="Section view">
                        <button type="button" aria-pressed={!editing} disabled={busy || opening || !draft || !!preview} onClick={() => setEditing(false)}>Preview</button>
                        <button type="button" aria-pressed={editing} disabled={busy || opening || !draft || !!preview} onClick={() => setEditing(true)}>Edit</button>
                      </div>
                      <button className="button ghost small" disabled={busy || !activeDirty || !!preview} onClick={() => {
                        if (window.confirm('Discard this section’s unsaved draft?')) setContent(draft.base.content);
                      }}>Discard draft</button>
                      <button className="button small" disabled={busy || !activeDirty || !!preview} onClick={() => void save()}><Icon name="save" size={13} />Save section</button>
                    </div>
                  </div>
                  {notice ? <p className="notice info track-notice" role="status">{notice}</p> : null}
                  {opening ? <Loading label="Loading section…" /> : draft ? <>
                    <div className={`track-document${editing ? ' editing' : ''}`}>
                      {editing ? <textarea aria-label={`${label(section)} Markdown`} className="track-textarea" spellCheck value={draft.content} disabled={busy || !!preview} onChange={(event) => setContent(event.target.value)} />
                        : draft.content.trim() ? <MarkdownViewer content={draft.content} workspacePath={draft.base.path} onNavigate={navigate} />
                          : <p className="track-document-empty">This section is empty. Choose Edit to start writing.</p>}
                    </div>
                    <div className="track-doc-footer button-row">
                      <button className="button ghost small" disabled={busy || !!preview} onClick={() => void perform(async () => { setLatest(await tracksApi.section(track.id, section)); })}>Compare latest saved version</button>
                      <button className="button ghost small" disabled={busy || opening} onClick={() => void perform(async () => {
                        const versions = await tracksApi.history(track.id, section);
                        setHistory(versions);
                        if (!versions.length) setNotice('No earlier versions yet. Explicit saves and imports preserve earlier content here.');
                      })}><Icon name="clock" size={13} />Version history</button>
                      <span className="row-spacer" />
                      <button className="button ghost small" disabled={busy} onClick={() => void perform(() => tracksApi.download(track.id, section))}><Icon name="download" size={13} />Export section Markdown</button>
                    </div>
                    {latest ? <div className="track-block track-comparison">
                      <div className="track-block-header"><h3>Compare with latest saved version</h3>
                        <button className="button secondary small" disabled={busy} onClick={() => {
                          if (window.confirm('Use the displayed latest revision as the save base? Your draft stays unchanged. Review and reconcile all differences before saving.')) {
                            setDrafts((items) => ({ ...items, [key]: { base: latest, content: items[key].content } }));
                            setLatest(null);
                            setNotice('Latest revision loaded as save base. Review your draft before saving.');
                          }
                        }}>Use latest revision as save base</button></div>
                      <div className="track-diff">
                        <div><h4>Latest saved content</h4><pre>{latest.content}</pre></div>
                        <div><h4>Your draft</h4><pre>{draft.content}</pre></div>
                      </div>
                    </div> : null}
                    {history.length ? <div className="track-block track-history"><div className="track-block-header"><h3>Version history</h3></div>
                      <div className="track-history-list">{history.map((item) => <button className="track-history-item" key={item.revision} disabled={busy} onClick={() => void perform(async () => {
                        setHistorical(await tracksApi.revision(track.id, section, item.revision));
                      })}>View version {item.revision} · {date(item.created_at)}</button>)}</div></div> : null}
                    {historical ? <div className="track-block track-historical">
                      <div className="track-block-header"><h3>Prior version preview</h3>
                        <button className="button secondary small" disabled={busy || !!preview} onClick={() => {
                          if (!activeDirty || window.confirm('Replace your unsaved draft with this prior version?')) {
                            setContent(historical.content); setEditing(true); setHistorical(null);
                            setNotice('Prior content recovered into an unsaved draft. Save explicitly to restore it.');
                          }
                        }}>Recover into draft</button></div>
                      <div className="track-historical-body"><MarkdownViewer content={historical.content} workspacePath={historical.path} onNavigate={navigate} /></div>
                    </div> : null}
                  </> : null}
                  {section === 'experiments' ? <section className="track-block track-experiments" aria-label="Saved experiment status">
                    <div className="track-block-header"><div><h3>Experiment status</h3>
                      <p>Read from the saved Markdown table. Edit the table to change statuses; unsaved drafts are not counted.</p></div></div>
                    <div className="track-status-counts">{Object.entries(track.status_counts).map(([status, count]) => <span className="tag" key={status}>{status}: {count}</span>)}</div>
                    {track.experiments.length ? <div className="table-wrap"><table><thead><tr><th>ID</th><th>Experiment</th><th>Status</th><th>Success criterion</th><th>Evidence</th></tr></thead>
                      <tbody>{track.experiments.map((item) => <tr key={item.id}><td className="mono">{item.id}</td><td>{item.experiment}</td><td>{item.status}</td><td>{item.success_criterion}</td><td>{item.evidence}</td></tr>)}</tbody>
                    </table></div> : <p className="muted">No experiments in the saved table yet.</p>}
                  </section> : null}
                </section>
              </section>

              <aside className="track-context" aria-label="Track context">
                <section className="track-card">
                  <header><h3>Exploration · {exploration.length} files</h3></header>
                  <p className="track-card-hint">Working ideas, questions, search leads and dead ends — exploratory notes, not established results.</p>
                  {exploration.length ? <ul className="track-files">{exploration.map((item) => <li key={item.section}>
                    <button type="button" id={`track-tab-${item.section}`} className="track-file" disabled={busy || !!preview}
                      aria-pressed={section === item.section} onClick={() => setSection(item.section)}><Icon name="file" size={13} />{item.section.slice('exploration/'.length)}.md</button>
                    {sectionDirty(track.id, item.section) ? <span className="track-file-dirty" title="Unsaved draft">Unsaved</span> : null}
                  </li>)}</ul> : null}
                  <form className="track-inline-form" onSubmit={(event) => { event.preventDefault(); void createExploration(); }}>
                    <input aria-label="New exploration filename" placeholder="retrieval-ideas.md" value={explorationName}
                      maxLength={83} disabled={busy || !!preview} onChange={(event) => setExplorationName(event.target.value)} />
                    <button className="button secondary small" type="submit" disabled={busy || !!preview || !explorationName.trim()}>Create exploration file</button>
                  </form>
                </section>

                <section className="track-card">
                  <header><h3>Linked papers</h3><span className="tag">{track.paper_ids.length}</span></header>
                  {track.paper_ids.length ? <ul className="track-links">{track.paper_ids.map((id) => <li key={id}>
                    <Link to={`/library?document=${encodeURIComponent(id)}`}><Icon name="papers" size={13} /><span>{papers.find((item) => item.id === id)?.title ?? id}</span></Link>
                    <button className="icon-button small danger" title="Remove paper" disabled={busy} onClick={() => void perform(async () => { updateTrack(await tracksApi.paper(track.id, id, false)); })}>
                      <Icon name="close" size={13} /><span className="visually-hidden">Remove paper</span></button>
                  </li>)}</ul> : <p className="track-card-hint">No papers linked yet.</p>}
                  <div className="track-inline-form">
                    <select aria-label="Library paper" value={paperId} disabled={busy} onChange={(event) => setPaperId(event.target.value)}>
                      <option value="">Choose a library paper</option>{papers.filter((item) => !track.paper_ids.includes(item.id)).map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}
                    </select>
                    <button className="button secondary small" disabled={busy || !paperId} onClick={() => void perform(async () => { updateTrack(await tracksApi.paper(track.id, paperId, true)); setPaperId(''); })}>Link paper</button>
                  </div>
                  <p className="track-card-hint">Links reuse library entries; PDFs are not copied.</p>
                </section>

                <section className="track-card">
                  <header><h3>Discuss in chat</h3></header>
                  <div className="track-inline-form">
                    <select aria-label="Track assistance skill" value={skill} onChange={(event) => setSkill(event.target.value)}>
                      <option value="">No specific skill</option>{skills.map((name) => <option key={name} value={name}>{name}</option>)}
                    </select>
                    <Link className="button secondary small" to={trackAssistUrl(track, section, skill)}><Icon name="chat" size={13} />Discuss {label(section)} in new chat</Link>
                  </div>
                  <p className="track-card-hint">Opens an unsent prompt using your normal chat model. It references saved content, not unsaved drafts.</p>
                </section>

                {track.conversation_ids.length ? <section className="track-card">
                  <header><h3>Linked chats</h3><span className="tag">{track.conversation_ids.length}</span></header>
                  <ul className="track-links">{track.conversation_ids.map((id) => <li key={id}>
                    <Link to={`/?conversation=${encodeURIComponent(id)}`} title={id}><Icon name="chat" size={13} /><span>Open chat {id.slice(0, 8)}</span></Link>
                  </li>)}</ul>
                </section> : null}

                <section className="track-card">
                  <header><h3>Recent section updates</h3></header>
                  <ul className="track-updates">{track.sections.map((item) => <li key={item.section}><span>{label(item.section)}</span><time dateTime={item.modified_at}>{shortDate(item.modified_at)}</time></li>)}</ul>
                </section>
              </aside>
            </div>

            {preview ? <section ref={importPanel} className="panel track-import" aria-label="Import preview">
              <header className="panel-header"><div><h2>Import preview — no changes saved yet</h2>
                <p>{preview.paper_ids.length} incoming paper links. Existing local library papers are reused.</p></div></header>
              {preview.warnings.length ? <div className="notice track-warnings"><ul>{preview.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul></div> : null}
              {preview.sections.map((item) => <div className="track-import-section" key={item.section}>
                <h3>{label(item.section)}</h3>
                <details className="disclosure"><summary>Line-by-line diff</summary><pre>{item.diff || 'No differences'}</pre></details>
                <div className="track-diff"><div><h4>Current content</h4><pre>{item.current_content}</pre></div><div><h4>Incoming content</h4><pre>{item.incoming_content}</pre></div></div>
                {importMode === 'merge' ? <label className="field">Merged {label(item.section)} Markdown<textarea className="track-textarea" aria-label={`Merged ${label(item.section)} Markdown`} value={mergeDrafts[item.section] ?? ''} disabled={busy} onChange={(event) => setMergeDrafts((items) => ({ ...items, [item.section]: event.target.value }))} /></label> : null}
              </div>)}
              <div className="track-import-footer">
                <label className="field">Import mode <select aria-label="Import mode" value={importMode} disabled={busy} onChange={(event) => { setImportMode(event.target.value as 'merge' | 'replace'); setReplaceConfirmed(false); }}>
                  <option value="merge">Merge (review combined content)</option><option value="replace">Replace affected sections</option>
                </select></label>
                {importMode === 'replace' ? <label className="check-row"><input type="checkbox" checked={replaceConfirmed} disabled={busy} onChange={(event) => setReplaceConfirmed(event.target.checked)} />I confirm replacing the displayed sections. Earlier content remains in history.</label> : null}
                <div className="button-row">
                  <button className="button secondary" disabled={busy} onClick={() => { if (window.confirm('Discard this import preview and merged drafts?')) setPreview(null); }}>Cancel import</button>
                  <button className="button" disabled={busy || (importMode === 'replace' && !replaceConfirmed)} onClick={() => void applyImport()}>Apply import</button>
                </div>
              </div>
            </section> : null}
          </>}
      </main>
    </div>
  </div>;
}
