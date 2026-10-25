import { useEffect, useState } from 'react';
import { ApiError, apiUrl, json, request } from '../../api/client';
import type { components } from '../../api/schema.generated';
import { Link, useNavigationGuard } from '../../app/router';
import { MarkdownViewer } from '../../shared/components/MarkdownViewer';
import { ErrorNotice, Loading } from '../../shared/components/Ui';
import type { Track } from './api';
import './writing.css';

type Claims = components['schemas']['ClaimCollection'];
type Claim = components['schemas']['ResearchClaim'];
type Evidence = components['schemas']['EvidenceInput'];
type Manuscript = components['schemas']['ManuscriptResponse'];
type Audit = components['schemas']['ManuscriptAudit'];
type Page = components['schemas']['EvidencePageResponse'];
type Paper = Pick<components['schemas']['DocumentSummaryResponse'], 'id' | 'title'>;
type ReferenceOption = components['schemas']['Reference'];
const base = (id: string) => `/tracks/${encodeURIComponent(id)}/writing`;

export function WritingPanel({ track, papers, onManuscriptSaved, onOpenManuscript }: {
  track: Track;
  papers: Paper[];
  onManuscriptSaved?: () => Promise<void>;
  onOpenManuscript?: () => void;
}) {
  const [claims, setClaims] = useState<Claims | null>(null);
  const [manuscript, setManuscript] = useState<Manuscript | null>(null);
  const [content, setContent] = useState('');
  const [references, setReferences] = useState<ReferenceOption[]>([]);
  const [referenceKey, setReferenceKey] = useState('');
  const [claimId, setClaimId] = useState<string | null>(null);
  const [claimRevision, setClaimRevision] = useState<string | null>(null);
  const [statement, setStatement] = useState('');
  const [interpretation, setInterpretation] = useState('');
  const [evidence, setEvidence] = useState<Evidence[]>([]);
  const [documentId, setDocumentId] = useState('');
  const [pageNumber, setPageNumber] = useState(1);
  const [page, setPage] = useState<Page | null>(null);
  const [quote, setQuote] = useState('');
  const [relation, setRelation] = useState<Evidence['relation']>('supports');
  const [audit, setAudit] = useState<Audit | null>(null);
  const [latest, setLatest] = useState<Manuscript | null>(null);
  const [preview, setPreview] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState('');
  const [loadAttempt, setLoadAttempt] = useState(0);
  const dirty = manuscript !== null && manuscript.content !== content;
  useNavigationGuard(dirty || !!statement || !!interpretation || evidence.length > 0 || busy,
    'Leave writing? Unsaved manuscript and claim drafts will be lost.', !busy);

  useEffect(() => {
    let active = true;
    setError(null);
    Promise.all([
      request<Claims>(`${base(track.id)}/claims`),
      request<Manuscript>(`${base(track.id)}/manuscript`),
      request<components['schemas']['ReferenceLibrary']>('/references'),
    ]).then(([savedClaims, draft, refs]) => {
      if (!active) return;
      setClaims(savedClaims); setManuscript(draft); setContent(draft.content); setReferences(refs.references ?? []);
    }).catch((reason) => { if (active) setError(reason); });
    return () => { active = false; };
  }, [track.id, loadAttempt]);

  async function perform(action: () => Promise<void>) {
    if (busy) return;
    setBusy(true); setError(null); setNotice('');
    try { await action(); } catch (reason) { setError(reason); } finally { setBusy(false); }
  }
  function clearClaim() {
    setClaimId(null); setClaimRevision(null); setStatement(''); setInterpretation(''); setEvidence([]); setQuote('');
  }
  function editClaim(claim: Claim) {
    if ((statement || interpretation || evidence.length) && !window.confirm('Replace the current claim draft?')) return;
    setClaimId(claim.id); setClaimRevision(claims?.sha256 ?? null);
    setStatement(claim.statement); setInterpretation(claim.interpretation);
    setEvidence(claim.evidence.map(({ document_id, page: number, quote: text, relation: kind }) =>
      ({ document_id, page: number, quote: text, relation: kind })));
  }
  function insertClaim(claim: Claim) {
    const keys = [...new Set(claim.evidence.filter((item) => item.relation === 'supports')
      .flatMap((item) => references.filter((ref) => ref.document_id === item.document_id).map((ref) => ref.citation_key)))];
    setContent((text) => `${text}${text ? '\n\n' : ''}${claim.statement} [claim:${claim.id}]${keys.map((key) => ` [@${key}]`).join('')}`);
    setAudit(null);
  }
  async function download(format: 'markdown' | 'latex' | 'docx') {
    const response = await fetch(apiUrl(`${base(track.id)}/export?format=${format}`));
    if (!response.ok) {
      const detail: unknown = await response.json();
      throw new ApiError(response.status, typeof detail === 'object' && detail && 'message' in detail
        ? String(detail.message) : 'Export failed. Resolve missing references or claims and try again.');
    }
    const url = URL.createObjectURL(await response.blob());
    const anchor = document.createElement('a');
    anchor.href = url; anchor.download = `manuscript.${format === 'markdown' ? 'md' : format === 'latex' ? 'tex' : 'docx'}`;
    document.body.append(anchor); anchor.click(); anchor.remove(); URL.revokeObjectURL(url);
  }

  return <section className="writing-workspace" aria-label="Claims and writing">
    <ErrorNotice error={error} />
    {notice ? <p role="status" className="notice info">{notice}</p> : null}
    {!claims || !manuscript ? error
      ? <button className="button secondary small" onClick={() => setLoadAttempt((value) => value + 1)}>Retry loading writing workspace</button>
      : <Loading label="Loading claims and manuscript..." /> : <>
      <section className="panel writing-claims">
        <h3>Traceable claims</h3>
        <p className="muted">Keep statements, your interpretation, and supporting or contradictory passages separate. A matching quote verifies attribution, not the truth of a claim.</p>
        <div className="button-row">
          <button className="button secondary small" disabled={busy} onClick={() => void perform(async () => {
            setClaims(await request<Claims>(`${base(track.id)}/claims`));
            setReferences((await request<components['schemas']['ReferenceLibrary']>('/references')).references ?? []);
            setNotice('Source status and references refreshed. Your drafts are unchanged.');
          })}>Refresh evidence and references</button>
          <Link className="button ghost small" to="/library">Manage papers and references</Link>
        </div>
        <div className="writing-claim-list">
          {claims.claims.map((claim) => <article className="writing-claim" id={`writing-claim-${claim.id}`} key={claim.id}>
            <h4>{claim.statement}</h4>
            {claim.interpretation ? <p><strong>Researcher interpretation:</strong> {claim.interpretation}</p> : null}
            {!claim.evidence.length ? <p className="muted">No source evidence attached.</p> : null}
            {claim.evidence.map((item, index) => <details key={index}>
              <summary>{item.relation} · {item.paper_title} · p. {item.page} · {item.source_status}</summary>
              <blockquote>{item.quote}</blockquote>
              <p className="muted">Source version: <code>{item.source_version}</code></p>
              <Link to={`/library?document=${encodeURIComponent(item.document_id)}&page=${item.page}`}>Open original paper</Link>
              <button className="button ghost small" disabled={busy} onClick={() => void perform(async () => {
                const current = await request<Page>(`${base(track.id)}/page?${new URLSearchParams({ document_id: item.document_id, page: String(item.page) })}`);
                setDocumentId(item.document_id); setPageNumber(item.page); setPage(current); setQuote('');
              })}>Inspect current source page</button>
            </details>)}
            <div className="button-row">
              <button className="button secondary small" disabled={busy} onClick={() => insertClaim(claim)}>Insert into manuscript</button>
              <button className="button ghost small" disabled={busy} onClick={() => editClaim(claim)}>Edit claim</button>
              <button className="button ghost small" disabled={busy} onClick={() => {
                if (!window.confirm('Delete this claim? Existing manuscript links will be marked unresolved.')) return;
                void perform(async () => {
                  setClaims(await request<Claims>(`${base(track.id)}/claims/${claim.id}`, json('DELETE', { expected_sha256: claims.sha256 })));
                  setAudit(null);
                });
              }}>Delete claim</button>
            </div>
          </article>)}
        </div>
        <h4>{claimId ? 'Edit claim and recheck its source passages' : 'Add a claim'}</h4>
        {claimId && claimRevision !== claims.sha256 ? <div role="status" className="notice">
          <p>The claim collection changed after you opened this draft. Review the current saved claim above before reconciling.</p>
          <button className="button secondary small" disabled={busy} onClick={() => {
            if (window.confirm('Use the displayed claim collection as the save base? Your draft stays unchanged. Reconcile it with the current claim before saving.')) setClaimRevision(claims.sha256);
          }}>Use displayed claims as save base</button>
        </div> : null}
        <label>Claim statement<textarea aria-label="Claim statement" value={statement} disabled={busy} maxLength={5000} onChange={(event) => setStatement(event.target.value)} /></label>
        <label>Researcher interpretation<textarea aria-label="Researcher interpretation" value={interpretation} disabled={busy} maxLength={10000} onChange={(event) => setInterpretation(event.target.value)} /></label>
        <div className="writing-source-picker">
          <label>Source paper<select aria-label="Claim source paper" value={documentId} disabled={busy} onChange={(event) => { setDocumentId(event.target.value); setPage(null); setQuote(''); }}>
            <option value="">Choose a prepared library paper</option>
            {papers.map((paper) => <option key={paper.id} value={paper.id}>{paper.title}</option>)}
          </select></label>
          <label>Page<input aria-label="Source page number" type="number" min={1} value={pageNumber} disabled={busy} onChange={(event) => { setPageNumber(Number(event.target.value)); setPage(null); setQuote(''); }} /></label>
          <button className="button secondary small" disabled={busy || !documentId || !Number.isInteger(pageNumber) || pageNumber < 1} onClick={() => void perform(async () => {
            setPage(await request<Page>(`${base(track.id)}/page?${new URLSearchParams({ document_id: documentId, page: String(pageNumber) })}`));
          })}>Read source page</button>
        </div>
        {page ? <div className="writing-source">
          <h4>{page.title}, p. {page.page}</h4>
          <pre tabIndex={0}>{page.text}</pre>
          <label>Exact quotation<textarea aria-label="Evidence quotation" value={quote} maxLength={10000} disabled={busy} onChange={(event) => setQuote(event.target.value)} /></label>
          <label>Relationship<select aria-label="Evidence relationship" value={relation} disabled={busy} onChange={(event) => {
            const value = event.target.value;
            if (value === 'supports' || value === 'contradicts' || value === 'context') setRelation(value);
          }}><option value="supports">Supports</option><option value="contradicts">Contradicts</option><option value="context">Context only</option></select></label>
          <button className="button secondary small" disabled={busy || !quote.trim() || evidence.length >= 30} onClick={() => {
            setEvidence((items) => [...items, { document_id: page.document_id, page: page.page, quote: quote.trim(), relation }]); setQuote('');
          }}>Attach passage to draft</button>
        </div> : null}
        {evidence.map((item, index) => <div className="writing-evidence-draft" key={index}>
          <span>{item.relation} · p. {item.page}: {item.quote}</span>
          <button className="button ghost small" disabled={busy} onClick={() => setEvidence((items) => items.filter((_, i) => i !== index))}>Remove passage {index + 1}</button>
        </div>)}
        <div className="button-row">
          <button className="button" disabled={busy || !statement.trim()} onClick={() => void perform(async () => {
            setClaims(await request<Claims>(`${base(track.id)}/claims${claimId ? `/${claimId}` : ''}`, json(claimId ? 'PUT' : 'POST', {
              statement, interpretation, evidence, expected_sha256: claimId ? claimRevision : claims.sha256,
            })));
            clearClaim(); setAudit(null); setNotice('Claim saved. Attached quotations were checked against their source pages.');
          })}>Save claim</button>
          <button className="button ghost small" disabled={busy} onClick={() => { if (window.confirm('Discard the claim draft?')) clearClaim(); }}>Discard claim draft</button>
        </div>
      </section>
      <section className="panel writing-manuscript">
        <h3>Manuscript</h3>
        <p className="muted">A track-linked Markdown document. Use headings for sections, [@citation_key] for references, and insert claims to preserve evidence links. Saved versions appear under Exploration / manuscript.</p>
        <div className="button-row">
          <button className="button secondary small" disabled={busy} onClick={() => setPreview(!preview)}>{preview ? 'Edit manuscript' : 'Preview manuscript'}</button>
          <button className="button secondary small" disabled={busy || content.trim().length > 0} onClick={() => setContent(`# ${track.title}\n\n## Abstract\n\n## Introduction\n\n## Related work\n\n## Methods\n\n## Results\n\n## Discussion\n\n## Limitations\n`)}>Start with outline</button>
          <select aria-label="Insert reference" value={referenceKey} disabled={busy} onChange={(event) => setReferenceKey(event.target.value)}>
            <option value="">Choose a reference</option>{references.map((ref) => <option key={ref.citation_key} value={ref.citation_key}>{ref.citation_key} — {ref.title}</option>)}
          </select>
          <button className="button secondary small" disabled={busy || !referenceKey} onClick={() => { setContent((text) => `${text} [@${referenceKey}]`); setAudit(null); }}>Insert citation</button>
        </div>
        {preview ? <MarkdownViewer content={content} workspacePath={manuscript.path} />
          : <textarea className="track-textarea" aria-label="Manuscript Markdown" value={content} disabled={busy} onChange={(event) => { setContent(event.target.value); setAudit(null); }} />}
        {claims.claims.some((claim) => content.includes(`[claim:${claim.id}]`)) ? <div className="button-row" aria-label="Inspect manuscript evidence">
          <strong>Inspect linked evidence:</strong>
          {claims.claims.filter((claim) => content.includes(`[claim:${claim.id}]`)).map((claim) =>
            <a key={claim.id} href={`#writing-claim-${claim.id}`}>{claim.statement}</a>)}
        </div> : null}
        <div className="button-row">
          <button className="button" disabled={busy || !dirty} onClick={() => void perform(async () => {
            const saved = await request<Manuscript>(`${base(track.id)}/manuscript`, json('PUT', { content, expected_sha256: manuscript.sha256 }));
            setManuscript(saved); setLatest(null); setNotice('Manuscript saved with revision history.');
            try { await onManuscriptSaved?.(); } catch (reason) {
              setNotice('Manuscript saved with revision history, but the Notebook inventory could not be refreshed. Retry the inventory refresh; your drafts are unchanged.');
              throw reason;
            }
          })}>Save manuscript</button>
          <button className="button secondary small" disabled={busy} onClick={() => void perform(async () => {
            setAudit(await request<Audit>(`${base(track.id)}/audit`, json('POST', { content, expected_sha256: manuscript.sha256 })));
          })}>Check citations and evidence</button>
          <button className="button ghost small" disabled={busy} onClick={() => void perform(async () => setLatest(await request<Manuscript>(`${base(track.id)}/manuscript`)))}>Compare saved manuscript</button>
          {onManuscriptSaved ? <button className="button ghost small" disabled={busy} onClick={() => void perform(async () => {
            await onManuscriptSaved();
            setNotice('Notebook inventory refreshed. Your writing and Notebook drafts are unchanged.');
          })}>Refresh Notebook inventory</button> : null}
          {onOpenManuscript && track.exploration?.some((item) => item.section === 'exploration/manuscript')
            ? <button className="button ghost small" disabled={busy} onClick={onOpenManuscript}>Open manuscript and history in Notebook</button> : null}
        </div>
        {latest ? <div className="writing-comparison">
          <h4>Latest saved manuscript</h4><pre>{latest.content || '(empty)'}</pre>
          <button className="button secondary small" disabled={busy} onClick={() => {
            if (window.confirm('Use this revision as the save base? Your draft is retained; reconcile differences before saving.')) { setManuscript(latest); setLatest(null); }
          }}>Use compared revision as save base</button>
        </div> : null}
        {audit ? <div role="status"><h4>Writing checks</h4><ul>{audit.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul></div> : null}
        <div className="button-row">
          {(['markdown', 'latex', 'docx'] as const).map((format) => <button className="button secondary small" key={format} disabled={busy || dirty || !manuscript.sha256}
            onClick={() => void perform(() => download(format))}>Export {format === 'markdown' ? 'Markdown' : format === 'latex' ? 'LaTeX' : 'DOCX'}</button>)}
          <Link className="button ghost small" to={`/chat?${new URLSearchParams({ track: track.id, research: `Help revise the saved exploration/manuscript section of research track "${track.title}" (ID: ${track.id}). Read that section, use read_research_track action=claims to inspect linked evidence, and read relevant original sources first. Preserve citation keys and claim markers. Discuss changes without saving unless I ask.` })}`}>Discuss saved manuscript</Link>
        </div>
        <p className="muted">Export uses saved text, adds references and claim evidence, and blocks unresolved links. LaTeX and DOCX are plain-text drafts, not journal templates; advanced Markdown formatting is not converted.</p>
      </section>
    </>}
  </section>;
}
