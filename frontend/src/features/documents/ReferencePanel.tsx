import { useEffect, useState } from 'react';
import { json, request } from '../../api/client';
import type { components } from '../../api/schema.generated';
import { useNavigationGuard } from '../../app/router';
import { ErrorNotice } from '../../shared/components/Ui';
import './references.css';

export type ReferenceFields = Required<components['schemas']['ReferenceFields']>;
export type ReferenceRecord = Required<components['schemas']['Reference']>;
export type ReferenceLibrary = Omit<Required<components['schemas']['ReferenceLibrary']>, 'references'> & { references: ReferenceRecord[] };
const empty = (): ReferenceFields => ({
  citation_key: '', title: '', authors: [], year: '', doi: '', arxiv_id: '', journal: '', volume: '',
  number: '', pages: '', publisher: '', url: '', abstract: '', entry_type: 'article',
  document_id: null, related_reference_ids: [],
});
const textFields = ['citation_key', 'title', 'year', 'doi', 'arxiv_id', 'journal', 'volume', 'number', 'pages', 'publisher', 'url'] as const;
const labels: Record<typeof textFields[number], string> = {
  citation_key: 'Citation key', title: 'Reference title', year: 'Year', doi: 'DOI', arxiv_id: 'arXiv ID',
  journal: 'Journal / proceedings', volume: 'Volume', number: 'Issue', pages: 'Pages', publisher: 'Publisher', url: 'URL',
};

export function ReferencePanel({ papers, onGuardChange }: {
  papers: { id: string; title: string }[];
  onGuardChange?: (state: { dirty: boolean; busy: boolean }) => void;
}) {
  const [library, setLibrary] = useState<ReferenceLibrary | null>(null);
  const [draft, setDraft] = useState<ReferenceFields>(empty);
  const [editing, setEditing] = useState<string | null>(null);
  const [authors, setAuthors] = useState('');
  const [format, setFormat] = useState<'bibtex' | 'ris'>('bibtex');
  const [content, setContent] = useState('');
  const [exported, setExported] = useState('');
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState('');
  const [message, setMessage] = useState('');
  const [base, setBase] = useState<ReferenceFields>(empty);
  const [draftRevision, setDraftRevision] = useState<number | null>(null);
  const current = { ...draft, authors: authors.split('\n').map((value) => value.trim()).filter(Boolean) };
  const draftDirty = JSON.stringify(current) !== JSON.stringify(base);
  const dirty = draftDirty || !!content;
  const stale = draftRevision !== null && library !== null && draftRevision !== library.revision;
  const latest = library?.references.find((record) => record.id === editing);
  useNavigationGuard(dirty || busy, 'Discard unsaved reference changes and leave?', !busy);
  useEffect(() => { onGuardChange?.({ dirty, busy }); }, [dirty, busy, onGuardChange]);
  useEffect(() => () => { onGuardChange?.({ dirty: false, busy: false }); }, [onGuardChange]);
  const load = async () => {
    const next = await request<ReferenceLibrary>('/references');
    setLibrary(next);
    if (draftRevision === null) setDraftRevision(next.revision);
  };
  useEffect(() => { load().catch(setError); }, []);

  function select(record?: ReferenceRecord, force = false, revision = library?.revision ?? null) {
    if (!force && draftDirty && !window.confirm('Discard unsaved reference metadata changes?')) return;
    if (record) {
      const { id, ...fields } = record;
      setEditing(id); setDraft(fields); setBase(fields); setAuthors(record.authors.join('\n'));
    } else {
      setEditing(null); setDraft(empty()); setBase(empty()); setAuthors('');
    }
    setDraftRevision(revision);
    setMessage('');
  }
  function rebase() {
    if (!library || (editing && !latest)) return;
    const { id: _id, ...remote } = latest ?? { id: '', ...empty() };
    const merged = { ...remote };
    for (const field of Object.keys(current) as (keyof ReferenceFields)[]) {
      if (JSON.stringify(current[field]) !== JSON.stringify(base[field])) {
        Object.assign(merged, { [field]: current[field] });
      }
    }
    setDraft(merged); setAuthors(merged.authors.join('\n')); setBase(remote);
    setDraftRevision(library.revision);
    setMessage('Draft rebased: your edited fields are retained; unchanged fields use the latest record. Review and save explicitly.');
  }
  async function execute(action: () => Promise<void>) {
    setBusy(true); setError(null); setMessage('');
    try { await action(); } catch (failure) { setError(failure); } finally { setBusy(false); }
  }
  async function save() {
    if (!library || stale) return;
    const next = await request<ReferenceLibrary>(editing ? `/references/${encodeURIComponent(editing)}` : '/references',
      json(editing ? 'PUT' : 'POST', {
        expected_revision: draftRevision, reference: current,
      }));
    setLibrary(next); select(next.references.find((record) => record.citation_key === draft.citation_key), true, next.revision);
    setMessage('Reference saved. Citation key remains stable.');
  }
  return <section className="reference-panel" aria-label="Reference management">
    <header className="row"><h2>References</h2><button className="button small secondary" disabled={busy} onClick={() => void execute(load)}>Reload references</button></header>
    <p className="muted">Manage metadata without downloading a PDF. DOI, arXiv and linked-paper duplicates stop the entire import; nothing is merged automatically.</p>
    {error ? <ErrorNotice error={error} /> : null}
    {message ? <p role="status">{message}</p> : null}
    {stale ? <section aria-label="Compare reference versions">
      <h3>References changed — compare before saving</h3>
      <p>Your draft has not been replaced. Rebasing retains your edited fields (including conflicts) and takes latest values for untouched fields. Nothing is saved until you press Save.</p>
      <div className="reference-columns">
        <label>Your draft<textarea readOnly value={JSON.stringify(current, null, 2)} /></label>
        <label>Latest saved metadata<textarea readOnly value={JSON.stringify(latest ?? null, null, 2)} /></label>
      </div>
      <button className="button secondary" disabled={busy || (!!editing && !latest)} onClick={rebase}>Rebase my edited fields onto latest</button>
      <button className="button secondary" disabled={busy} onClick={() => {
        if (!draftDirty || window.confirm('Discard your metadata draft and use the latest saved version?')) select(latest, true);
      }}>Discard draft and use latest</button>
    </section> : null}
    <div className="reference-columns">
      <aside>
        <input aria-label="Search references" placeholder="Title, key, author or DOI" value={query} onChange={(event) => setQuery(event.target.value)} />
        <button className="button small" disabled={busy} onClick={() => select()}>New reference</button>
        <ul className="reference-list">
          {library?.references.filter((record) => [record.title, record.citation_key, record.doi, ...record.authors].join(' ').toLowerCase().includes(query.toLowerCase())).map((record) =>
            <li key={record.id}><button className="button ghost" aria-pressed={editing === record.id} disabled={busy} onClick={() => select(record)}>
              <span>{record.title}<small>[@{record.citation_key}] · {record.year || 'No year'}{record.document_id ? ' · Linked PDF' : ''}</small></span>
            </button></li>)}
        </ul>
      </aside>
      <form onSubmit={(event) => { event.preventDefault(); void execute(save); }}>
        <fieldset disabled={busy || !library}>
          <legend>{editing ? 'Edit bibliographic metadata' : 'New bibliographic reference'}</legend>
          <div className="reference-fields">{textFields.map((field) => <label key={field}>{labels[field]}
            <input value={draft[field]} required={field === 'title' || field === 'citation_key'} readOnly={field === 'citation_key' && !!editing}
              onChange={(event) => setDraft({ ...draft, [field]: event.target.value })} />
          </label>)}</div>
          <label>Authors (one per line)<textarea value={authors} onChange={(event) => setAuthors(event.target.value)} /></label>
          <label>Abstract<textarea value={draft.abstract} onChange={(event) => setDraft({ ...draft, abstract: event.target.value })} /></label>
          <label>Reference type<select value={draft.entry_type} onChange={(event) => setDraft({ ...draft, entry_type: event.target.value as ReferenceFields['entry_type'] })}>
            {['article', 'book', 'inproceedings', 'misc', 'phdthesis', 'techreport'].map((type) => <option key={type}>{type}</option>)}
          </select></label>
          <label>Linked paper<select value={draft.document_id ?? ''} onChange={(event) => {
            const paper = papers.find((item) => item.id === event.target.value);
            setDraft({ ...draft, document_id: paper?.id ?? null, title: draft.title || paper?.title || '' });
          }}><option value="">Metadata only (no PDF)</option>{papers.map((paper) => <option key={paper.id} value={paper.id}>{paper.title}</option>)}</select></label>
          <label>Related versions (select preprint / published reference)<select multiple value={draft.related_reference_ids} onChange={(event) =>
            setDraft({ ...draft, related_reference_ids: Array.from(event.target.selectedOptions, (option) => option.value) })}>
            {library?.references.filter((record) => record.id !== editing).map((record) => <option key={record.id} value={record.id}>{record.citation_key}: {record.title}</option>)}
          </select></label>
          <button className="button" type="submit" disabled={stale}>Save reference</button>
        </fieldset>
      </form>
    </div>
    <details><summary>Import / export BibTeX or RIS</summary>
      <p className="muted">Supported: articles, books, proceedings, miscellaneous works, theses and reports. BibTeX accepts braced/quoted text and numeric values, not macros or LaTeX commands. RIS uses single-line fields and requires TY / ER records. Unsupported fields are rejected explicitly; imports are atomic. Local PDF/version links are not part of interchange files.</p>
      <label>Reference format<select value={format} disabled={busy} onChange={(event) => { setFormat(event.target.value as 'bibtex' | 'ris'); setExported(''); }}>
        <option value="bibtex">BibTeX</option><option value="ris">RIS</option>
      </select></label>
      <label>Reference file<input type="file" accept=".bib,.ris,text/plain" disabled={busy} onChange={(event) => {
        const file = event.target.files?.[0];
        if (file) void execute(async () => { setContent(await file.text()); });
      }} /></label>
      <label>Import content<textarea value={content} onChange={(event) => setContent(event.target.value)} /></label>
      <button className="button secondary" disabled={busy || !library || !content.trim()} onClick={() => void execute(async () => {
        const next = await request<ReferenceLibrary>('/references/import', json('POST', { expected_revision: library!.revision, format, content }));
        setLibrary(next); setContent(''); setMessage('Import completed. No PDFs downloaded.');
      })}>Import references</button>
      <button className="button secondary" disabled={busy || !library} onClick={() => void execute(async () => {
        const result = await request<{ content: string }>('/references/export', json('POST', { format, reference_ids: null }));
        setExported(result.content); setMessage(`Exported ${library!.references.length} references.`);
      })}>Export all references</button>
      {exported ? <><label>Export content<textarea readOnly value={exported} /></label>
        <a className="button secondary" download={`references.${format === 'bibtex' ? 'bib' : 'ris'}`} href={`data:text/plain;charset=utf-8,${encodeURIComponent(exported)}`}>Download {format === 'bibtex' ? '.bib' : '.ris'}</a></> : null}
    </details>
  </section>;
}
