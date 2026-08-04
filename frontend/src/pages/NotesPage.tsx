import { useEffect, useMemo, useState } from 'react';
import { useConfirm } from '../components/common/ConfirmDialog';
import { EmptyState } from '../components/common/EmptyState';
import { ErrorNotice } from '../components/common/ErrorNotice';
import { Icon } from '../components/common/Icon';
import { SkeletonList } from '../components/common/Skeleton';
import { toMessage, useToast } from '../components/common/Toast';
import { MarkdownViewer } from '../components/notes/MarkdownViewer';
import { api } from '../lib/api';
import { formatBytes, formatDateTime } from '../lib/format';
import { useNavigate, useSearchParams } from '../lib/router';
import type { WorkspaceNote, WorkspaceNoteContent } from '../types/api';

type NoteFolder = {
  name: string;
  path: string;
  folders: Map<string, NoteFolder>;
  notes: WorkspaceNote[];
};

function buildNoteTree(notes: WorkspaceNote[]): NoteFolder {
  const root: NoteFolder = { name: 'Notes', path: '', folders: new Map(), notes: [] };
  for (const note of notes) {
    const segments = note.path.split('/');
    let folder = root;
    for (const segment of segments.slice(0, -1)) {
      const path = folder.path ? `${folder.path}/${segment}` : segment;
      if (!folder.folders.has(segment)) {
        folder.folders.set(segment, { name: segment, path, folders: new Map(), notes: [] });
      }
      folder = folder.folders.get(segment)!;
    }
    folder.notes.push(note);
  }
  return root;
}

function countNotes(folder: NoteFolder): number {
  return folder.notes.length + [...folder.folders.values()].reduce((total, child) => total + countNotes(child), 0);
}

function FolderContents({
  folder,
  selectedPath,
  onSelect,
}: {
  folder: NoteFolder;
  selectedPath: string;
  onSelect: (path: string) => void;
}) {
  const folders = [...folder.folders.values()].sort((left, right) => left.name.localeCompare(right.name));
  return (
    <div className="note-tree-children">
      {folders.map((child) => (
        <details key={child.path} className="note-folder" open>
          <summary>
            <Icon name="chevronRight" size={12} className="chevron" />
            <Icon name="folder" size={13} />
            <span>{child.name}</span>
            <span className="tiny-tag" style={{ marginLeft: 'auto' }}>
              {countNotes(child)}
            </span>
          </summary>
          <FolderContents folder={child} selectedPath={selectedPath} onSelect={onSelect} />
        </details>
      ))}
      {folder.notes.map((note) => (
        <button
          key={note.path}
          type="button"
          className={`note-tree-item ${selectedPath === note.path ? 'active' : ''}`}
          title={note.path}
          onClick={() => onSelect(note.path)}
        >
          <Icon name="file" size={13} />
          <span>{note.name}</span>
        </button>
      ))}
    </div>
  );
}

export function NotesPage() {
  const navigate = useNavigate();
  const toast = useToast();
  const confirm = useConfirm();
  const [searchParams] = useSearchParams();
  const requestedPath = searchParams.get('path') || '';
  const [notes, setNotes] = useState<WorkspaceNote[]>([]);
  const [selectedNote, setSelectedNote] = useState<WorkspaceNoteContent | null>(null);
  const [filter, setFilter] = useState('');
  const [loading, setLoading] = useState(true);
  const [contentLoading, setContentLoading] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState('');

  const visibleNotes = useMemo(() => {
    const term = filter.trim().toLowerCase();
    if (!term) return notes;
    return notes.filter((note) => note.path.toLowerCase().includes(term));
  }, [filter, notes]);

  const tree = useMemo(() => buildNoteTree(visibleNotes), [visibleNotes]);
  const selectedPath = requestedPath || notes[0]?.path || '';

  const refresh = async () => {
    setLoading(true);
    try {
      setNotes(await api.listNotes());
      setError('');
    } catch (err) {
      setError(toMessage(err, 'Failed to list notes'));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void refresh();
  }, []);

  useEffect(() => {
    if (!selectedPath) {
      setSelectedNote(null);
      return;
    }
    let active = true;
    setContentLoading(true);
    api
      .getNote(selectedPath)
      .then((note) => {
        if (active) {
          setSelectedNote(note);
          setError('');
        }
      })
      .catch((err) => active && setError(toMessage(err, 'Failed to load note')))
      .finally(() => active && setContentLoading(false));
    return () => {
      active = false;
    };
  }, [selectedPath]);

  const deleteSelectedNote = async () => {
    if (!selectedNote) return;
    const confirmed = await confirm({
      title: `Delete “${selectedNote.name}”?`,
      description: `${selectedNote.path} will be removed from your workspace folder. This cannot be undone.`,
      confirmLabel: 'Delete note',
    });
    if (!confirmed) return;
    setDeleting(true);
    setError('');
    try {
      await api.deleteNote(selectedNote.path);
      const nextNotes = await api.listNotes();
      setNotes(nextNotes);
      setSelectedNote(null);
      const nextPath = nextNotes[0]?.path;
      navigate(nextPath ? `/notes?${new URLSearchParams({ path: nextPath })}` : '/notes');
      toast.success(`Deleted ${selectedNote.name}`);
    } catch (err) {
      const message = toMessage(err, 'Failed to delete note');
      setError(message);
      toast.failure('Delete failed', message);
    } finally {
      setDeleting(false);
    }
  };

  return (
    <div className="notes-layout page-stack">
      <aside className="panel note-browser">
        <div className="panel-subheader">
          <h3>Workspace</h3>
          <button type="button" className="button subtle icon-only sm" disabled={loading} title="Rescan workspace" aria-label="Rescan workspace" onClick={() => void refresh()}>
            <Icon name="refresh" size={14} />
          </button>
        </div>
        <label className="search-field">
          <Icon name="search" size={13} />
          <input value={filter} placeholder="Filter by path" onChange={(event) => setFilter(event.target.value)} />
          {filter ? (
            <button type="button" aria-label="Clear filter" onClick={() => setFilter('')}>
              <Icon name="close" size={12} />
            </button>
          ) : null}
        </label>
        {error ? <ErrorNotice message={error} /> : null}
        {loading ? <SkeletonList rows={4} /> : null}
        {!loading && notes.length === 0 ? (
          <EmptyState
            icon="notes"
            title="No Markdown files"
            description="Add a .md file anywhere under your workspace/ folder and it will show up here."
          />
        ) : null}
        {!loading && notes.length > 0 && visibleNotes.length === 0 ? (
          <p className="empty-state">Nothing matched “{filter}”.</p>
        ) : null}
        <nav className="note-tree" aria-label="Markdown notes">
          <FolderContents
            folder={tree}
            selectedPath={selectedPath}
            onSelect={(path) => navigate(`/notes?${new URLSearchParams({ path })}`)}
          />
        </nav>
      </aside>

      <section className="panel note-reader">
        {contentLoading ? <SkeletonList rows={5} /> : null}
        {!contentLoading && selectedNote ? (
          <>
            <header className="note-reader-header">
              <div>
                <p className="eyebrow">{selectedNote.path}</p>
                <h2>{selectedNote.name}</h2>
                <p className="muted-text small">
                  {formatBytes(selectedNote.size_bytes)} · Updated {formatDateTime(selectedNote.modified_at)}
                </p>
              </div>
              <button type="button" className="button danger sm" disabled={deleting} onClick={() => void deleteSelectedNote()}>
                <Icon name="trash" size={12} />
                {deleting ? 'Deleting…' : 'Delete'}
              </button>
            </header>
            <MarkdownViewer content={selectedNote.content} />
          </>
        ) : null}
        {!contentLoading && !selectedNote ? (
          <EmptyState icon="notes" title="Select a note" description="Pick a Markdown file from the tree to read it here." />
        ) : null}
      </section>
    </div>
  );
}
