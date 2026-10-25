import { useCallback, useEffect, useRef, useState } from 'react';
import { json, request } from '../../api/client';
import type { components } from '../../api/schema.generated';
import { Link, useLocation, useNavigationGuard } from '../../app/router';
import { Icon } from '../../shared/components/Icons';
import { MarkdownViewer } from '../../shared/components/MarkdownViewer';
import type { Quote } from './PdfReader';
import { ReaderNotesPanel } from './ReaderNotesPanel';

type SearchHit = components['schemas']['WorkspaceSearchResponse'];
type FileContent = components['schemas']['WorkspaceFileContentResponse'];

interface NotesState { paperId: string; path: string; content: string; sha256: string | null }

/** The canonical notes.md that lives beside each paper in the workspace. */
export function usePaperNotes(paperId: string | null) {
  const [loadedNotes, setNotes] = useState<NotesState | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const currentScope = useRef({ paperId, active: true });
  if (currentScope.current.paperId !== paperId) currentScope.current = { paperId, active: true };
  const scope = currentScope.current;
  const requestId = useRef(0);
  const notes = loadedNotes?.paperId === paperId ? loadedNotes : null;
  const assertCurrent = () => {
    if (!paperId || currentScope.current !== scope || !scope.active) throw new Error('The open paper has changed. Reopen its notes before editing.');
  };
  const apply = (file: FileContent, id: number) => {
    const next = { paperId: paperId!, path: file.path, content: String(file.content ?? ''), sha256: file.sha256 ?? null };
    if (currentScope.current === scope && scope.active && requestId.current === id) {
      setNotes(next);
      setLoading(false);
      setError(null);
    }
    return next;
  };

  const load = useCallback(async () => {
    if (!paperId) { setLoading(false); return null; }
    assertCurrent();
    const id = ++requestId.current;
    const active = () => currentScope.current === scope && scope.active && requestId.current === id;
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({ kinds: 'paper_notes', tags: `paper:${paperId}`, limit: '1' });
      const [hit] = await request<SearchHit[]>(`/workspace/search?${params}`);
      if (!hit) {
        if (active()) setNotes(null);
        return null;
      }
      const file = await request<FileContent>(`/workspace/files/content?${new URLSearchParams({ path: hit.path })}`);
      return apply(file, id);
    } catch (reason) {
      if (active()) setError(reason instanceof Error ? reason.message : String(reason));
      throw reason;
    } finally {
      if (active()) setLoading(false);
    }
  }, [scope]);

  useEffect(() => {
    scope.active = true;
    setNotes(null);
    setError(null);
    void load().catch(() => undefined);
    return () => { scope.active = false; };
  }, [load]);

  const ensure = useCallback(async () => {
    assertCurrent();
    const current = notes ?? (await load());
    assertCurrent();
    if (current) return current;
    const id = ++requestId.current;
    return apply(await request<FileContent>(`/documents/${encodeURIComponent(paperId!)}/notes`, { method: 'POST' }), id);
  }, [notes, load]);

  const append = useCallback(async (text: string) => {
    const current = await ensure();
    assertCurrent();
    if (!current.sha256) throw new Error('Reload the notes before editing: their revision is missing.');
    const id = ++requestId.current;
    const separator = current.content.endsWith('\n') ? '\n' : '\n\n';
    return apply(await request<FileContent>('/workspace/files/content', json('PATCH', {
      path: current.path, operation: 'append', content: `${separator}${text}\n`, selection: null, expected_sha256: current.sha256,
    })), id);
  }, [ensure]);

  const save = useCallback(async (base: NotesState, content: string) => {
    assertCurrent();
    if (base.paperId !== paperId) throw new Error('This draft belongs to another paper.');
    if (!base.sha256) throw new Error('Reload the notes before editing: their revision is missing.');
    const id = ++requestId.current;
    return apply(await request<FileContent>('/workspace/files/content', json('PUT', {
      path: base.path, content, expected_sha256: base.sha256,
    })), id);
  }, [scope]);

  return { paperId, notes, loading, error, reload: load, ensure, append, save };
}

export function quoteMarkdown(quote: Quote): string {
  return `<!-- scholarweave-highlight:${crypto.randomUUID()} -->\n${quote.text.split(/\r?\n/).map((line) => `> ${line}`).join('\n')}\n>\n> — p. ${quote.page}`;
}

export function PaperNotesDrawer({
  paperNotes,
  onClose,
  open = true,
}: {
  paperNotes: ReturnType<typeof usePaperNotes>;
  onClose: () => void;
  open?: boolean;
}) {
  const { paperId, notes, loading, error, save, ensure } = paperNotes;
  const { searchParams } = useLocation();
  const highlightId = searchParams.get('highlight') ?? undefined;
  const [showHighlights, setShowHighlights] = useState(!!highlightId);
  useEffect(() => { setShowHighlights(!!highlightId); }, [paperId, highlightId]);
  const [draft, setDraft] = useState<{ base: NotesState; content: string } | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const operation = useRef(0);
  const busy = useRef(false);
  const editingDraft = draft?.base.paperId === paperId ? draft : null;
  const dirty = !!editingDraft && editingDraft.content !== editingDraft.base.content;
  useNavigationGuard(dirty || saving, 'Discard unsaved paper notes and leave this draft?', !saving);
  useEffect(() => {
    setDraft(null);
    setSaveError(null);
    setSaving(false);
    busy.current = false;
    return () => { operation.current += 1; };
  }, [paperId]);

  const edit = (base: NotesState) => {
    setDraft({ base, content: base.content });
    setSaveError(null);
  };
  const execute = async (action: (id: number) => Promise<void>) => {
    if (busy.current) return;
    busy.current = true;
    const id = ++operation.current;
    setSaving(true);
    setSaveError(null);
    try {
      await action(id);
    } catch (reason) {
      if (operation.current === id) setSaveError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      if (operation.current === id) { busy.current = false; setSaving(false); }
    }
  };

  const commit = async () => {
    if (!editingDraft) return;
    await execute(async (id) => {
      await save(editingDraft.base, editingDraft.content);
      if (operation.current === id) setDraft(null);
    });
  };
  const create = async () => {
    await execute(async (id) => {
      const created = await ensure();
      if (operation.current === id) edit(created);
    });
  };

  if (!open) return null;

  return (
    <aside className="notes-drawer" aria-label="Paper notes">
      <header>
        <strong>Notes</strong>
        <span className="notes-drawer-actions">
          {notes && !editingDraft ? <button type="button" className="button small secondary" disabled={saving} onClick={() => edit(notes)}>Edit</button> : null}
          {editingDraft ? (
            <>
              <button type="button" className="button small" disabled={saving} onClick={() => void commit()}>{saving ? 'Saving…' : 'Save'}</button>
              <button type="button" className="button small secondary" disabled={saving} onClick={() => {
                if (!dirty || window.confirm('Discard unsaved paper notes?')) { setDraft(null); setSaveError(null); }
              }}>Cancel</button>
            </>
          ) : null}
          {notes ? <Link className="icon-button" to={`/library/notes?${new URLSearchParams({ path: notes.path })}`} title="Open in Notes" aria-label="Open in Notes"><Icon name="expand" size={13} /></Link> : null}
          <button type="button" className="icon-button" aria-label="Close notes" onClick={onClose}><Icon name="close" size={13} /></button>
        </span>
      </header>
      {error || saveError ? <p className="notice error">{saveError ?? error}</p> : null}
      {loading && !notes ? <p className="muted">Loading notes…</p> : null}
      {!loading && !notes && !error ? (
        <div>
          <p className="muted">No notes file exists for this paper yet.</p>
          <button type="button" className="button small" disabled={saving || !paperId} onClick={() => void create()}>{saving ? 'Creating…' : 'Create notes'}</button>
        </div>
      ) : null}
      {editingDraft ? (
        <textarea
          className="notes-editor"
          aria-label="Edit notes"
          value={editingDraft.content}
          disabled={saving}
          autoFocus
          onChange={(event) => setDraft({ ...editingDraft, content: event.target.value })}
          onKeyDown={(event) => { if ((event.ctrlKey || event.metaKey) && event.key === 's') { event.preventDefault(); void commit(); } }}
        />
      ) : null}
      {notes && !editingDraft ? (
        <>
          <div className="notes-body" onDoubleClick={() => { if (!saving) edit(notes); }}>
            <MarkdownViewer content={notes.content} />
            <p className="notes-tip muted">Select text in the paper and choose <em>Add to notes</em> to collect quotes here.</p>
          </div>
          <button type="button" className="button secondary small" aria-expanded={showHighlights}
            onClick={() => setShowHighlights((value) => !value)}>Highlights &amp; connections</button>
          {showHighlights && paperId ? <ReaderNotesPanel key={`${paperId}:${highlightId ?? ''}`} paperId={paperId}
            revision={notes.sha256 ?? undefined} highlightId={highlightId} onChanged={paperNotes.reload} /> : null}
        </>
      ) : null}
    </aside>
  );
}
