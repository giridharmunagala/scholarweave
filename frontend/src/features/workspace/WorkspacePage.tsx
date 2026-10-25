import { useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, json, request } from '../../api/client';
import type { components } from '../../api/schema.generated';
import { Link, useLocation, useNavigate, useNavigationGuard } from '../../app/router';
import { Icon } from '../../shared/components/Icons';
import { MarkdownViewer } from '../../shared/components/MarkdownViewer';
import { SettingsDrawer } from '../../shared/components/SettingsDrawer';
import { EmptyState, ErrorNotice, Loading, PageHeader, Panel } from '../../shared/components/Ui';
import '../library.css';

type WorkspaceFile = components['schemas']['WorkspaceFileResponse'];
type WorkspaceContent = components['schemas']['WorkspaceFileContentResponse'];
type SearchResult = components['schemas']['WorkspaceSearchResponse'];
type CleanupPreview = components['schemas']['WorkspaceNoteCleanupPreview'];
type CleanupResult = components['schemas']['WorkspaceNoteCleanupResponse'];
const PAGE_SIZE = 25;
const WORKSPACE_VIEWS = [
  { value: 'notes', label: 'Notes' },
  { value: 'standalone', label: 'Standalone notes' },
  { value: 'paper-notes', label: 'Paper notes' },
  { value: 'summaries', label: 'Paper summaries' },
  { value: 'files', label: 'All workspace files (tree)' },
];
const ARTIFACT_AREAS = [
  { path: 'knowledge', label: 'Standalone notes', description: 'Ideas and supporting evidence' },
  { path: 'library', label: 'Paper library', description: 'Sources, notes and summaries by paper' },
  { path: 'research', label: 'Research artifacts', description: 'Review outputs grouped by track and review' },
  { path: 'projects', label: 'Projects', description: 'Research tracks and their sections' },
  { path: 'inbox', label: 'Inbox', description: 'Original chat attachments' },
];

interface FileTreeNode {
  name: string;
  displayName?: string;
  path: string;
  folders: FileTreeNode[];
  files: WorkspaceFile[];
}

function isJson(file: WorkspaceContent): boolean {
  return file.media_type === 'application/json' || file.path.toLowerCase().endsWith('.json');
}

function editorContent(file: WorkspaceContent): string {
  return isJson(file) ? JSON.stringify(file.content, null, 2)
    : typeof file.content === 'string' ? file.content : JSON.stringify(file.content, null, 2);
}

function artifactProtection(file: WorkspaceContent | null): { reason: string; owner: string; label: string } | null {
  if (!file) return null;
  const path = file.path.replace(/\\/g, '/').toLowerCase();
  if (path.startsWith('projects/tracks/')) {
    const [, id] = file.path.replace(/\\/g, '/').split('/')[2]?.split('--') ?? [];
    return {
      reason: 'Managed track files are read-only here. Edit them in their track to preserve revision history.',
      owner: id ? `/tracks?id=${encodeURIComponent(id)}` : '/tracks', label: 'Open track editor',
    };
  }
  if (path.startsWith('.scholarweave/') || path.startsWith('inbox/attachments/')
    || (path.startsWith('library/papers/') && !['paper_notes', 'paper_summary'].includes(file.kind))
    || (path.startsWith('research/') && path.endsWith('.json'))) {
    return {
      reason: 'This generated artifact is read-only. Its owning workflow manages its content and provenance.',
      owner: file.paper_id ? `/library?paper=${encodeURIComponent(file.paper_id)}` : '/deep-work',
      label: file.paper_id ? 'Open paper' : 'Open Research',
    };
  }
  return null;
}

export default function WorkspacePage() {
  const [files, setFiles] = useState<WorkspaceFile[]>([]);
  const [results, setResults] = useState<SearchResult[]>([]);
  const [scope, setScope] = useState('notes');
  const [area, setArea] = useState('');
  const [fileQuery, setFileQuery] = useState('');
  const [readerFocused, setReaderFocused] = useState(false);
  const [query, setQuery] = useState('');
  const [tagFilter, setTagFilter] = useState('');
  const [offset, setOffset] = useState(0);
  const [revision, setRevision] = useState(0);
  const [selected, setSelected] = useState<WorkspaceContent | null>(null);
  const [draft, setDraft] = useState('');
  const [tagsDraft, setTagsDraft] = useState('');
  const [view, setView] = useState<'preview' | 'edit'>('preview');
  const [newPath, setNewPath] = useState('');
  const [newName, setNewName] = useState('');
  const [openRevision, setOpenRevision] = useState(0);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [opening, setOpening] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveState, setSaveState] = useState('');
  const [savedPath, setSavedPath] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsError, setSettingsError] = useState<unknown>(null);
  const [cleaning, setCleaning] = useState(false);
  const [cleanupStatus, setCleanupStatus] = useState('');
  const [cleanupIssues, setCleanupIssues] = useState<{ path: string; reason: string }[]>([]);
  const [cleanupError, setCleanupError] = useState<unknown>(null);
  const requestId = useRef(0);
  const { searchParams } = useLocation();
  const navigate = useNavigate();
  const linkedPath = searchParams.get('path');
  const [deletingFolder, setDeletingFolder] = useState<string | null>(null);
  const load = () => { setRevision((value) => value + 1); };
  const visibleFiles = useMemo(() => files.filter((file) => {
    const inArea = !area || file.path.startsWith(`${area}/`);
    const text = [file.path, file.note_name, file.paper_name].filter(Boolean).join(' ').toLowerCase();
    return inArea && text.includes(fileQuery.trim().toLowerCase());
  }), [files, area, fileQuery]);
  const fileTree = useMemo(() => buildFileTree(visibleFiles), [visibleFiles]);
  const targetPath = selected?.path ?? newPath.trim();
  const protection = artifactProtection(selected);
  const savedContent = selected ? editorContent(selected) : '';
  const hasUnsavedChanges = !protection && ((!selected && !!newPath.trim()) || draft !== savedContent
    || tagsDraft.split(',').map((tag) => tag.trim()).filter(Boolean).join(',') !== (selected?.tags ?? []).join(','));
  const resetEditor = () => {
    setSelected(null);
    setDraft('');
    setTagsDraft('');
    setNewPath('');
  };
  useNavigationGuard(hasUnsavedChanges || saving || opening || cleaning, 'Discard unsaved note changes and leave this draft?', !saving && !opening && !cleaning);
  useEffect(() => {
    if (savedPath && !saving && !hasUnsavedChanges) {
      navigate(`/library/notes?path=${encodeURIComponent(savedPath)}`, { replace: true });
      setSavedPath(null);
    }
  }, [savedPath, saving, hasUnsavedChanges, navigate]);
  const discardDraft = () => !hasUnsavedChanges || window.confirm('Discard unsaved note changes?');
  useEffect(() => {
    let active = true;
    setLoading(true);
    const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) });
    if (query.trim()) params.set('query', query.trim());
    for (const tag of tagFilter.split(',').map((value) => value.trim()).filter(Boolean)) params.append('tags', tag);
    if (scope === 'notes') {
      params.append('kinds', 'note');
      params.append('kinds', 'paper_notes');
    } else if (scope === 'standalone') params.append('kinds', 'note');
    else if (scope === 'paper-notes') params.append('kinds', 'paper_notes');
    else if (scope === 'summaries') params.append('kinds', 'paper_summary');
    const tree = scope === 'files';
    const fetchResults = () => {
      request<SearchResult[]>(tree ? '/workspace/files' : `/workspace/search?${params}`)
        .then((items) => { if (active) { if (tree) setFiles(items); else setResults(items); } })
        .catch((reason) => { if (active) setError(reason); })
        .finally(() => { if (active) setLoading(false); });
    };
    const timer = !tree && (query || tagFilter) ? window.setTimeout(fetchResults, 200) : undefined;
    if (timer === undefined) fetchResults();
    return () => { active = false; window.clearTimeout(timer); };
  }, [scope, query, tagFilter, offset, revision]);
  useEffect(() => {
    setOpening(false);
    if (!linkedPath) {
      resetEditor();
      setSaveState('');
      return;
    }
    if (linkedPath === selected?.path) return;
    const id = ++requestId.current;
    setOpening(true);
    setError(null);
    request<WorkspaceContent>(`/workspace/files/content?path=${encodeURIComponent(linkedPath)}`)
      .then((file) => {
        if (id !== requestId.current) return;
        setSelected(file);
        setDraft(editorContent(file));
        setTagsDraft(file.tags.join(', '));
        setNewPath('');
        setSaveState('');
        setView(file.media_type === 'text/markdown' ? 'preview' : 'edit');
      })
      .catch((reason) => { if (id === requestId.current) setError(reason); })
      .finally(() => { if (id === requestId.current) setOpening(false); });
    return () => { requestId.current += 1; };
  }, [linkedPath, openRevision]);
  const open = async (path: string) => {
    if (opening || saving || cleaning) return;
    if (path === linkedPath && selected?.path === path) return;
    if (path === linkedPath) {
      if (discardDraft()) setOpenRevision((value) => value + 1);
    } else navigate(`/library/notes?path=${encodeURIComponent(path)}`);
  };
  const save = async () => {
    if (saving || opening || cleaning || protection) return;
    setSaving(true);
    setError(null);
    setSaveState('Saving...');
    try {
      const tags = tagsDraft.split(',').map((tag) => tag.trim()).filter(Boolean);
      if (selected && !selected.sha256) throw new Error('This file has no revision hash and cannot be safely saved. Reopen a text note.');
      let content: unknown = draft;
      if (selected && isJson(selected)) {
        try { content = JSON.parse(draft); }
        catch { throw new Error('Invalid JSON. Correct the JSON before saving; your draft is retained.'); }
      }
      const file = await request<WorkspaceContent>(
        selected ? '/workspace/files/content' : '/workspace/files/notes',
        json(selected ? 'PUT' : 'POST', selected
          ? { path: selected.path, content, tags, expected_sha256: selected.sha256 }
          : { name: newPath.trim(), content: draft, tags }),
      );
      setSelected(file);
      setNewPath('');
      setDraft(editorContent(file));
      setTagsDraft(file.tags.join(', '));
      setSaveState('Saved locally');
      setSavedPath(file.path);
      load();
    } catch (reason) {
      setError(reason);
      setSaveState(reason instanceof ApiError && reason.status === 409
        ? 'Conflict: this note changed on disk. Your draft is retained. Copy it before reloading the latest version to reconcile changes.'
        : 'Save failed. Your draft is retained.');
    } finally { setSaving(false); }
  };
  const deleteFolder = async (folder: FileTreeNode) => {
    if (saving || opening || cleaning) return;
    const fileCount = countFiles(folder);
    if (!window.confirm(`Delete "${folder.displayName ?? folder.name}" and its ${fileCount} file${fileCount === 1 ? '' : 's'}? This cannot be undone.`)) return;
    setDeletingFolder(folder.path);
    setError(null);
    try {
      await request<void>(
        `/workspace/files/folder?path=${encodeURIComponent(folder.path)}`,
        { method: 'DELETE' },
      );
      if (selected?.path.startsWith(`${folder.path}/`)) {
        resetEditor();
      }
      load();
    } catch (nextError) {
      setError(nextError);
    } finally {
      setDeletingFolder(null);
    }
  };
  const deleteFile = async () => {
    if (cleaning || !selected || !window.confirm(`Delete "${selected.path}"? This cannot be undone.`)) return;
    setError(null);
    try {
      await request<void>(
        `/workspace/files/content?path=${encodeURIComponent(selected.path)}`,
        { method: 'DELETE' },
      );
      resetEditor();
      load();
    } catch (nextError) {
      setError(nextError);
    }
  };
  const cleanupNotes = async () => {
    if (cleaning || saving || opening || deletingFolder) return;
    setCleaning(true);
    setCleanupError(null);
    setCleanupStatus('Checking note sizes...');
    setCleanupIssues([]);
    try {
      const preview = await request<CleanupPreview>('/workspace/notes/cleanup/preview', { method: 'POST' });
      const candidates = preview.candidates.filter((item) => item.path !== selected?.path);
      setCleanupIssues(preview.failed);
      if (!candidates.length) {
        setCleanupStatus(preview.failed.length
          ? `No removable notes found among the files checked. ${preview.failed.length} could not be inspected and were kept.`
          : 'No notes smaller than 1024 bytes to remove. The open note, if any, was kept.');
        return;
      }
      const confirmed = window.confirm(
        `Permanently delete ${candidates.length} note${candidates.length === 1 ? '' : 's'} smaller than 1 KB (1024 bytes) across all notes? `
        + 'Only standalone notes and canonical paper notes are included. Summaries, generated artifacts and the open note are kept. '
        + 'This cannot be undone.',
      );
      if (!confirmed) {
        setCleanupStatus('Cleanup canceled. No notes were removed.');
        return;
      }
      setCleanupStatus('Removing confirmed notes...');
      const result = await request<CleanupResult>('/workspace/notes/cleanup', json('POST', { candidates }));
      const count = result.deleted_paths.length;
      setCleanupStatus(`Removed ${count} note${count === 1 ? '' : 's'}. `
        + `${result.skipped.length} skipped; ${result.failed.length} failed. `
        + (preview.failed.length ? `${preview.failed.length} could not be inspected and were kept. ` : '')
        + 'The open note and any draft were kept.');
      setCleanupIssues([...preview.failed, ...result.skipped, ...result.failed]);
      if (count) setOffset(0);
      load();
    } catch (reason) {
      setCleanupError(reason);
      setCleanupStatus('Cleanup could not be completed. Refresh the file list before retrying; your open note and draft are kept.');
      load();
    } finally { setCleaning(false); }
  };

  return (
    <div className="page page-wide">
      <PageHeader
        title="Notes"
        description="Read, edit and rediscover your notes, summaries and research artifacts."
        actions={<button className="icon-button settings-trigger" type="button" aria-label="Notes settings" title="Notes settings"
          aria-haspopup="dialog" aria-expanded={settingsOpen} aria-controls="notes-settings"
          onClick={() => setSettingsOpen(true)}><Icon name="settings" size={20} /><span>Settings</span></button>}
      />
      {error ? <ErrorNotice error={error} /> : null}
      <SettingsDrawer id="notes-settings" title="Notes settings" open={settingsOpen} onClose={() => setSettingsOpen(false)}>
        <div className="stack">
          {settingsError ? <ErrorNotice error={settingsError} /> : null}
          {scope !== 'files' ? <input aria-label="Filter by tags" placeholder="Filter tags, comma separated" value={tagFilter}
            onChange={(event) => { setTagFilter(event.target.value); setOffset(0); }} /> : null}
          <button className="button secondary" type="button" aria-pressed={readerFocused}
            onClick={() => setReaderFocused((value) => !value)}>
            {readerFocused ? 'Show artifact browser' : 'Focus reader'}
          </button>
          <button className="button secondary small" type="button" disabled={refreshing || cleaning} onClick={async () => {
            setRefreshing(true);
            setError(null);
            setSettingsError(null);
            try { await request('/workspace/index', { method: 'POST' }); load(); }
            catch (reason) { setError(reason); setSettingsError(reason); }
            finally { setRefreshing(false); }
          }}>{refreshing ? 'Refreshing...' : 'Refresh external edits'}</button>
          <section className="stack-tight" aria-label="Note cleanup">
            <h3>Clean up stub notes</h3>
            <p>Remove standalone and canonical paper notes smaller than 1 KB (1024 bytes), across all filters.
              Summaries, generated artifacts, managed track files and the open note are kept. Your unsaved draft is never removed.</p>
            <button className="button danger" type="button" disabled={cleaning || saving || opening || refreshing || deletingFolder !== null}
              onClick={() => void cleanupNotes()}>{cleaning ? 'Cleaning up...' : 'Clean up notes under 1 KB'}</button>
            {cleanupStatus ? <p role="status">{cleanupStatus}</p> : null}
            {cleanupError ? <ErrorNotice error={cleanupError} /> : null}
            {cleanupIssues.length ? <ul>{cleanupIssues.map((issue, index) =>
              <li key={`${issue.path}-${index}`}>{issue.path}: {issue.reason}</li>)}</ul> : null}
          </section>
        </div>
      </SettingsDrawer>
      <div className={`workspace-layout${readerFocused ? ' workspace-focused' : ''}`}>
        <Panel className="workspace-browser" title="Artifact browser" description="Find existing work before creating another.">
          <div className="stack-tight">
            <label>Workspace view
              <select aria-label="Workspace view" value={scope} onChange={(event) => { setScope(event.target.value); setOffset(0); }}>
                {WORKSPACE_VIEWS.map((view) => <option key={view.value} value={view.value}>{view.label}</option>)}
              </select>
            </label>
            {scope !== 'notes' || tagFilter.trim() ? <div className="row" aria-label="Active note filters">
              <span className="muted" role="status">{WORKSPACE_VIEWS.find((view) => view.value === scope)?.label}
                {scope !== 'files' && tagFilter.trim() ? ` · Tags: ${tagFilter}` : ''}</span>
              <button className="button ghost small" type="button" onClick={() => { setScope('notes'); setTagFilter(''); setOffset(0); }}>Reset filters</button>
            </div> : null}
            {scope !== 'files' ?
              <input aria-label="Search notes" placeholder="Search note text (BM25)" value={query} onChange={(event) => { setQuery(event.target.value); setOffset(0); }} />
            : <>
              <nav className="artifact-areas" aria-label="Artifact locations">
                <button type="button" className="artifact-area" aria-pressed={!area} onClick={() => setArea('')}>
                  <strong>All locations</strong><small>{files.length} files</small>
                </button>
                {ARTIFACT_AREAS.map((item) => <button type="button" key={item.path}
                  className="artifact-area" aria-pressed={area === item.path} onClick={() => setArea(item.path)}>
                  <strong>{item.label}</strong>
                  <small>{files.filter((file) => file.path.startsWith(`${item.path}/`)).length} files</small>
                  <span>{item.description}</span>
                </button>)}
              </nav>
              <input aria-label="Find artifact" placeholder="Find by name, paper or folder" value={fileQuery}
                onChange={(event) => setFileQuery(event.target.value)} />
            </>}
            <div className="row new-note">
              <input
                aria-label="New note name"
                placeholder="New note name"
                value={newName}
                onChange={(event) => setNewName(event.target.value)}
              />
              <button
                className="button small"
                type="button"
                disabled={!newName.trim() || saving || opening || cleaning}
                onClick={() => {
                  if (!discardDraft()) return;
                  setSelected(null);
                  setDraft('');
                  setTagsDraft('');
                  setNewPath(newName.trim());
                  setNewName('');
                  setView('edit');
                  setSaveState('');
                }}
              >
                <Icon name="plus" size={13} />
                New
              </button>
            </div>
            <div className="file-list" aria-label="Artifacts" aria-busy={loading}>
              {loading ? <Loading label="Loading workspace..." /> : null}
              {scope === 'files' ? <>{fileTree.folders.map((folder) => (
                <WorkspaceFolder
                  folder={folder}
                  key={folder.path}
                  selectedPath={selected?.path}
                  onOpen={open}
                  onDelete={deleteFolder}
                  deletingPath={deletingFolder}
                  expandMatches={!!fileQuery.trim()}
                />
              ))}
              {fileTree.files.map((file) => (
                <WorkspaceFileRow
                  file={file}
                  key={file.path}
                  selectedPath={selected?.path}
                  onOpen={open}
                />
              ))}
              {!visibleFiles.length && !loading ? <p>{files.length ? 'No artifacts match this location or name.' : 'No workspace files yet.'}</p> : null}</> : <>
                {results.map((file) => {
                  const systemTags = file.kind === 'paper_notes' ? ['paper', 'notes']
                    : file.kind === 'paper_summary' ? ['paper', 'summary']
                    : file.kind === 'note' ? ['note'] : [];
                  const tags = file.tags.filter((tag) => !systemTags.includes(tag.toLowerCase())
                    && !tag.toLowerCase().startsWith('paper:') && !tag.toLowerCase().startsWith('brief-source:'));
                  const activeTags = tagFilter.split(',').map((tag) => tag.trim()).filter(Boolean);
                  return <div className="workspace-result" key={file.path}>
                  <WorkspaceFileRow file={file} selectedPath={selected?.path} onOpen={open} showPaperTitle />
                  <small className="muted">{file.kind === 'paper_notes' ? 'Paper note' : file.kind === 'paper_summary' ? 'Paper summary' : 'Standalone note'} · {new Date(file.modified_at).toLocaleDateString()}</small>
                  {tags.length ? <div className="library-tags" aria-label={`Tags for ${file.note_name ?? file.paper_name ?? file.name}`}>
                    {tags.map((tag) => <button className="button secondary small" type="button" key={tag}
                      aria-pressed={activeTags.some((value) => value.toLowerCase() === tag.toLowerCase())}
                      title={`Toggle filter: ${tag}`} onClick={() => {
                      const active = activeTags.some((value) => value.toLowerCase() === tag.toLowerCase());
                      setTagFilter((active ? activeTags.filter((value) => value.toLowerCase() !== tag.toLowerCase()) : [...activeTags, tag]).join(', '));
                      setOffset(0);
                    }}>{tag}</button>)}
                  </div> : null}
                  {file.excerpt ? <p className="muted">{file.excerpt}</p> : null}
                </div>;
                })}
                {!results.length && !loading ? <p>No matching notes. Try fewer search terms or tags.</p> : null}
              </>}
            </div>
            {scope !== 'files' ? <div className="library-pagination">
                  <button className="button small" disabled={offset === 0 || loading} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>Previous</button>
                  <small>Page {Math.floor(offset / PAGE_SIZE) + 1}</small>
                  <button className="button small" disabled={results.length < PAGE_SIZE || loading} onClick={() => setOffset(offset + PAGE_SIZE)}>Next</button>
                </div> : null}
          </div>
        </Panel>
        <Panel
          className="workspace-reader"
          title={selected?.note_name ?? selected?.paper_name ?? (targetPath ? (targetPath.split('/').pop() ?? targetPath) : 'Reader')}
          description={targetPath || undefined}
          actions={(selected || newPath) && !protection ? (
            <>
              <button className="button" type="button" disabled={saving || opening || cleaning || !hasUnsavedChanges} onClick={() => void save()}>
                <Icon name="save" size={14} />
                {saving ? 'Saving...' : 'Save'}
              </button>
              {selected ? (
                <button className="button danger small" type="button" disabled={saving || opening || cleaning} onClick={() => void deleteFile()}>
                  <Icon name="trash" size={13} />
                  Delete
                </button>
              ) : null}
            </>
          ) : undefined}
        >
          {selected || newPath ? (
            <div className="stack">
              <p role="status">{opening ? 'Opening...' : protection ? 'Read-only artifact' : saveState || (hasUnsavedChanges ? 'Unsaved changes' : 'Saved locally')}</p>
              {protection ? <div className="notice info">
                <p>{protection.reason}</p>
                <Link className="button secondary small" to={protection.owner}>{protection.label}</Link>
              </div> : null}
              {selected ? <div className="row">
                <a href={`/library/notes?path=${encodeURIComponent(selected.path)}`}>Link to this note</a>
                <button className="button secondary small" disabled={saving || opening || cleaning} onClick={async () => {
                  if (!discardDraft()) return;
                  setOpening(true);
                  try {
                    const file = await request<WorkspaceContent>(`/workspace/files/content?path=${encodeURIComponent(selected.path)}`);
                    setSelected(file);
                    setDraft(editorContent(file));
                    setTagsDraft(file.tags.join(', '));
                    setSaveState('');
                    setError(null);
                  } catch (reason) { setError(reason); }
                  finally { setOpening(false); }
                }}>Reload latest</button>
              </div> : null}
              <div className="library-handoff">
                {selected && !hasUnsavedChanges ? (
                  <Link
                    className="button secondary small"
                    title="Open an editable draft in a new research chat"
                    to={`/chat?research=${encodeURIComponent(`Analyze the existing saved work at workspace path "${selected.path}". Read it, explain its key ideas, assess its evidence and open questions, and discuss it here in chat. Do not create another saved summary or overwrite existing work.`)}`}
                  >
                    <Icon name="chat" size={13} />
                    Analyze saved work
                  </Link>
                ) : (
                  <button className="button secondary small" type="button" disabled>Analyze saved work</button>
                )}
                <small className="muted">
                  {selected && !hasUnsavedChanges
                    ? 'Opens a draft you can edit before sending.'
                    : 'Save your changes before discussing them in chat.'}
                </small>
              </div>
              {isMarkdown(selected?.media_type, selected?.path ?? newPath) ? (
                <div className="segmented workspace-view-toggle" aria-label="File view">
                  <button type="button" aria-pressed={view === 'preview'} onClick={() => setView('preview')}>Preview</button>
                  <button type="button" aria-pressed={view === 'edit'} onClick={() => setView('edit')}>{protection ? 'View source' : 'Edit'}</button>
                </div>
              ) : null}
              {view === 'preview' && isMarkdown(selected?.media_type, selected?.path ?? newPath) ? (
                <div className="workspace-preview">
                  <MarkdownViewer content={draft} workspacePath={selected?.path} onNavigate={navigate} />
                </div>
              ) : (
                <textarea aria-label="Note content" className="workspace-editor mono" readOnly={Boolean(protection)} disabled={saving || opening} value={draft} onChange={(event) => { setDraft(event.target.value); setSaveState(''); }} />
              )}
              <details className="workspace-options">
                <summary>Tags{tagsDraft ? ` · ${tagsDraft.split(',').filter(Boolean).length}` : ''}</summary>
                <input
                  aria-label="Tags"
                  placeholder="transformers, evaluation"
                  value={tagsDraft}
                  disabled={saving || opening || Boolean(protection)}
                  onChange={(event) => { setTagsDraft(event.target.value); setSaveState(''); }}
                />
              </details>
            </div>
          ) : (
            <EmptyState
              icon="workspace"
              title="Nothing open"
              description="Select a note on the left, or enter a name to create a standalone supporting note."
            />
          )}
        </Panel>
      </div>
    </div>
  );
}

function WorkspaceFolder({
  folder,
  selectedPath,
  onOpen,
  onDelete,
  deletingPath,
  expandMatches = false,
}: {
  folder: FileTreeNode;
  selectedPath?: string;
  onOpen: (path: string) => Promise<void>;
  onDelete: (folder: FileTreeNode) => Promise<void>;
  deletingPath: string | null;
  expandMatches?: boolean;
}) {
  const containsSelection = !!selectedPath?.startsWith(`${folder.path}/`);
  const [expanded, setExpanded] = useState(containsSelection || !folder.path.includes('/') || folder.path === 'library/papers');
  useEffect(() => { if (containsSelection) setExpanded(true); }, [containsSelection]);
  return (
    <details className="file-tree-folder" open={expanded || expandMatches} onToggle={(event) => {
      if (!expandMatches) setExpanded(event.currentTarget.open);
    }}>
      <summary>
        <Icon name="workspace" size={15} />
        <span className="file-tree-folder-label">
          <strong title={folder.path}>{folder.displayName ?? folder.name}</strong>
        </span>
        <small>{countFiles(folder)}</small>
        {!expandMatches && !['library', 'library/papers', 'knowledge', 'projects', 'inbox'].includes(folder.path)
          && !folder.path.startsWith('library/papers/')
          && folder.path !== 'projects/tracks' && !folder.path.startsWith('projects/tracks/') ? (
          <button
            className="folder-delete-button icon-button danger row-action small"
            type="button"
            title={`Delete ${folder.displayName ?? folder.name}`}
            aria-label={`Delete folder ${folder.displayName ?? folder.name}`}
            disabled={deletingPath !== null}
            onClick={(event) => {
              event.preventDefault();
              event.stopPropagation();
              void onDelete(folder);
            }}
          >
            <Icon name="trash" size={13} />
          </button>
        ) : null}
      </summary>
      <div className="file-tree-children">
        {folder.folders.map((child) => (
          <WorkspaceFolder
            folder={child}
            key={child.path}
            selectedPath={selectedPath}
            onOpen={onOpen}
            onDelete={onDelete}
            deletingPath={deletingPath}
            expandMatches={expandMatches}
          />
        ))}
        {folder.files.map((file) => (
          <WorkspaceFileRow
            file={file}
            key={file.path}
            selectedPath={selectedPath}
            onOpen={onOpen}
          />
        ))}
      </div>
    </details>
  );
}

function WorkspaceFileRow({
  file,
  selectedPath,
  onOpen,
  showPaperTitle = false,
}: {
  file: WorkspaceFile;
  selectedPath?: string;
  onOpen: (path: string) => Promise<void>;
  showPaperTitle?: boolean;
}) {
  return (
    <button
      className={selectedPath === file.path ? 'file-row active' : 'file-row'}
      type="button"
      onClick={() => void onOpen(file.path)}
      title={file.path}
      aria-current={selectedPath === file.path ? 'true' : undefined}
    >
      <span className="file-row-name">
        <Icon name="file" size={14} />
        <strong>{file.note_name ?? (showPaperTitle ? file.paper_name ?? file.name : file.name)}</strong>
      </span>
      <small>{formatBytes(file.size_bytes)}</small>
    </button>
  );
}

export function buildFileTree(files: WorkspaceFile[]): FileTreeNode {
  const root: FileTreeNode = { name: '', path: '', folders: [], files: [] };
  for (const file of files) {
    const parts = file.path.split('/').filter(Boolean);
    let parent = root;
    for (const part of parts.slice(0, -1)) {
      let folder = parent.folders.find((item) => item.name === part);
      if (!folder) {
        const path = parent.path ? `${parent.path}/${part}` : part;
        folder = { name: part, path, displayName: ARTIFACT_AREAS.find((area) => area.path === path)?.label, folders: [], files: [] };
        parent.folders.push(folder);
      }
      parent = folder;
    }
    if (file.paper_id && file.paper_name) {
      const paperFolder = root.folders
        .find((folder) => folder.name === 'library')
        ?.folders.find((folder) => folder.name === 'papers')
        ?.folders.find((folder) => folder.name === parts[2]);
      if (paperFolder) paperFolder.displayName = file.paper_name;
    }
    parent.files.push(file);
  }
  sortFileTree(root);
  return root;
}

function sortFileTree(node: FileTreeNode): void {
  node.folders.sort((left, right) => (left.displayName ?? left.name).localeCompare(right.displayName ?? right.name));
  node.files.sort((left, right) => left.name.localeCompare(right.name));
  node.folders.forEach(sortFileTree);
}

function countFiles(node: FileTreeNode): number {
  return node.files.length + node.folders.reduce((total, folder) => total + countFiles(folder), 0);
}

function isMarkdown(mediaType: string | undefined, path: string): boolean {
  return mediaType === 'text/markdown' || path.toLowerCase().endsWith('.md');
}

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}
