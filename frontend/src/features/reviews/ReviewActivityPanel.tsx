import { useEffect, useState } from 'react';
import { ErrorNotice, Loading } from '../../shared/components/Ui';
import { MarkdownViewer } from '../../shared/components/MarkdownViewer';
import { reviewsApi, type AnalysisProgress, type Review, type ReviewActivityDetail, type ReviewActivityPage } from './api';
import { ProgressTiming } from './ProgressTiming';

const label = (value: string) => value.replace(/_/g, ' ');

function SavedOutput({ value }: { value: unknown }) {
  if (value === null || value === undefined) return <p>No saved output yet.</p>;
  if (typeof value === 'string') return <MarkdownViewer content={value} />;
  if (Array.isArray(value)) return value.length
    ? <ol>{value.map((item, index) => <li key={index}><SavedOutput value={item} /></li>)}</ol>
    : <p>None</p>;
  if (typeof value === 'object') return <dl>{Object.entries(value).map(([key, item]) =>
    <div key={key}><dt><strong>{label(key)}</strong></dt><dd><SavedOutput value={item} /></dd></div>)}</dl>;
  return <span>{String(value)}</span>;
}

function ActivityOutput({ reviewId, jobKey }: { reviewId: string; jobKey: string }) {
  const [detail, setDetail] = useState<ReviewActivityDetail | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    let active = true;
    void reviewsApi.activityDetail(reviewId, jobKey).then((result) => {
      if (active) { setDetail(result); setError(null); }
    }).catch((reason: unknown) => { if (active) setError(reason); });
    return () => { active = false; };
  }, [reviewId, jobKey, refresh]);
  return <>
    <button className="button ghost small" onClick={() => setRefresh((value) => value + 1)}>Refresh saved output</button>
    <ErrorNotice error={error} />
    {!detail && !error ? <Loading label="Loading saved output..." /> : null}
    {detail ? <>
      <p>Saved status: {label(detail.status)}. Invalid or unfinished outputs are not accepted source evidence.</p>
      <ErrorNotice error={detail.error} />
      <SavedOutput value={detail.result} />
    </> : null}
  </>;
}

export function ReviewActivityPanel({ review }: { review: Review }) {
  const [open, setOpen] = useState(review.stage === 'synthesis');
  const [analysis, setAnalysis] = useState<AnalysisProgress | null>(null);
  const [activity, setActivity] = useState<ReviewActivityPage | null>(null);
  const [planOffset, setPlanOffset] = useState(0);
  const [jobOffset, setJobOffset] = useState(0);
  const [selected, setSelected] = useState('');
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    if (!open) return;
    let active = true;
    let inFlight = false;
    const load = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const [plan, jobs] = await Promise.all([
          reviewsApi.analysis(review.id, planOffset), reviewsApi.activity(review.id, jobOffset),
        ]);
        if (active) { setAnalysis(plan); setActivity(jobs); setError(null); }
      } catch (reason) {
        if (active) setError(reason);
      } finally {
        inFlight = false;
      }
    };
    void load();
    const timer = window.setInterval(() => void load(), 3000);
    return () => { active = false; window.clearInterval(timer); };
  }, [open, review.id, planOffset, jobOffset]);

  useEffect(() => {
    if (review.stage === 'synthesis') setOpen(true);
  }, [review.stage]);

  const pageTarget = analysis ? Math.min(analysis.total, analysis.page_limit) : 0;
  return <details open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>Live research plan and saved activity</summary>
    {open ? <section className="review-activity" aria-label="Live research plan and saved activity">
      <ErrorNotice error={error} />
      {!activity && !error ? <Loading label="Loading saved research activity..." /> : null}
      {analysis ? <>
        <h3>Synthesis: {label(analysis.phase)}</h3>
        <p>The main model selects original pages from the saved summaries. Source reading is limited to
          {' '}{analysis.page_limit} distinct pages, including follow-ups. Further reading requires your approval.</p>
        <label>Summary batches inspected: {analysis.selection_completed} / {analysis.selection_total}
          <progress aria-label="Summary batches inspected" value={analysis.selection_completed}
            max={Math.max(1, analysis.selection_total)} /></label>
        <label>Source pages completed: {analysis.completed_pages} / {pageTarget}
          <progress aria-label="Synthesis source reading" value={analysis.completed_pages} max={Math.max(1, pageTarget)} /></label>
        {pageTarget > 0 ? <ProgressTiming key={`${review.id}:${analysis.page_limit}`}
          count={analysis.completed_pages} total={pageTarget} unit="pages"
          running={review.status === 'running' && analysis.phase === 'reading'} /> : null}
        <p>{analysis.pending_pages} requested pages pending. {analysis.admitted_pages} pages admitted.
          {' '}These bars measure selection and source reading, not total report completion.</p>
        {analysis.report_requested ? <p>Writing from current evidence; unread selections are skipped.</p> : null}
        <ol>{analysis.items.map((page) => <li key={`${page.candidate_id}:${page.page}`}>
          <strong>{page.title}, page {page.page}</strong> — {label(page.status)}
          <p>{page.reason}</p>
        </li>)}</ol>
        {analysis.total > 25 ? <div className="button-row">
          <button className="button ghost small" disabled={planOffset === 0}
            onClick={() => setPlanOffset(Math.max(0, planOffset - 25))}>Previous source pages</button>
          <button className="button ghost small" disabled={analysis.next_offset === null}
            onClick={() => { if (analysis.next_offset !== null) setPlanOffset(analysis.next_offset); }}>Next source pages</button>
        </div> : null}
        <p className="field-hint">After source reading: draft evidence batches, combine them, audit each section and gap, then publish.
          Those are finite context-sized jobs, not further autonomous paper exploration.</p>
      </> : <p>No synthesis reading plan saved yet. Earlier completed jobs are available below.</p>}
      <h3>Saved jobs and intermediate results</h3>
      <p>Local database records, newest updates first. Open a job to inspect its output, source quotes,
        selection reasons or draft sections. Results load only when requested.</p>
      <ul className="review-activity-jobs">{activity?.items.map((job) => <li key={job.key}>
        <button className="button ghost small" onClick={() => setSelected(job.key)}>
          {label(job.stage)}: {label(job.key.split(':')[0])} — {label(job.status)}
        </button>
        {job.model ? <small> {job.model} · {job.tokens.toLocaleString()} accounted tokens</small> : null}
        <ErrorNotice error={job.error} />
      </li>)}</ul>
      {activity ? <div className="button-row">
        <button className="button ghost small" disabled={jobOffset === 0}
          onClick={() => setJobOffset(Math.max(0, jobOffset - 25))}>Previous jobs</button>
        <span>{activity.total} saved jobs</span>
        <button className="button ghost small" disabled={activity.next_offset === null}
          onClick={() => { if (activity.next_offset !== null) setJobOffset(activity.next_offset); }}>Next jobs</button>
      </div> : null}
      {selected ? <section className="review-paper-reader" aria-label="Saved job output">
        <h4>{label(selected.split(':')[0])}</h4>
        <button className="button ghost small" onClick={() => setSelected('')}>Close saved output</button>
        <ActivityOutput key={selected} reviewId={review.id} jobKey={selected} />
      </section> : null}
    </section> : null}
  </details>;
}
