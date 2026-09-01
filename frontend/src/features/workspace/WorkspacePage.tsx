import { useEffect, useState } from 'react';
import { json, request } from '../../api/client';
import type { components } from '../../api/schema.generated';
import { Icon } from '../../shared/components/Icons';
import { MarkdownViewer } from '../../shared/components/MarkdownViewer';
import { EmptyState, ErrorNotice, Loading, PageHeader, Panel } from '../../shared/components/Ui';
import './workspace.css';

type WorkspaceFile = components['schemas']['WorkspaceFileResponse'];
type WorkspaceContent = components['schemas']['WorkspaceFileContentResponse'];

interface FileTreeNode {
  name: string;
  displayName?: string;
  path: string;
  folders: FileTreeNode[];
  files: WorkspaceFile[];
}

export default function WorkspacePage() {
  const [files, setFiles] = useState<WorkspaceFile[]>([]);
  const [selected, setSelected] = useState<WorkspaceContent | null>(null);
  const [draft, setDraft] = useState('');
  const [tagsDraft, setTagsDraft] = useState('');
  const [view, setView] = useState<'preview' | 'edit'>('preview');
  const [newPath, setNewPath] = useState('');
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [deletingFolder, setDeletingFolder] = useState<string | null>(null);
  const load = () => request<WorkspaceFile[]>('/workspace/files').then(setFiles);
  const fileTree = buildFileTree(files);
  useEffect(() => { load().catch(setError).finally(() => setLoading(false)); }, []);
  if (loading) return <Loading label="Loading workspace…" />;

  const open = (path: string) =>
    request<WorkspaceContent>(`/workspace/files/content?path=${encodeURIComponent(path)}`)
      .then((file) => {
        setSelected(file);
        setDraft(typeof file.content === 'string' ? file.content : JSON.stringify(file.content, null, 2));
        setTagsDraft(file.tags.join(', '));
        setView(file.media_type === 'text/markdown' ? 'preview' : 'edit');
      })
      .catch(setError);
  const save = (path: string) =>
    request<WorkspaceContent>('/workspace/files/content', json('PUT', {
      path,
      content: draft,
      tags: tagsDraft.split(',').map((tag) => tag.trim()).filter(Boolean),
    }))
      .then((file) => { setSelected(file); setNewPath(''); return load(); })
      .catch(setError);
  const deleteFolder = async (folder: FileTreeNode) => {
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
        setSelected(null);
        setDraft('');
        setTagsDraft('');
      }
      await load();
    } catch (nextError) {
      setError(nextError);
    } finally {
      setDeletingFolder(null);
    }
  };

  return (
    <div className="page">
      <PageHeader title="Files" description="Notes and documents the agent can read and write while it works." />
      {error ? <ErrorNotice error={error} /> : null}
      <div className="workspace-layout">
        <Panel description={files.length ? `${files.length} file${files.length === 1 ? '' : 's'}` : 'No files yet'}>
          <div className="stack-tight">
            <div className="row" style={{ marginBottom: 'var(--space-1)' }}>
              <input placeholder="notes/idea.md" value={newPath} onChange={(event) => setNewPath(event.target.value)} />
              <button
                className="button small"
                type="button"
                disabled={!newPath.trim()}
                onClick={() => { setSelected(null); setDraft(''); setTagsDraft(''); setView('edit'); }}
              >
                <Icon name="plus" size={13} />
                New
              </button>
            </div>
            <div className="file-list">
              {fileTree.folders.map((folder) => (
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
              {!files.length ? <p>No workspace files yet.</p> : null}
            </div>
          </div>
        </Panel>
        <Panel title={(selected?.path ?? newPath) || 'Editor'}>
          {selected || newPath ? (
            <div className="stack">
              <label className="field workspace-tags">
                <span>Tags</span>
                <input
                  placeholder="transformers, evaluation, paper:…"
                  value={tagsDraft}
                  onChange={(event) => setTagsDraft(event.target.value)}
                />
              </label>
              {selected?.tags.length ? (
                <div className="workspace-tag-list" aria-label="File tags">
                  {selected.tags.map((tag) => <span className="tag" key={tag}>{tag}</span>)}
                </div>
              ) : null}
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
                <textarea className="workspace-editor mono" value={draft} onChange={(event) => setDraft(event.target.value)} />
              )}
              <div className="button-row"><button className="button" type="button" onClick={() => void save(selected?.path ?? newPath)}><Icon name="save" size={15} />Save</button>{selected ? <button className="button danger" type="button" onClick={() => void request<void>(`/workspace/files/content?path=${encodeURIComponent(selected.path)}`, { method: 'DELETE' }).then(() => { setSelected(null); setDraft(''); setTagsDraft(''); return load(); }).catch(setError)}>Delete</button> : null}</div>
            </div>
          ) : (
            <EmptyState
              icon="workspace"
              title="Nothing open"
              description="Select a file on the left, or enter a new safe relative path to create one."
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
        {folder.path !== 'papers' ? (
          <button
            className="folder-delete-button"
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
        <strong>{file.name}</strong>
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
        .find((folder) => folder.name === 'papers')
        ?.folders.find((folder) => folder.name === file.paper_id);
      if (paperFolder) paperFolder.displayName = file.paper_name;
    }
    if (file.note_id && file.note_name) {
      const noteFolder = root.folders
        .find((folder) => folder.name === 'notes')
        ?.folders.find((folder) => folder.name === file.note_id);
      if (noteFolder) noteFolder.displayName = file.note_name;
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
