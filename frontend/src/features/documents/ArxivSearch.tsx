import { useRef, useState } from 'react';
import { request } from '../../api/client';
import type { components } from '../../api/schema.generated';
import { ErrorNotice } from '../../shared/components/Ui';

export type ArxivPaper = components['schemas']['ArxivSearchResult'];

export function ArxivSearch({ onChoose, chosenIds = [], disabled = false, actionLabel = 'Use as input' }: {
  onChoose: (paper: ArxivPaper) => void;
  chosenIds?: string[];
  disabled?: boolean;
  actionLabel?: string;
}) {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<ArxivPaper[]>([]);
  const [searched, setSearched] = useState('');
  const [next, setNext] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const active = useRef(false);
  const search = async (start = 0) => {
    const text = start ? searched : query.trim();
    if (!text || active.current) return;
    active.current = true;
    setBusy(true); setError(null);
    try {
      const page = await request<components['schemas']['ArxivSearchResponse']>(
        `/research/arxiv?${new URLSearchParams({ query: text, start: String(start) })}`,
      );
      setResults((items) => start ? [...items, ...page.results.filter((row) => !items.some((item) => item.arxiv_id === row.arxiv_id))] : page.results);
      setSearched(text); setNext(page.next_start ?? null);
    } catch (reason) { setError(reason); }
    finally { active.current = false; setBusy(false); }
  };
  return <section className="arxiv-search" aria-label="Search arXiv">
    <h3>Find papers on arXiv</h3>
    <p>Search titles, authors or keywords directly. Searching shows metadata only; nothing is downloaded automatically.</p>
    <div className="source-form">
      <label>arXiv query<input value={query} maxLength={1000} onChange={(event) => setQuery(event.target.value)}
        onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); void search(); } }}
        placeholder="e.g. uncertainty calibration transformers" /></label>
      <button className="button" type="button" disabled={disabled || busy || !query.trim()} onClick={() => void search()}>{busy ? 'Searching arXiv…' : 'Search arXiv'}</button>
    </div>
    <ErrorNotice error={error} />
    {searched ? <p role="status">{results.length ? `${results.length} results for “${searched}”` : `No papers found for “${searched}”. Try a broader query.`}</p> : null}
    {results.map((paper) => {
      const authors = paper.authors ?? [];
      return <article key={paper.arxiv_id}>
        <h4><a href={paper.url} target="_blank" rel="noreferrer">{paper.title}</a></h4>
        <small>{authors.slice(0, 5).join(', ')}{authors.length > 5 ? ` et al. (${authors.length} authors)` : ''} · {paper.published_at.slice(0, 10)} · {paper.arxiv_id}</small>
        <details><summary>Read abstract</summary><p>{paper.summary || 'No abstract available.'}</p></details>
        <button className="button secondary" type="button" disabled={disabled || chosenIds.includes(paper.arxiv_id)} onClick={() => onChoose(paper)}>
          {chosenIds.includes(paper.arxiv_id) ? 'Added as input' : actionLabel}
        </button>
      </article>;
    })}
    {next !== null ? <button className="button secondary" type="button" disabled={disabled || busy} onClick={() => void search(next)}>More arXiv results</button> : null}
  </section>;
}
