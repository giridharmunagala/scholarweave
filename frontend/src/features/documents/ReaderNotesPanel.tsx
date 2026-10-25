import { useEffect, useState } from 'react';
import { json, request } from '../../api/client';
import type { components } from '../../api/schema.generated';
import { Link, useNavigationGuard } from '../../app/router';
import { ErrorNotice, Loading } from '../../shared/components/Ui';
import './readerNotes.css';

type ReaderNote = components['schemas']['ReaderNoteResponse'];
type NotePage = components['schemas']['ReaderNotePage'];
const noteUrl = (note: ReaderNote) => `/library?${new URLSearchParams({
  document: note.paper_id, page: String(note.page), highlight: note.id,
})}`;

export function ReaderNotesPanel({ paperId, trackId, revision, highlightId, onChanged }: {
  paperId?: string;
  trackId?: string;
  revision?: string;
  highlightId?: string;
  onChanged?: () => Promise<unknown>;
}) {
  const [query, setQuery] = useState('');
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<NotePage | null>(null);
  const [version, setVersion] = useState(0);
  const [focused, setFocused] = useState(highlightId);
  const [connectedTo, setConnectedTo] = useState<ReaderNote | null>(null);
  const [source, setSource] = useState<ReaderNote | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState('');
  useNavigationGuard(busy, 'Wait for the note connection to finish.', !busy);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    const params = new URLSearchParams({ limit: '25', offset: String(offset) });
    if (query.trim()) params.set('query', query.trim());
    if (connectedTo) params.set('connected_to', connectedTo.id);
    else if (!source) {
      if (paperId) params.set('paper_id', paperId);
      if (focused) params.set('note_id', focused);
    }
    const base = trackId && !source && !connectedTo ? `/tracks/${encodeURIComponent(trackId)}/reader-notes` : '/workspace/reader-notes';
    request<NotePage>(`${base}?${params}`).then((result) => { if (active) setPage(result); })
      .catch((reason) => { if (active) { setPage(null); setError(reason); } })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [paperId, trackId, query, offset, version, revision, focused, connectedTo?.id, source?.id]);

  const reset = () => {
    setSource(null); setConnectedTo(null); setFocused(undefined); setQuery(''); setOffset(0);
    setVersion((value) => value + 1);
  };
  const connect = async (target: ReaderNote) => {
    if (!source || busy) return;
    setBusy(true); setError(null); setNotice('');
    try {
      await request('/workspace/reader-notes/connections', json('POST', {
        source_id: source.id, target_id: target.id, expected_sha256: source.sha256,
      } satisfies components['schemas']['ReaderNoteConnectionRequest']));
      reset();
      setNotice('Highlights connected. Both directions are included in note search.');
      await onChanged?.();
    } catch (reason) { setError(reason); } finally { setBusy(false); }
  };

  return <section className="reader-notes-panel" aria-label="Reader highlights">
    <header className="button-row">
      <h3>{source ? 'Connect a highlight' : connectedTo ? 'Connected highlights' : 'Reader highlights'}</h3>
      <button type="button" className="button ghost small" disabled={busy} onClick={reset}>
        {source || connectedTo || focused ? 'Show all highlights' : 'Refresh highlights'}
      </button>
    </header>
    <p className="field-hint">Your page-attributed reading notes, not generated summaries. Search also includes directly connected highlights.</p>
    {source ? <p>Connect <q>{source.text}</q> to a highlight from any paper.</p> : null}
    {connectedTo ? <p>Connected to <q>{connectedTo.text}</q>.</p> : null}
    <input type="search" aria-label={source ? 'Find a highlight to connect' : 'Search reader highlights'} value={query}
      disabled={busy} placeholder="Search your highlights..." onChange={(event) => {
        setQuery(event.target.value); setOffset(0); setFocused(undefined);
      }} />
    {error ? <ErrorNotice error={error} /> : null}
    {notice ? <p role="status">{notice}</p> : null}
    {loading ? <Loading label="Loading highlights..." /> : !page?.items.length && !error
      ? <p className="muted">No reader highlights found. Select a passage in a paper and choose Add to notes.</p> : null}
    {!loading && page ? <ul className="reader-highlight-list">{page.items.filter((note) => note.id !== source?.id).map((note) =>
      <li key={note.id}>
        <Link to={noteUrl(note)}>{note.paper_name} · p. {note.page}</Link>
        <blockquote>{note.text}</blockquote>
        {note.match_type === 'connected' ? <span className="tag">Via a connected highlight</span> : null}
        <div className="button-row">
          {source ? <button type="button" className="button secondary small" disabled={busy || source.connection_ids.includes(note.id)}
            onClick={() => void connect(note)}>{source.connection_ids.includes(note.id) ? 'Already connected' : 'Connect this highlight'}</button>
            : <button type="button" className="button secondary small" disabled={busy} onClick={() => {
              setSource(note); setConnectedTo(null); setQuery(''); setOffset(0); setNotice('');
            }}>Connect highlight</button>}
          {!source && note.connection_ids.length ? <button type="button" className="button ghost small" disabled={busy} onClick={() => {
            setConnectedTo(note); setQuery(''); setOffset(0); setFocused(undefined);
          }}>{note.connection_ids.length} connections</button> : null}
        </div>
      </li>)}</ul> : null}
    {page && (offset > 0 || page.next_offset !== null) ? <div className="button-row">
      <button type="button" className="button secondary small" disabled={busy || loading || offset === 0} onClick={() => setOffset(Math.max(0, offset - 25))}>Previous highlights</button>
      <button type="button" className="button secondary small" disabled={busy || loading || page.next_offset === null}
        onClick={() => { if (page.next_offset !== null) setOffset(page.next_offset); }}>Next highlights</button>
    </div> : null}
  </section>;
}
