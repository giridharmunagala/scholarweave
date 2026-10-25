import { useEffect, useState } from 'react';
import { request } from '../../api/client';
import type { components } from '../../api/schema.generated';
import { Link } from '../../app/router';
import { ErrorNotice, Loading, Panel, StatusPill } from '../../shared/components/Ui';
import { providersApi, type Settings } from '../providers/api';
import { isActiveOcrBatch } from '../runs/api';
import { useOcrBatches } from '../runs/useOcrBatches';

type Paper = components['schemas']['DocumentSummaryResponse'];
type Options = components['schemas']['IngestionOptionsResponse'];

export function useOcrBatch() {
  const jobs = useOcrBatches();
  const current = jobs.batches.find(isActiveOcrBatch) ?? jobs.batches[0];
  return {
    id: current?.id,
    status: current?.status === 'pending' ? 'running' : current?.status ?? 'idle',
    items: current?.items ?? [],
    loading: jobs.loading,
    error: jobs.error,
    refresh: jobs.refresh,
  };
}

export function needsOcr(paper: Paper): boolean {
  if (paper.status === 'processing') return false;
  if (paper.status !== 'ready') return true;
  const metadata = paper.metadata;
  if (['ocr_skipped_pages', 'ocr_fallback_pages'].some((key) => Array.isArray(metadata[key]) && metadata[key].length > 0)) return true;
  const pages = metadata.ocr_pages;
  if (Array.isArray(pages)) {
    const covered = new Set(pages.filter((page): page is number => Number.isInteger(page) && page > 0));
    if (!covered.size) return true;
    if (paper.page_count && paper.page_count > 0) {
      return covered.size < paper.page_count || Array.from({ length: paper.page_count }, (_, index) => index + 1).some((page) => !covered.has(page));
    }
    return false;
  }
  return metadata.extraction_mode !== 'ocr';
}

export function OcrBatchPanel({ papers, disabled = false }: { papers: Paper[]; disabled?: boolean }) {
  const jobs = useOcrBatches();
  const current = jobs.batches.find(isActiveOcrBatch);
  const candidates = [...new Map(papers.filter(needsOcr).map((paper) => [paper.id, paper])).values()];
  const candidateId = candidates[0]?.id;
  const [settings, setSettings] = useState<Settings | null>(null);
  const [options, setOptions] = useState<Options | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let active = true;
    setOptions(null);
    setError(null);
    Promise.all([
      providersApi.settings(),
      candidateId ? request<Options>(`/documents/${encodeURIComponent(candidateId)}/ingestion-options`) : Promise.resolve(null),
    ]).then(([nextSettings, nextOptions]) => {
      if (active) { setSettings(nextSettings); setOptions(nextOptions); }
    }).catch((nextError: unknown) => { if (active) setError(nextError); });
    return () => { active = false; };
  }, [candidateId, retry]);
  const parallel = settings?.ocr_engine === 'surya' ? Math.max(1, Math.floor(settings.ocr_surya_parallel ?? 1)) : 1;
  return <Panel title="Batch OCR" description="Re-extract papers with missing or incomplete OCR using the configured engine. Notes and saved summaries are preserved.">
    {error || jobs.error ? <ErrorNotice error={error || jobs.error} /> : null}
    {error || jobs.error ? <button className="button secondary small" type="button" disabled={jobs.refreshing}
      onClick={() => { setRetry((value) => value + 1); void jobs.refresh(); }}>Retry OCR configuration and runs</button> : null}
    {!settings && !error ? <Loading label="Loading OCR configuration..." /> : null}
    {settings ? <p className="field-hint">
      {settings.ocr_engine === 'surya' ? `Surya OCR model: ${settings.ocr_surya_model}. Shared page-request limit: ${parallel} across all papers.`
        : `Tesseract OCR${settings.ocr_llm_enhancement_enabled ? ' with configured model enhancement' : ''}. Shared page-request limit: 1.`}
      {' '}<Link to="/settings">OCR settings</Link>.
    </p> : null}
    {options && !options.ocr_available ? <p role="alert">The configured {options.ocr_engine} OCR runtime is unavailable.</p> : null}
    <p>{candidates.length} papers with missing or incomplete OCR in this selection. Complete OCR and active ingestion are skipped.</p>
    <p className="field-hint">Batches run on the server, even after closing this panel or reloading. <Link to="/runs">View OCR progress and history in Runs</Link>.</p>
    <button className="button" type="button" disabled={disabled || Boolean(current) || jobs.loading || jobs.refreshing || jobs.starting || !candidates.length || !settings || !options?.ocr_available || Boolean(error || jobs.error)}
      onClick={() => { void jobs.start(candidates.map((paper) => paper.id)).catch(() => undefined); }}>
      {jobs.starting ? 'Starting OCR…' : `Start OCR for ${candidates.length} selected papers`}
    </button>
    {current ? <p role="status">OCR batch <StatusPill value={current.status} />. <Link to="/runs">Open Runs to see progress or stop this batch</Link>.</p> : null}
  </Panel>;
}
