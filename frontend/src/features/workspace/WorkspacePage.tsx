import { useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, json, request } from '../../api/client';
import type { components } from '../../api/schema.generated';
import { Link, useLocation, useNavigate, useNavigationGuard } from '../../app/router';
import { Icon } from '../../shared/components/Icons';
import { MarkdownViewer } from '../../shared/components/MarkdownViewer';
import { EmptyState, ErrorNotice, LibraryTabs, Loading, PageHeader, Panel } from '../../shared/components/Ui';
import '../library.css';

type WorkspaceFile = components['schemas']['WorkspaceFileResponse'];
type WorkspaceContent = components['schemas']['WorkspaceFileContentResponse'];
type SearchResult = components['schemas']['WorkspaceSearchResponse'];
const PAGE_SIZE = 25;

interface FileTreeNode {
  name: string;
  displayName?: string;
  path: string;
  folders: FileTreeNode[];
  files: WorkspaceFile[];
}

export default function WorkspacePage() {
  const [files, setFiles] = useState<WorkspaceFile[]>([]);
  const [results, setResults] = useState<SearchResult[]>([]);
  const [scope, setScope] = useState('notes');
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
  const requestId = useRef(0);
  const { searchParams } = useLocation();
  const navigate = useNavigate();
  const linkedPath = searchParams.get('path');
  const [deletingFolder, setDeletingFolder] = useState<string | null>(null);
  const load = () => { setRevision((value) => value + 1); };
  const fileTree = useMemo(() => buildFileTree(files), [files]);
  const targetPath = selected?.path ?? newPath.trim();
  const savedContent = selected
    ? typeof selected.content === 'string' ? selected.content : JSON.stringify(selected.content, null, 2)
    : '';
  const hasUnsavedChanges = (!selected && !!newPath.trim()) || draft !== savedContent
    || tagsDraft.split(',').map((tag) => tag.trim()).filter(Boolean).join(',') !== (selected?.tags ?? []).join(',');
  const resetEditor = () => {
    setSelected(null);
    setDraft('');
    setTagsDraft('');
    setNewPath('');
  };
  useNavigationGuard(hasUnsavedChanges || saving || opening, 'Discard unsaved note changes and leave this draft?', !saving && !opening);
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
    } else if (scope === 'summaries') params.append('kinds', 'paper_summary');
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
        setDraft(typeof file.content === 'string' ? file.content : JSON.stringify(file.content, null, 2));
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
    if (opening || saving) return;
    if (path === linkedPath && selected?.path === path) return;
    if (path === linkedPath) {
      if (discardDraft()) setOpenRevision((value) => value + 1);
    } else navigate(`/library/notes?path=${encodeURIComponent(path)}`);
  };
  const save = async () => {
    if (saving || opening) return;
    setSaving(true);
    setError(null);
    setSaveState('Saving...');
    try {
      const tags = tagsDraft.split(',').map((tag) => tag.trim()).filter(Boolean);
      if (selected && !selected.sha256) throw new Error('This file has no revision hash and cannot be safely saved. Reopen a text note.');
      const file = await request<WorkspaceContent>(
        selected ? '/workspace/files/content' : '/workspace/files/notes',
        json(selected ? 'PUT' : 'POST', selected
          ? { path: selected.path, content: draft, tags, expected_sha256: selected.sha256 }
          : { name: newPath.trim(), content: draft, tags }),
      );
      setSelected(file);
      setNewPath('');
      setDraft(typeof file.content === 'string' ? file.content : JSON.stringify(file.content, null, 2));
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
    if (saving || opening) return;
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
    if (!selected || !window.confirm(`Delete "${selected.path}"? This cannot be undone.`)) return;
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

  return (
    <div className="page">
      <PageHeader
        title="Notes"
        description="Durable research notes and paper summaries."
      />
      <LibraryTabs active="notes" />
      {error ? <ErrorNotice error={error} /> : null}
      <div className="workspace-layout">
        <Panel description="Find existing notes before creating another.">
          <div className="stack-tight">
            <select aria-label="Workspace view" value={scope} onChange={(event) => { setScope(event.target.value); setOffset(0); }}>
              <option value="notes">Notes</option>
              <option value="summaries">Paper summaries</option>
              <option value="files">All workspace files (tree)</option>
            </select>
            {scope !== 'files' ? <>
              <input aria-label="Search notes" placeholder="Search note text (BM25)" value={query} onChange={(event) => { setQuery(event.target.value); setOffset(0); }} />
              <input aria-label="Filter by tags" placeholder="Filter tags, comma separated" value={tagFilter} onChange={(event) => { setTagFilter(event.target.value); setOffset(0); }} />
            </> : null}
            <button className="button secondary small" type="button" disabled={refreshing} onClick={async () => {
              setRefreshing(true);
              setError(null);
              try { await request('/workspace/index', { method: 'POST' }); load(); }
              catch (reason) { setError(reason); }
              finally { setRefreshing(false); }
            }}>{refreshing ? 'Refreshing...' : 'Refresh external edits'}</button>
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
                disabled={!newName.trim() || saving || opening}
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
            <div className="file-list">
              {loading ? <Loading label="Loading workspace..." /> : null}
              {scope === 'files' ? <>{fileTree.folders.map((folder) => (
                <WorkspaceFolder
                  folder={folder}
                  key={folder.path}
                  selectedPath={selected?.path}
                  onOpen={open}
                  onDelete={deleteFolder}
                  deletingPath={deletingFolder}
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
              {!files.length && !loading ? <p>No workspace files yet.</p> : null}</> : <>
                {results.map((file) => <div key={file.path}>
                  <WorkspaceFileRow file={file} selectedPath={selected?.path} onOpen={open} />
                  <small>{file.kind === 'paper_notes' ? `Paper note: ${file.paper_name ?? file.paper_id}` : file.kind === 'paper_summary' ? `Paper summary: ${file.paper_name ?? file.paper_id}` : 'Standalone note'}{file.tags.length ? ` | ${file.tags.join(', ')}` : ''}</small>
                  {file.excerpt ? <p className="muted">{file.excerpt}</p> : null}
                </div>)}
                {!results.length && !loading ? <p>No matching notes. Try fewer search terms or tags.</p> : null}
                <div className="row">
                  <button className="button small" disabled={offset === 0 || loading} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>Previous</button>
                  <small>Page {Math.floor(offset / PAGE_SIZE) + 1}</small>
                  <button className="button small" disabled={results.length < PAGE_SIZE || loading} onClick={() => setOffset(offset + PAGE_SIZE)}>Next</button>
                </div>
              </>}
            </div>
          </div>
        </Panel>
        <Panel
          title={targetPath ? (targetPath.split('/').pop() ?? targetPath) : 'Editor'}
          description={targetPath || undefined}
          actions={selected || newPath ? (
            <>
              <button className="button" type="button" disabled={saving || opening || !hasUnsavedChanges} onClick={() => void save()}>
                <Icon name="save" size={14} />
                {saving ? 'Saving...' : 'Save'}
              </button>
              {selected ? (
                <button className="button danger small" type="button" disabled={saving || opening} onClick={() => void deleteFile()}>
                  <Icon name="trash" size={13} />
                  Delete
                </button>
              ) : null}
            </>
          ) : undefined}
        >
          {selected || newPath ? (
            <div className="stack">
              <p role="status">{opening ? 'Opening...' : saveState || (hasUnsavedChanges ? 'Unsaved changes' : 'Saved locally')}</p>
              {selected ? <div className="row">
                <a href={`/library/notes?path=${encodeURIComponent(selected.path)}`}>Link to this note</a>
                <button className="button secondary small" disabled={saving || opening} onClick={async () => {
                  if (!discardDraft()) return;
                  setOpening(true);
                  try {
                    const file = await request<WorkspaceContent>(`/workspace/files/content?path=${encodeURIComponent(selected.path)}`);
                    setSelected(file);
                    setDraft(typeof file.content === 'string' ? file.content : JSON.stringify(file.content, null, 2));
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
                    to={`/?research=${encodeURIComponent(`Analyze the existing saved work at workspace path "${selected.path}". Read it, explain its key ideas, assess its evidence and open questions, and discuss it here in chat. Do not create another saved summary or overwrite existing work.`)}`}
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
                  <button type="button" aria-pressed={view === 'edit'} onClick={() => setView('edit')}>Edit</button>
                </div>
              ) : null}
              {view === 'preview' && isMarkdown(selected?.media_type, selected?.path ?? newPath) ? (
                <div className="workspace-preview">
                  <MarkdownViewer content={draft} />
                </div>
              ) : (
                <textarea aria-label="Note content" className="workspace-editor mono" disabled={saving || opening} value={draft} onChange={(event) => { setDraft(event.target.value); setSaveState(''); }} />
              )}
              <details className="workspace-options">
                <summary>Tags{tagsDraft ? ` · ${tagsDraft.split(',').filter(Boolean).length}` : ''}</summary>
                <input
                  aria-label="Tags"
                  placeholder="transformers, evaluation"
                  value={tagsDraft}
                  disabled={saving || opening}
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
}: {
  folder: FileTreeNode;
  selectedPath?: string;
  onOpen: (path: string) => Promise<void>;
  onDelete: (folder: FileTreeNode) => Promise<void>;
  deletingPath: string | null;
}) {
  return (
    <details className="file-tree-folder" open>
      <summary>
        <Icon name="workspace" size={15} />
        <span className="file-tree-folder-label">
          <strong title={folder.path}>{folder.displayName ?? folder.name}</strong>
        </span>
        <small>{countFiles(folder)}</small>
        {!['library', 'library/papers', 'knowledge', 'projects', 'inbox'].includes(folder.path)
          && !folder.path.startsWith('library/papers/') ? (
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
}: {
  file: WorkspaceFile;
  selectedPath?: string;
  onOpen: (path: string) => Promise<void>;
}) {
  return (
    <button
      className={selectedPath === file.path ? 'file-row active' : 'file-row'}
      type="button"
      onClick={() => void onOpen(file.path)}
      title={file.path}
    >
      <span className="file-row-name">
        <Icon name="file" size={14} />
        <strong>{file.note_name ?? file.name}</strong>
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
        folder = { name: part, path, folders: [], files: [] };
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
  node.folders.sort((left, right) => left.name.localeCompare(right.name));
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
