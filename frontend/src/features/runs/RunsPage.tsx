import { useEffect, useState } from 'react';
import { Link } from '../../app/router';
import { EmptyState, ErrorNotice, Loading, PageHeader, Panel, StatusPill } from '../../shared/components/Ui';
import { providersApi, type Settings } from '../providers/api';
import { ProgressTiming } from '../reviews/ProgressTiming';
import { isActiveOcrBatch, type OcrBatch, type OcrBatchItem } from './api';
import { useOcrBatches } from './useOcrBatches';
import './runs.css';

function pageProgress(item: OcrBatchItem) {
  const total = item.total_pages !== null && item.total_pages > 0 ? item.total_pages : null;
  const completed = Math.max(0, Math.min(item.completed_pages, total ?? item.completed_pages));
  return { total, completed };
}

function Progress({ label, completed, total }: { label: string; completed: number; total: number | null }) {
  return <div className="ocr-progress">
    <div><span>{label}</span><strong>{total === null ? `${completed} completed · total unknown` : `${completed} / ${total}`}</strong></div>
    <progress aria-label={label} max={total || 1} value={total === null ? undefined : completed} />
  </div>;
}

function BatchProgress({ batch, stopping, stop }: { batch: OcrBatch; stopping: boolean; stop: () => void }) {
  const counts = (status: OcrBatchItem['status']) => batch.items.filter((item) => item.status === status).length;
  const withErrors = counts('completed_with_errors');
  const completed = counts('completed') + withErrors;
  const failed = counts('failed');
  const skipped = counts('skipped');
  const stopped = counts('stopped');
  const pages = batch.items.map(pageProgress);
  const knownPages = pages.reduce((sum, item) => sum + (item.total ?? 0), 0);
  const completedPages = pages.reduce((sum, item) => sum + item.completed, 0);
  const unknownPages = pages.filter((item) => item.total === null).length;
  const remaining = batch.items.filter((item) => item.status === 'pending' || item.status === 'running').map(pageProgress);
  const remainingPages = remaining.reduce<number | null>((sum, item) =>
    sum === null || item.total === null ? null : sum + item.total - item.completed, 0);
  const estimatedTotal = remainingPages === null ? null : completedPages + remainingPages;
  const active = isActiveOcrBatch(batch);
  const visibleItems = active ? batch.items.filter((item) => item.status === 'running') : batch.items;
  const estimating = batch.status === 'running' && !stopping;
  return <Panel className="ocr-run" title={`OCR batch · ${new Date(batch.created_at).toLocaleString()}`}
    actions={<><StatusPill value={batch.status} />
      {active ? <button className="button secondary" type="button" disabled={stopping || batch.status === 'stopping'} onClick={stop}>
        {stopping || batch.status === 'stopping' ? 'Stopping batch…' : 'Stop batch'}
      </button> : null}</>}>
    <p className="field-hint">Shared page-request limit: <strong>{batch.parallel_requests}</strong>. All papers share this cap.</p>
    <div className="ocr-run-counts" aria-label="Paper status counts">
      <span><strong>{counts('running')}</strong> running</span><span><strong>{counts('pending')}</strong> queued</span>
      <span><strong>{completed}</strong> completed</span><span><strong>{failed}</strong> failed</span>
      {withErrors ? <span><strong>{withErrors}</strong> with page errors</span> : null}
      {skipped ? <span><strong>{skipped}</strong> skipped</span> : null}
      {stopped ? <span><strong>{stopped}</strong> stopped</span> : null}
    </div>
    <div className="ocr-run-overall">
      <Progress label="Papers completed" completed={completed} total={batch.items.length} />
      <Progress label="Pages completed" completed={completedPages} total={unknownPages ? null : knownPages} />
      {estimating ? <div className="ocr-run-timing">
        <strong>Estimated batch time (last 2 minutes)</strong>
        <ProgressTiming count={completedPages} total={estimatedTotal} running unit="pages" windowSeconds={120} />
      </div> : null}
    </div>
    {unknownPages ? <p className="field-hint">{knownPages} known pages; page totals are not yet available for {unknownPages} papers.</p> : null}
    <p className="field-hint">{completed + failed + skipped + stopped} of {batch.items.length} papers settled.</p>
    {stopping || batch.status === 'stopping' ? <p role="status">Cancellation requested. Waiting for the server to settle active work; queued papers will not start.</p> : null}
    {batch.status === 'interrupted' ? <p role="status">This batch was interrupted by a server restart. Start a new batch manually to process remaining eligible papers.</p> : null}
    <ul className="ocr-run-items">
      {visibleItems.map((item) => {
        const progress = pageProgress(item);
        return <li key={item.document_id}>
          <div className="ocr-run-item-heading"><Link to={`/library?paper=${encodeURIComponent(item.document_id)}`}>{item.title}</Link><StatusPill value={item.status} /></div>
          <p className="field-hint" aria-label={`Pages for ${item.title}`}>
            Pages: {progress.total === null ? `${progress.completed} processed · total unknown` : `${progress.completed} / ${progress.total}`}
          </p>
          {item.phase_label ? <p className="field-hint">{item.phase_label}</p> : null}
          {item.error ? <ErrorNotice error={item.error} /> : null}
        </li>;
      })}
    </ul>
    {active && !visibleItems.length ? <p className="field-hint">No papers are currently running. Queued papers remain included in the totals above.</p> : null}
  </Panel>;
}

function HistoryBatch({ batch }: { batch: OcrBatch }) {
  const [expanded, setExpanded] = useState(false);
  return <details className="ocr-history-batch" onToggle={(event) => setExpanded(event.currentTarget.open)}>
    <summary>
      <span>{new Date(batch.created_at).toLocaleString()}</span>
      <StatusPill value={batch.status} />
    </summary>
    {expanded ? <BatchProgress batch={batch} stopping={false} stop={() => undefined} /> : null}
  </details>;
}

export function RunsPage() {
  const jobs = useOcrBatches();
  const [settings, setSettings] = useState<Settings | null>(null);
  const [settingsError, setSettingsError] = useState<unknown>(null);
  const [settingsRevision, setSettingsRevision] = useState(0);
  useEffect(() => {
    let active = true;
    setSettingsError(null);
    providersApi.settings().then((next) => { if (active) setSettings(next); })
      .catch((error: unknown) => { if (active) setSettingsError(error); });
    return () => { active = false; };
  }, [settingsRevision]);
  const active = jobs.batches.filter(isActiveOcrBatch);
  const history = jobs.batches.filter((batch) => !isActiveOcrBatch(batch))
    .sort((a, b) => b.created_at.localeCompare(a.created_at)).slice(0, 2);
  const parallel = settings?.ocr_engine === 'surya' ? Math.max(1, Math.floor(settings.ocr_surya_parallel ?? 1)) : 1;
  const refresh = () => {
    void jobs.refresh();
    if (settingsError) setSettingsRevision((value) => value + 1);
  };
  return <div className="runs-page">
    <PageHeader title="Runs" description="Server-managed OCR batches, progress and recent history."
      actions={<button className="button secondary" type="button" disabled={jobs.refreshing || jobs.starting || Boolean(jobs.stopping.length)} onClick={refresh}>
        {jobs.refreshing ? 'Refreshing…' : 'Refresh runs'}
      </button>} />
    <p className="muted">OCR batches continue on the server when you navigate away or reload. Nothing starts automatically.</p>
    {jobs.error ? <ErrorNotice error={jobs.error} /> : null}
    {settingsError ? <ErrorNotice error={settingsError} /> : null}
    <Panel title="Batch OCR" description="Process papers with missing or incomplete OCR. Existing complete OCR, active ingestion, notes and saved summaries are preserved.">
      {settings ? <p className="field-hint">{settings.ocr_engine === 'surya' ? `Surya · ${settings.ocr_surya_model}` : 'Tesseract'} · Configured shared page-request cap: <strong>{parallel}</strong>. <Link to="/settings">OCR settings</Link></p> : null}
      <button className="button" type="button" disabled={jobs.loading || jobs.refreshing || jobs.starting || Boolean(jobs.stopping.length) || Boolean(active.length) || Boolean(jobs.error) || !settings}
        onClick={() => { void jobs.start(null).catch(() => undefined); }}>{jobs.starting ? 'Starting OCR…' : 'Start OCR for remaining papers'}</button>
      {active.length ? <p className="field-hint">An OCR batch is active. Stop it or wait for completion before starting another.</p> : null}
    </Panel>
    {jobs.loading ? <Loading label="Loading OCR runs…" /> : null}
    {!jobs.loading && !jobs.error && !jobs.batches.length ? <EmptyState title="No OCR batches yet" description="Use Start OCR when you are ready. Progress and recent batches will appear here." /> : null}
    {active.length || history.length ? <div className={`ocr-runs-layout${history.length ? ' has-history' : ''}`}>
      {active.length ? <section className="ocr-runs-section" aria-label="Active OCR batches"><h2>Active batches</h2>
        {active.map((batch) => <BatchProgress key={batch.id} batch={batch} stopping={jobs.stopping.includes(batch.id)}
          stop={() => { void jobs.stop(batch.id).catch(() => undefined); }} />)}
      </section> : <EmptyState title="No active OCR batches" description="Start OCR when you are ready. The two most recent batches are available in history." />}
      {history.length ? <aside className="ocr-runs-history" aria-label="Recent OCR batches">
        <h2>History</h2>
        <p className="field-hint">Two most recent batches. Expand to view details.</p>
        {history.map((batch) => <HistoryBatch key={batch.id} batch={batch} />)}
      </aside> : null}
    </div> : null}
  </div>;
}

export default RunsPage;
