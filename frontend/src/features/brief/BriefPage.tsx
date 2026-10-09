import { useEffect, useRef, useState } from 'react';
import { Link, useNavigationGuard } from '../../app/router';
import { Icon } from '../../shared/components/Icons';
import { EmptyState, ErrorNotice, Loading } from '../../shared/components/Ui';
import {
  clearBrief, discardBriefItem, fetchBriefItem, keepBriefItem, loadCatalog,
  previewUrl, refreshBrief, savePreferences,
  type BriefCatalog, type BriefEdition, type BriefItem, type BriefPreferences, type BriefPreview,
} from './api';
import './brief.css';

const SECTIONS = [
  { kind: 'paper', title: 'On your research radar', subtitle: 'arXiv papers matched to your interests', icon: 'papers', initial: 5 },
  { kind: 'lab', title: 'Inside the labs', subtitle: 'Research and announcements, direct from the source', icon: 'bulb', initial: 5 },
  { kind: 'engineering', title: 'Under the hood', subtitle: 'Training, inference and framework releases', icon: 'tools', initial: 3 },
  { kind: 'reading', title: 'Worth a read', subtitle: 'Technical writing from the open ecosystem', icon: 'file', initial: 2 },
] as const;

function saved(item: BriefItem) { return !!(item.saved_document_id || item.saved_path); }
function savedUrl(item: BriefItem) {
  return item.saved_document_id ? `/library?paper=${encodeURIComponent(item.saved_document_id)}`
    : `/library/notes?path=${encodeURIComponent(item.saved_path ?? '')}`;
}
function bytes(size: number) {
  return size >= 1024 * 1024 ? `${(size / (1024 * 1024)).toFixed(1)} MB`
    : `${Math.max(1, Math.round(size / 1024))} KB`;
}
function published(item: BriefItem) {
  return item.published_at
    ? new Date(item.published_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
    : 'Date unverified';
}

export default function BriefPage() {
  const [catalog, setCatalog] = useState<BriefCatalog | null>(null);
  const [edition, setEdition] = useState<BriefEdition | null>(null);
  const [preview, setPreview] = useState<BriefPreview | null>(null);
  const [busy, setBusy] = useState<string | null>('Loading your brief');
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState('');
  const [query, setQuery] = useState('');
  const [topic, setTopic] = useState('');
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [limits, setLimits] = useState<Record<string, number>>({});
  const mounted = useRef(true);
  const running = useRef(true);
  useNavigationGuard(!!busy, 'A brief operation is still running. Please wait before leaving.', false);

  useEffect(() => {
    mounted.current = true;
    let active = true;
    Promise.all([loadCatalog(), refreshBrief()])
      .then(([nextCatalog, nextEdition]) => {
        if (active) { setCatalog(nextCatalog); setEdition(nextEdition); }
      })
      .catch((reason) => { if (active) setError(reason); })
      .finally(() => { if (active) { running.current = false; setBusy(null); } });
    return () => { active = false; mounted.current = false; };
  }, []);

  const perform = async (label: string, work: () => Promise<void>) => {
    if (running.current) return;
    running.current = true;
    setBusy(label);
    setError(null);
    setNotice('');
    try { await work(); }
    catch (reason) { if (mounted.current) setError(reason); }
    finally {
      running.current = false;
      if (mounted.current) setBusy(null);
    }
  };
  const updateItem = (item: BriefItem) => {
    if (!mounted.current) return;
    setEdition((current) => {
      if (!current) return current;
      const old = current.items?.find((value) => value.id === item.id);
      return {
        ...current,
        temporary_bytes: (current.temporary_bytes ?? 0) + (item.downloaded && !old?.downloaded ? item.size_bytes ?? 0 : 0),
        items: current.items?.map((value) => value.id === item.id ? item : value),
      };
    });
    setPreview((current) => current?.item.id === item.id ? { ...current, item } : current);
  };
  const download = (item: BriefItem) => perform(
    item.kind === 'paper' ? 'Downloading PDF' : 'Downloading page',
    async () => {
      const result = await fetchBriefItem(item.id);
      if (!mounted.current) return;
      updateItem(result.item);
      setPreview(result);
    },
  );
  const keep = (item: BriefItem) => perform('Keeping in your library', async () => {
    const result = await keepBriefItem(item.id);
    if (!mounted.current) return;
    updateItem(result);
    setNotice('Kept in your library. Clearing the brief will not delete it.');
  });
  const discard = (item: BriefItem) => perform('Discarding this item', async () => {
    const result = await discardBriefItem(item.id);
    if (!mounted.current) return;
    setEdition(result);
    if (preview?.item.id === item.id) setPreview(null);
    setNotice(saved(item) ? 'Headline dismissed. Your library copy is unchanged.' : 'Headline and temporary preview discarded.');
  });
  const refresh = () => perform('Checking your sources', async () => {
    const result = await refreshBrief(true);
    if (!mounted.current) return;
    setEdition(result);
    setPreview(null);
    setLimits({});
  });
  const clear = () => {
    if (!window.confirm('Clear all fetched headlines and temporary PDFs/pages? Everything you kept in the library is safe.')) return;
    void perform('Clearing temporary data', async () => {
      const result = await clearBrief();
      if (!mounted.current) return;
      setEdition(result);
      setPreview(null);
      setQuery('');
      setTopic('');
      setNotice('Temporary data cleared. Kept papers and pages remain in your library.');
    });
  };
  const configure = (preferences: BriefPreferences) => perform('Saving interests and refreshing', async () => {
    const nextCatalog = await savePreferences(preferences);
    if (mounted.current) { setCatalog(nextCatalog); setSettingsOpen(false); }
    const nextEdition = await refreshBrief(true);
    if (!mounted.current) return;
    setEdition(nextEdition);
    setPreview(null);
    setTopic('');
    setLimits({});
  });

  const items = edition?.items ?? [];
  const sourceStatuses = edition?.sources ?? [];
  const failures = sourceStatuses.filter((source) => source.error);
  const visible = items.filter((item) => (!topic || item.topics?.includes(topic))
    && (!query.trim() || `${item.title} ${item.source_name} ${item.abstract}`.toLowerCase().includes(query.trim().toLowerCase())));
  const topics = catalog?.topics?.filter((value) => catalog.preferences.topic_ids?.includes(value.id)) ?? [];
  const date = (edition?.day ? new Date(`${edition.day}T12:00:00`) : new Date())
    .toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' });
  const selectedItem = preview ? items.find((item) => item.id === preview.item.id) ?? preview.item : null;

  return (
    <div className={`brief-page${preview ? ' has-preview' : ''}`}>
      <header className="brief-masthead">
        <div>
          <div className="brief-date"><Icon name="bulb" size={16} /> YOUR MORNING EDITION <span>{date}</span></div>
          <h1>Morning Brief<span className="brief-title-dot">.</span></h1>
          <p>A little less noise. A little more understanding.</p>
        </div>
        <div className="brief-header-actions">
          <button type="button" onClick={() => setSettingsOpen(!settingsOpen)} aria-expanded={settingsOpen} disabled={!!busy}>
            <Icon name="sliders" size={15} /> Interests & sources
          </button>
          <button type="button" className="primary" onClick={() => void refresh()} disabled={!!busy}>
            <Icon name="refresh" size={15} /> Refresh
          </button>
        </div>
      </header>

      <div className="brief-lifecycle">
        <span><Icon name="shield" size={15} /> Headlines first. Downloads stay temporary until you choose <strong>Keep</strong>.</span>
        <button type="button" className="brief-clear" onClick={clear} disabled={!!busy || !items.length}>
          <Icon name="trash" size={14} /> Clear fetched data
        </button>
      </div>
      {error ? <ErrorNotice error={error} /> : null}
      {notice ? <p className="brief-notice" role="status">{notice}</p> : null}
      {settingsOpen && catalog ? <BriefSettings catalog={catalog} busy={!!busy} onSave={configure} onClose={() => setSettingsOpen(false)} /> : null}
      {busy ? <Loading label={`${busy}...`} /> : null}

      {edition ? (
        <>
          <div className="brief-edition-bar">
            <span><strong>{items.length}</strong> headlines</span>
            <span><strong>{items.filter(saved).length}</strong> already kept</span>
            <span>{edition.temporary_bytes ? `${bytes(edition.temporary_bytes)} temporary` : 'No content downloaded'}</span>
            <span className="brief-updated">{edition.fetched_at
              ? `Checked ${new Date(edition.fetched_at).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}`
              : 'Ready when you are'}</span>
          </div>
          {failures.length ? <div className="brief-source-warning" role="status">
            {failures.length} source{failures.length === 1 ? '' : 's'} could not refresh. See source status below; other headlines are still available.
          </div> : null}
          {items.length ? (
            <>
              <div className="brief-filters">
                <label className="brief-search"><Icon name="search" size={16} />
                  <input aria-label="Search brief headlines" placeholder="Find a headline..." value={query} onChange={(event) => setQuery(event.target.value)} />
                </label>
                <div className="brief-topic-filter" aria-label="Filter by research interest">
                  <button type="button" aria-pressed={!topic} onClick={() => setTopic('')}>All interests</button>
                  {topics.map((value) => <button type="button" key={value.id} aria-pressed={topic === value.label} onClick={() => setTopic(value.label)}>{value.label}</button>)}
                </div>
              </div>
              <div className="brief-reading-layout">
                <div className="brief-sections">
                  {!visible.length ? <EmptyState title="No matching headlines" description="Try another topic or a broader search." /> : null}
                  {SECTIONS.map((section) => {
                    const sectionItems = visible.filter((item) => item.kind === section.kind);
                    if (!sectionItems.length) return null;
                    const limit = limits[section.kind] ?? section.initial;
                    return <section className="brief-section" key={section.kind} aria-label={section.title}>
                      <header className="brief-section-header">
                        <span className="brief-section-icon"><Icon name={section.icon} size={18} /></span>
                        <div><h2>{section.title}</h2><p>{section.subtitle}</p></div>
                        <span className="brief-section-count">{sectionItems.length}</span>
                      </header>
                      <div className="brief-headlines">
                        {sectionItems.slice(0, limit).map((item, index) => <Headline
                          key={item.id} item={item} number={index + 1} busy={!!busy}
                          selected={preview?.item.id === item.id}
                          onDownload={() => void download(item)} onKeep={() => void keep(item)} onDiscard={() => void discard(item)}
                        />)}
                      </div>
                      {sectionItems.length > limit ? <button type="button" className="brief-more" onClick={() => setLimits({ ...limits, [section.kind]: limit + 10 })}>
                        Show more <span>({sectionItems.length - limit} remaining)</span><Icon name="arrowRight" size={14} />
                      </button> : null}
                    </section>;
                  })}
                </div>
                {preview && selectedItem ? <aside className="brief-reader" aria-label="Temporary reading preview">
                  <div className="brief-reader-top">
                    <span><Icon name="clock" size={14} /> TEMPORARY PREVIEW</span>
                    <button type="button" className="icon-button" aria-label="Close preview" onClick={() => setPreview(null)}><Icon name="close" size={17} /></button>
                  </div>
                  <h2>{selectedItem.title}</h2>
                  <p className="brief-reader-source">{selectedItem.source_name} · {bytes(selectedItem.size_bytes ?? 0)}</p>
                  <div className="brief-reader-actions">
                    {saved(selectedItem)
                      ? <Link className="button" to={savedUrl(selectedItem)}><Icon name="check" size={15} /> Open kept copy</Link>
                      : <button type="button" className="primary" disabled={!!busy} onClick={() => void keep(selectedItem)}><Icon name="save" size={15} /> Keep in library</button>}
                    <button type="button" disabled={!!busy} onClick={() => void discard(selectedItem)}><Icon name="trash" size={14} /> Discard</button>
                    <a href={selectedItem.url} target="_blank" rel="noreferrer">Original <Icon name="arrowRight" size={13} /></a>
                  </div>
                  <p className="brief-reader-help">Clear removes this preview, not a kept library copy.</p>
                  {selectedItem.preview_note ? <p className="brief-source-warning">{selectedItem.preview_note}</p> : null}
                  {preview.media_type === 'application/pdf'
                    ? <iframe title={`PDF preview: ${selectedItem.title}`} src={previewUrl(selectedItem.id)} className="brief-pdf" />
                    : <div className="brief-page-text">{preview.text}</div>}
                </aside> : null}
              </div>
            </>
          ) : !busy ? <EmptyState
            title={edition.cleared ? 'A clean slate.' : failures.length === sourceStatuses.length && failures.length ? 'Your sources are unavailable' : 'A quiet research morning'}
            description={edition.cleared
              ? 'Fetched headlines and temporary downloads are gone. Your kept library is untouched. Refresh when you want a new reading list.'
              : 'No matching headlines were found in this window. Check source status, adjust your interests, or refresh later.'}
          /> : null}
          {sourceStatuses.length ? <details className="brief-source-status">
            <summary><Icon name="globe" size={16} /> Source status <span>{sourceStatuses.length - failures.length}/{sourceStatuses.length} reached</span></summary>
            <div className="brief-source-grid">{sourceStatuses.map((source) => <div key={source.source_id} className={source.error ? 'failed' : ''}>
              <strong>{source.name}</strong>
              <span>{source.error ? 'Unavailable' : `${source.count} recent headlines collected`}</span>
              {source.error || source.warning ? <p>{source.error || source.warning}</p> : null}
            </div>)}</div>
          </details> : null}
          <footer className="brief-footer">
            Titles and abstracts drive topic matching—not popularity. Preprints and publisher claims are not independently verified.
            Undated links are labeled; arXiv revisions are separate from new submissions.
            <span>Refreshes on your first visit each day while ScholarWeave is running. Unsaved content also disappears on restart.</span>
          </footer>
        </>
      ) : !busy ? <button type="button" onClick={() => void refresh()}>Try again</button> : null}
    </div>
  );
}

function Headline({ item, number, busy, selected, onDownload, onKeep, onDiscard }: {
  item: BriefItem; number: number; busy: boolean; selected: boolean;
  onDownload: () => void; onKeep: () => void; onDiscard: () => void;
}) {
  return <article className={`brief-headline${selected ? ' selected' : ''}${saved(item) ? ' kept' : ''}`}>
    <span className="brief-headline-number">{String(number).padStart(2, '0')}</span>
    <div className="brief-headline-body">
      <div className="brief-headline-meta">
        <span>{item.source_name}</span><time dateTime={item.published_at ?? undefined}>{published(item)}</time>
        {item.kind === 'paper' ? <span>{item.announcement?.includes('replace') ? 'Revised preprint' : item.announcement === 'cross' ? 'Cross-listed preprint' : 'Preprint'}</span> : null}
        {saved(item) ? <span className="brief-kept"><Icon name="check" size={12} /> Kept</span> : item.downloaded ? <span>Temporary download</span> : null}
      </div>
      <h3><button type="button" onClick={onDownload} disabled={busy}>{item.title}</button></h3>
      <div className="brief-headline-bottom">
        <div className="brief-tags">{item.topics?.slice(0, 2).map((label) => <span key={label}>{label}</span>)}</div>
        <div className="brief-item-actions">
          <button type="button" onClick={onDownload} disabled={busy}>
            <Icon name={item.downloaded ? 'file' : 'download'} size={13} />
            {item.downloaded ? 'Read preview' : item.kind === 'paper' ? 'Download PDF' : 'Download page'}
          </button>
          {saved(item) ? <Link to={savedUrl(item)} aria-label={`Open kept copy of ${item.title}`}>Library</Link>
            : item.downloaded ? <button type="button" onClick={onKeep} disabled={busy}><Icon name="save" size={13} /> Keep</button> : null}
          <button type="button" className="icon-button" onClick={onDiscard} disabled={busy} title="Discard headline and temporary content" aria-label={`Discard ${item.title}`}><Icon name="close" size={14} /></button>
        </div>
      </div>
      {item.abstract || item.authors?.length ? <details className="brief-paper-details">
        <summary>{item.kind === 'paper' ? 'Paper details' : 'Publisher excerpt'}</summary>
        {item.authors?.length ? <p className="brief-authors">{item.authors.join(', ')}</p> : null}
        <p>{item.abstract}</p>
        <span>{item.kind === 'paper' ? 'Abstract supplied by the source. The full paper has not been reviewed.' : 'Excerpt supplied by the publisher. Download the page to read its text.'}</span>
        <a href={item.url} target="_blank" rel="noreferrer">Read original</a>
      </details> : <a className="brief-original" href={item.url} target="_blank" rel="noreferrer">View original</a>}
    </div>
  </article>;
}

function BriefSettings({ catalog, busy, onSave, onClose }: {
  catalog: BriefCatalog; busy: boolean; onSave: (preferences: BriefPreferences) => Promise<void>; onClose: () => void;
}) {
  const [draft, setDraft] = useState<BriefPreferences>(catalog.preferences);
  const [exclusions, setExclusions] = useState(catalog.preferences.exclude_terms?.join(', ') ?? '');
  const toggle = (key: 'topic_ids' | 'source_ids', id: string) => {
    const ids = draft[key] ?? [];
    setDraft({ ...draft, [key]: ids.includes(id) ? ids.filter((value) => value !== id) : [...ids, id] });
  };
  return <form className="brief-settings" onSubmit={(event) => {
    event.preventDefault();
    void onSave({ ...draft, exclude_terms: exclusions.split(',').map((term) => term.trim()).filter(Boolean) });
  }}>
    <div className="brief-settings-heading"><div><h2>Make it your morning</h2><p>Choose research interests and primary sources. No paid feed or model is required.</p></div>
      <button type="button" className="icon-button" onClick={onClose} aria-label="Close interests and sources" disabled={busy}><Icon name="close" size={17} /></button></div>
    <fieldset disabled={busy}><legend>Research interests</legend><div className="brief-choice-grid">
      {catalog.topics?.map((value) => <label key={value.id}><input type="checkbox" checked={draft.topic_ids?.includes(value.id) ?? false} onChange={() => toggle('topic_ids', value.id)} />{value.label}</label>)}
    </div></fieldset>
    <div className="brief-settings-fields">
      <label>Additional interests <input value={draft.extra_interests ?? ''} maxLength={1000} onChange={(event) => setDraft({ ...draft, extra_interests: event.target.value })} placeholder="e.g. mixture of experts, diffusion" disabled={busy} /><small>Comma-separated phrases matched against titles and abstracts.</small></label>
      <label>Exclude phrases <input value={exclusions} maxLength={3000} onChange={(event) => setExclusions(event.target.value)} placeholder="e.g. sponsored, webinar" disabled={busy} /></label>
      <label>Recent reading window <select value={draft.lookback_days ?? 7} onChange={(event) => setDraft({ ...draft, lookback_days: Number(event.target.value) })} disabled={busy}>
        <option value={1}>Last 24 hours</option><option value={3}>Last 3 days</option><option value={7}>Last 7 days</option><option value={14}>Last 14 days</option>
      </select><small>arXiv RSS covers its latest announcement batch, not a historical search.</small></label>
    </div>
    <fieldset disabled={busy}><legend>Sources</legend><div className="brief-choice-grid">
      {catalog.sources?.map((source) => <label key={source.id}><input type="checkbox" checked={draft.source_ids?.includes(source.id) ?? false} onChange={() => toggle('source_ids', source.id)} />{source.name}</label>)}
    </div></fieldset>
    <div className="brief-settings-foot"><span>Free-to-read does not necessarily mean open-source models or code.</span><button className="primary" type="submit" disabled={busy || !draft.topic_ids?.length || !draft.source_ids?.length}>Save & refresh</button></div>
  </form>;
}
