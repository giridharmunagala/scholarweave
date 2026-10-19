import { useEffect, useRef, useState } from 'react';
import { subscribeToReview } from '../../api/events';
import { Link, useNavigationGuard } from '../../app/router';
import { ModelSelect, type ModelSelectValue } from '../../shared/components/ModelSelect';
import { ErrorNotice, Loading, StatusPill } from '../../shared/components/Ui';
import { MarkdownViewer } from '../../shared/components/MarkdownViewer';
import { capabilityOptions } from '../providers/ModelDefaultsPanel';
import { providersApi, type Provider } from '../providers/api';
import {
  reviewsApi, REVIEW_STAGES, type Candidate, type CandidatePage, type CandidateSort,
  type LocalPaper, type PaperProgressPage, type PaperReview, type Review, type ReviewControl, type ReviewReference, type SummaryRole,
} from './api';
import './reviews.css';
import { ProgressTiming } from './ProgressTiming';
import { ReviewActivityPanel } from './ReviewActivityPanel';

const label = (value: string) => value.replace(/_/g, ' ');

export function LiteratureReviews({
  conversationId, trackId, initialModel = {}, initialObjective = '', webEnabled = true,
}: {
  conversationId?: string;
  trackId?: string;
  initialModel?: ModelSelectValue;
  initialObjective?: string;
  webEnabled?: boolean;
}) {
  const [reviews, setReviews] = useState<Review[]>([]);
  const [selectedId, setSelectedId] = useState(() => new URLSearchParams(window.location.search).get('review') ?? '');
  const [providers, setProviders] = useState<Provider[]>([]);
  const [main, setMain] = useState(initialModel);
  const [mainParallelRequests, setMainParallelRequests] = useState(1);
  const [decision, setDecision] = useState<ModelSelectValue>({});
  const [summary, setSummary] = useState<SummaryRole | null>(null);
  const [protocol, setProtocol] = useState<'off' | 'systemone' | 'generation'>('off');
  const [inputTokens, setInputTokens] = useState(8192);
  const [title, setTitle] = useState('Literature review');
  const [objective, setObjective] = useState(initialObjective);
  const [external, setExternal] = useState(webEnabled);
  const [creating, setCreating] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [clearReceipt, setClearReceipt] = useState('');
  const [parallelCalls, setParallelCalls] = useState(8);
  const [liveError, setLiveError] = useState('');
  const requestVersion = useRef(0);
  const mounted = useRef(true);
  const selected = selectedId ? reviews.find((review) => review.id === selectedId) : reviews[0];
  useNavigationGuard(busy || ((creating || !reviews.length) && !!objective.trim()),
    'Leave Deep Work and discard this review setup? Running reviews will continue.', !busy);

  useEffect(() => {
    mounted.current = true;
    let active = true;
    let inFlight = false;
    requestVersion.current += 1;
    setBusy(false);
    setReviews([]);
    setLoading(true);
    const load = async () => {
      if (inFlight) return;
      inFlight = true;
      const version = requestVersion.current;
      try {
        const items = await reviewsApi.list(conversationId, trackId);
        if (active && mounted.current && version === requestVersion.current) setReviews((previous) => items.map((item) => {
          const current = previous.find((review) => review.id === item.id);
          return current && current.revision > item.revision ? current : item;
        }));
      } catch (reason) {
        if (active && mounted.current) setError(reason);
      } finally {
        inFlight = false;
        if (active && mounted.current) setLoading(false);
      }
    };
    void load();
    void Promise.all([providersApi.list(), providersApi.settings()]).then(([items, settings]) => {
      if (!active || !mounted.current) return;
      setProviders(items);
      setParallelCalls(settings.decision_parallel_requests);
      setMain((value) => value.provider_profile_id && value.model ? value : settings.default_model_references.chat ?? {});
      const defaultDecision = settings.default_model_references.decision;
      if (defaultDecision?.provider_profile_id && defaultDecision.model) {
        setDecision(defaultDecision);
        setProtocol('systemone');
      }
    }).catch((reason) => { if (active && mounted.current) setError(reason); });
    const timer = window.setInterval(() => void load(), 3000);
    return () => { active = false; mounted.current = false; window.clearInterval(timer); };
  }, [conversationId, trackId]);

  useEffect(() => {
    setLiveError('');
    if (!selected || selected.status !== 'running') return;
    if (typeof EventSource === 'undefined') {
      setLiveError('Live streaming is unavailable; progress refreshes every three seconds.');
      return;
    }
    return subscribeToReview(selected.id, (review) => {
      if (review.id !== selected.id) return;
      setLiveError('');
      setReviews((items) => items.map((item) => item.id === review.id && item.revision <= review.revision ? review : item));
    }, () => setLiveError('Live updates interrupted; reconnecting. Progress still refreshes every three seconds.'));
  }, [selected?.id, selected?.status]);

  const perform = async (action: () => Promise<Review>) => {
    if (busy) return;
    const version = ++requestVersion.current;
    setBusy(true);
    setError(null);
    try {
      const review = await action();
      if (!mounted.current || version !== requestVersion.current) return;
      setReviews((items) => [review, ...items.filter((item) => item.id !== review.id)]);
      setSelectedId(review.id);
      setCreating(false);
    } catch (reason) {
      if (mounted.current && version === requestVersion.current) setError(reason);
    } finally {
      if (mounted.current && version === requestVersion.current) {
        requestVersion.current += 1;
        setBusy(false);
      }
    }
  };
  const start = () => perform(() => reviewsApi.create({
    title: title.trim(), objective: objective.trim(), main_model: main,
    main_parallel_requests: mainParallelRequests,
    summary_model: summary,
    decision_model: protocol === 'off' ? null : {
      model_reference: decision, protocol, input_tokens: inputTokens,
    },
    conversation_id: conversationId ?? null, track_id: trackId || null,
    web_enabled: external && webEnabled, openalex_enabled: false,
  }));
  const clear = async () => {
    if (!selected || busy || !window.confirm(
      `Clear "${selected.title}"? This stops all its work and removes its history and task-owned downloads. Pre-existing, shared, edited, or unverified artifacts will be kept. This cannot be undone.`,
    )) return;
    const version = ++requestVersion.current;
    setBusy(true);
    setError(null);
    try {
      const current = await reviewsApi.get(selected.id);
      const result = await reviewsApi.clear(selected.id, current.revision);
      if (!mounted.current || version !== requestVersion.current) return;
      setReviews((items) => items.filter((item) => item.id !== selected.id));
      setSelectedId('');
      setTitle(selected.title);
      setObjective(selected.objective);
      setCreating(true);
      setClearReceipt(`Review cleared. Removed ${result.deleted_document_ids.length} task-owned papers. Kept ${result.preserved_document_ids.length} pre-existing, shared, or unverified papers and ${result.preserved_paths.length} modified or unverified output files.`);
    } catch (reason) {
      if (mounted.current && version === requestVersion.current) setError(reason);
    } finally {
      if (mounted.current && version === requestVersion.current) {
        requestVersion.current += 1;
        setBusy(false);
      }
    }
  };
  const validModels = !!main.provider_profile_id && !!main.model
    && validParallelRequests(mainParallelRequests)
    && validSummary(summary)
    && (protocol === 'off' || (!!decision.provider_profile_id && !!decision.model));
  const decisionOptions = capabilityOptions(providers, protocol === 'systemone' ? 'decision' : 'chat')
    .filter((option) => protocol !== 'systemone' || option.providerKind === 'openai_compatible');

  return <section className="literature-reviews" aria-label="Literature review workspace">
    <header className="review-heading">
      <div><h2>Literature reviews</h2><p>A focused shortlist, grounded findings, and explicit reading decisions.</p></div>
      <button className="button secondary small" disabled={busy} onClick={() => setCreating(!creating)}>
        {creating ? 'Close setup' : 'New review'}
      </button>
    </header>
    <ErrorNotice error={error} />
    {liveError ? <p role="status" className="field-hint">{liveError}</p> : null}
    {clearReceipt ? <p className="review-receipt" role="status">{clearReceipt}</p> : null}
    {reviews.some((review) => review.status === 'awaiting_approval') ? <div className="review-approval" role="status">
      <strong>Decisions needed</strong>
      <div className="button-row">{reviews.filter((review) => review.status === 'awaiting_approval').map((review) =>
        <button className="button secondary small" key={review.id} disabled={busy} onClick={() => setSelectedId(review.id)}>
          {review.title}: {review.approval === 'budget' ? 'Review allowance' : review.approval === 'plan' ? 'Approve scope and allowance' : 'Continue automatic reading'}
        </button>)}</div>
    </div> : null}
    {loading ? <Loading label="Loading reviews..." /> : null}
    {!loading && selectedId && !selected ? <p role="alert">The linked review is unavailable in this selection. Choose another review or clear the track filter.</p> : null}
    {creating || (!loading && !reviews.length) ? <form className="review-setup" onSubmit={(event) => { event.preventDefault(); void start(); }}>
      <label>Review title<input value={title} maxLength={200} required onChange={(event) => setTitle(event.target.value)} /></label>
      <label>Research objective<textarea value={objective} required maxLength={20000} rows={4}
        placeholder="Define the field, research question and relevant constraints." onChange={(event) => setObjective(event.target.value)} /></label>
      <div className="review-models">
        <label>Main research model<ModelSelect ariaLabel="Main research model" options={capabilityOptions(providers, 'chat')} value={main} onChange={setMain} /></label>
        <label>Main model parallel requests<input type="number" min={1} max={32} step={1} required
          value={mainParallelRequests} onChange={(event) => setMainParallelRequests(Number(event.target.value))} /></label>
        <label>Decision role<select value={protocol} onChange={(event) => {
          const value = event.target.value;
          if (value === 'off' || value === 'systemone' || value === 'generation') { setProtocol(value); setDecision({}); }
        }}>
          <option value="off">Settings default (main model if unset)</option>
          <option value="systemone">Native llama.cpp System One</option>
          <option value="generation">Structured generation (local or API)</option>
        </select></label>
        {protocol !== 'off' ? <label>Decision model<ModelSelect ariaLabel="Decision model" options={decisionOptions} value={decision} onChange={setDecision} /></label> : null}
        {protocol === 'systemone' ? <label>Verified decision input allowance<input type="number" min={128} max={131072} step={1}
          value={inputTokens} required onChange={(event) => setInputTokens(Number(event.target.value))} /></label> : null}
      </div>
      <SummaryModelFields providers={providers} value={summary} onChange={setSummary} disabled={busy} />
      <p className="field-hint">The main model defaults to one request at a time; increase its limit only when the selected server can safely run concurrent requests. System One screens up to {parallelCalls} papers in parallel. <Link to="/settings">Choose the decision model and parallel-call limit in Settings.</Link> Main-model fallback uses batches, not one generation per paper.</p>
      <details className="review-technical"><summary>Model and source limits</summary>
      <p className="field-hint">Native probabilities are not calibrated relevance or scientific-quality scores; oversized or uncertain inputs use the main model. Generation and model switches remain serialized. To keep only one model resident in a llama.cpp router, configure its server with <code>--models-max 1</code>.</p>
      <p className="field-hint">Every paper is rated before the shortlist: highly relevant, relevant, or somewhat relevant. Irrelevant papers are automatically excluded; the strongest matches appear first.</p>
      <div className="button-row">
        <label><input type="checkbox" checked={external && webEnabled} disabled={!webEnabled} onChange={(event) => setExternal(event.target.checked)} /> Allow arXiv and web discovery</label>
      </div>
      <p className="field-hint">Discovery stops at 500 papers, then screens what it found. arXiv requests run one at a time, at least 3 seconds apart. DuckDuckGo retains its existing rate and request limits. OpenAlex is disabled.</p>
      <p className="field-hint">First, approve the model's plan and paper/comparison limits. Highly relevant and relevant papers are then collected and read automatically, including shared references after screening. Ambiguous publication identities are skipped with a visible notice, not guessed. Results save to the track's literature section or a local note; existing paper summaries and notes are kept.</p>
      </details>
      <button className="button primary" type="submit" disabled={busy || !validModels || !title.trim() || !objective.trim()}>Propose review plan</button>
    </form> : null}
    {reviews.length && !creating ? <div className="review-workspace">
      <nav className="review-list" aria-label="Reviews">
        <span className="review-eyebrow">Your reviews</span>
        {reviews.map((review) => <button key={review.id} disabled={busy} aria-pressed={selected?.id === review.id}
          onClick={() => setSelectedId(review.id)}>
          <strong>{review.title}</strong><span>{label(review.stage)} · {label(review.status)}</span>
          <small>{review.candidate_count} discovered · {review.selected_count} automatically selected</small>
        </button>)}
      </nav>
      {selected ? <ReviewDetails key={selected.id} review={selected} busy={busy} clear={clear}
        providers={providers}
        control={(payload) => perform(() => reviewsApi.control(selected.id, { ...payload, expected_revision: selected.revision }))} /> : null}
    </div> : null}
  </section>;
}

const validSummary = (role: SummaryRole | null) => !role || (
  !!role.model_reference.provider_profile_id && !!role.model_reference.model
  && Number.isSafeInteger(role.max_output_tokens) && role.max_output_tokens >= 512 && role.max_output_tokens <= 26214
  && Number.isSafeInteger(role.parallel_requests ?? 1) && (role.parallel_requests ?? 1) >= 1 && (role.parallel_requests ?? 1) <= 32
);
const validParallelRequests = (value: number) => Number.isSafeInteger(value) && value >= 1 && value <= 32;

function SummaryModelFields({ providers, value, onChange, disabled }: {
  providers: Provider[];
  value: SummaryRole | null;
  onChange: (value: SummaryRole | null) => void;
  disabled: boolean;
}) {
  const efforts = providers.find((provider) => provider.id === value?.model_reference.provider_profile_id)
    ?.models.find((model) => model.name === value?.model_reference.model)?.reasoning_efforts ?? [];
  return <fieldset disabled={disabled}>
    <legend>Paper summaries</legend>
    <label><input type="checkbox" checked={!!value} onChange={(event) => onChange(event.target.checked
      ? { model_reference: {}, max_output_tokens: 26214, parallel_requests: 1, reasoning_effort: null } : null)} />
      Use a separate compact summary model</label>
    {value ? <div className="review-models">
      <label>Summary model<ModelSelect ariaLabel="Summary model" options={capabilityOptions(providers, 'chat')}
        value={value.model_reference} disabled={disabled}
        onChange={(model_reference) => onChange({ ...value, model_reference, reasoning_effort: null })} /></label>
      <label>Summary output token limit<input type="number" min={512} max={26214} step={1} required
        value={value.max_output_tokens} onChange={(event) => onChange({ ...value, max_output_tokens: Number(event.target.value) })} /></label>
      <label>Parallel summaries<input type="number" min={1} max={32} step={1} required
        value={value.parallel_requests ?? 1} onChange={(event) => onChange({ ...value, parallel_requests: Number(event.target.value) })} /></label>
      <label>Summary reasoning<select value={value.reasoning_effort ?? ''}
        onChange={(event) => {
          const effort = efforts.find((item) => item === event.target.value);
          onChange({ ...value, reasoning_effort: effort ?? null });
        }}>
        <option value="">Provider default</option>
        {efforts.map((effort) => <option key={effort} value={effort}>{effort}</option>)}
      </select></label>
    </div> : null}
    <p className="field-hint">{value
      ? 'Plain-text summaries with flexible section and sentence guidance, without content checks or repair calls. Context is capped at 128K (131,072 tokens) per request, or the smaller configured model context; 20% is reserved for output, up to 26,214 tokens including reasoning. Safety and prompt overhead also reduce available input. Papers can run concurrently on the same summary model; other models wait. Your server must support the selected parallel slots and context per slot. Oversized papers use bounded packets combined locally. Choose low or no reasoning when supported.'
      : 'Uses the existing main-model reading, combination and audit workflow. Discovery, screening and page selection are unchanged in either mode.'}</p>
  </fieldset>;
}

function ReviewDetails({ review, busy, control, clear, providers }: {
  review: Review;
  busy: boolean;
  control: (payload: Omit<ReviewControl, 'expected_revision'>) => Promise<void>;
  clear: () => Promise<void>;
  providers: Provider[];
}) {
  const [workBudget, setWorkBudget] = useState({ screened_papers: 100, read_papers: 20, comparison_passes: 1 });
  const [resumeStage, setResumeStage] = useState(review.stage);
  const [main, setMain] = useState<ModelSelectValue>(review.main_model);
  const [mainParallelRequests, setMainParallelRequests] = useState(review.main_parallel_requests ?? 1);
  const [summary, setSummary] = useState<SummaryRole | null>(review.summary_model ?? null);
  const [offset, setOffset] = useState(0);
  const [sort, setSort] = useState<CandidateSort>('relevance');
  const [page, setPage] = useState<CandidatePage | null>(null);
  const [priorSelection, setPriorSelection] = useState<string[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);
  const approved = new Set(priorSelection);
  const pendingOffset = offset;
  const visibleCandidates = page?.items ?? [];
  const pendingTotal = page?.total ?? 0;
  const budget = review.suggested_work_budget ?? review.work_budget ?? review.scope?.suggested_work_budget;
  const screenedPapers = budget?.screened_papers ?? 100;
  const readPapers = budget?.read_papers ?? 20;
  const comparisonPasses = budget?.comparison_passes ?? 1;
  const canChangeModels = ['paused', 'blocked', 'completed'].includes(review.status);

  useEffect(() => {
    setWorkBudget({ screened_papers: screenedPapers, read_papers: readPapers, comparison_passes: comparisonPasses });
  }, [review.approval, screenedPapers, readPapers, comparisonPasses]);
  useEffect(() => { setResumeStage(review.stage); }, [review.stage]);
  useEffect(() => { setMain(review.main_model); },
    [review.main_model.provider_profile_id, review.main_model.model]);
  useEffect(() => { setMainParallelRequests(review.main_parallel_requests ?? 1); },
    [review.main_parallel_requests]);
  useEffect(() => { setSummary(review.summary_model ?? null); },
    [review.summary_model?.model_reference.provider_profile_id, review.summary_model?.model_reference.model,
      review.summary_model?.max_output_tokens, review.summary_model?.reasoning_effort, review.summary_model?.parallel_requests]);
  useEffect(() => {
    if (!review.candidate_count) { setPage(null); return; }
    let active = true;
    setLoading(true);
    reviewsApi.candidates(review.id, offset, sort).then((result) => {
      if (!active) return;
      setPage(result);
      setPriorSelection(result.selected_ids);
      setError(null);
    }).catch((reason) => { if (active) setError(reason); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [review.id, review.revision, review.candidate_count, offset, sort]);

  return <div className="review-detail">
    <header className="review-heading"><div><span className="review-eyebrow">{label(review.stage)}</span><h2>{review.title}</h2></div>
      <StatusPill value={review.status} /></header>
    <div className="review-utility">
    <Link to={`/deep-work?review=${encodeURIComponent(review.id)}${review.track_id ? `&track=${encodeURIComponent(review.track_id)}` : ''}`}>Link to this review</Link>
      <a href={reviewsApi.exportUrl(review.id)} download>Export evidence JSON</a>
      <button className="button ghost small review-clear" disabled={busy} onClick={() => void clear()}>Clear review</button>
    </div>
    <div className="review-budget">
      <span>Screened papers: {review.work_used?.screened_papers ?? 0} / {review.work_budget?.screened_papers ?? 'not approved'}</span>
      <span>Collection/read allowance used: {review.work_used?.read_papers ?? 0} / {review.work_budget?.read_papers ?? 'not approved'}</span>
      <span>Comparison passes: {review.work_used?.comparison_passes ?? 0} / {review.work_budget?.comparison_passes ?? 'not approved'}</span>
    </div>
    <p className="review-objective">{review.objective}</p>
    <dl className="review-metrics">
      <div><dt>Discovered</dt><dd>{review.candidate_count}<small> / 500</small></dd></div>
      <div><dt>Rated</dt><dd>{review.screened_count}</dd></div>
      <div><dt>Excluded</dt><dd>{review.excluded_count}</dd></div>
      <div><dt>Automatically selected</dt><dd>{review.selected_count}</dd></div>
    </dl>
    <p className="field-hint">Main: {review.main_model.model} (up to {review.main_parallel_requests ?? 1} parallel). Decision: {review.decision_model?.model_reference.model ?? 'main model (batched)'}. {review.evidence_count} verified source excerpts.</p>
    <p className="field-hint">Paper summaries: {review.summary_model
      ? `${review.summary_model.model_reference.model} (up to ${review.summary_model.parallel_requests ?? 1} parallel, ${review.summary_model.max_output_tokens} output tokens maximum, 128K context maximum)`
      : 'main model, multi-pass source auditing'}.</p>
    <section aria-label="Main model settings">
      <h3>Main model for remaining research</h3>
      <label>Main research model<ModelSelect ariaLabel="Resume main research model"
        options={capabilityOptions(providers, 'chat')} value={main} onChange={setMain}
        disabled={busy || !canChangeModels} /></label>
      <label>Main model parallel requests<input type="number" min={1} max={32} step={1} required
        value={mainParallelRequests} disabled={busy || !canChangeModels}
        onChange={(event) => setMainParallelRequests(Number(event.target.value))} /></label>
      {canChangeModels
        ? <p className="field-hint">Choose a model, then Resume review to continue from the saved stage. Collected papers, completed summaries, verified evidence and approved work limits are retained. Earlier outputs keep their original model provenance.</p>
        : <><p className="field-hint">Pause before switching the main model. Completed work is retained, and pending scope or work-limit approvals still apply.</p>
          <button className="button secondary" disabled={busy} onClick={() => void control({ action: 'pause' })}>
            Pause to switch main model
          </button></>}
    </section>
    <section aria-label="Summary model settings">
      <h3>Summary model for unfinished papers</h3>
      <SummaryModelFields providers={providers} value={summary} onChange={setSummary}
        disabled={busy || !canChangeModels} />
      {canChangeModels
        ? <p className="field-hint">Choose the summary model above, then Resume review to apply it. Completed paper summaries and earlier exploration are retained.</p>
        : <><p className="field-hint">Pause the review to choose a separate summary model. The current request is stopped safely; completed work is retained. Your selection applies when you resume.</p>
          <button className="button secondary" disabled={busy} onClick={() => void control({ action: 'pause' })}>
            Pause to choose summary model
          </button></>}
    </section>
    {review.status === 'running' ? <div className="review-progress" role="status" aria-live="polite">
      <Loading label={`${label(review.stage)} in progress...`} />
      {review.stage === 'screening' && review.candidate_count > 0
        ? <><progress aria-label="Paper screening progress" value={review.screened_count} max={review.candidate_count} />
          <span>{review.screened_count} of {review.candidate_count} papers rated</span>
          <ProgressTiming key={`${review.id}:${review.stage}`} count={review.screened_count}
            total={review.candidate_count} running /></>
        : <progress aria-label={`${label(review.stage)} progress`} />}
      {review.stage !== 'screening' || !review.candidate_count
        ? <p className="field-hint">Time remaining unavailable: this stage has no fixed progress total.</p> : null}
      <p className="field-hint">Updates after each completed decision. You can leave this page without stopping the review.</p>
    </div> : null}
    {review.screened_count > 0 ? <p className="field-hint">
      {review.screened_count} papers rated; {review.excluded_count} irrelevant papers automatically excluded.
      Excluded records remain in the evidence export, not the reading shortlist.
    </p> : null}
    <ol className="review-stages" aria-label="Research stages">{REVIEW_STAGES.map((stage) =>
      <li key={stage} aria-current={review.stage === stage ? 'step' : undefined}>
        {label(stage)}<small>{review.stage_history[stage] ?? (stage === review.stage ? label(review.status) : 'not started')}</small>
      </li>)}</ol>
    <ErrorNotice error={review.error} />
    <ErrorNotice error={error} />
    {review.notices.length ? <details><summary>Coverage and source notices ({review.notices.length})</summary>
      <ul>{review.notices.map((notice, index) => <li key={index}>{notice}</li>)}</ul></details> : null}
    {review.scope ? <details open={review.approval === 'plan'}>
      <summary>Proposed scope and search waves</summary>
      <p>{review.scope.rationale}</p>
      <ul>{review.scope.questions.map((question, index) => <li key={index}>{question}</li>)}</ul>
      <p>Include: {review.scope.inclusion_criteria.join('; ') || 'Not specified'}</p>
      <p>Exclude: {review.scope.exclusion_criteria.join('; ') || 'Not specified'}</p>
      <ul>{review.scope.queries.map((query, index) => <li key={index}>{query.provider}: {query.query} ({query.pages} suggested pages)</li>)}</ul>
    </details> : null}
    {review.status === 'awaiting_approval' && (review.approval === 'plan' || review.approval === 'budget') ? <div className="review-approval">
      <h3>{review.approval === 'plan' ? 'Approve the research scope' : 'Review needs a work-budget extension'}</h3>
      {(['screened_papers', 'read_papers', 'comparison_passes'] as const).map((kind) => <label key={kind}>
        {kind === 'screened_papers' ? 'Papers screened limit' : kind === 'read_papers' ? 'Papers collected/read limit (including references)' : 'Cross-paper comparison passes limit'}
        <input type="number" min={Math.max(1, review.work_used?.[kind] ?? 0)} max={kind === 'comparison_passes' ? 100 : 5000} step={1}
          value={workBudget[kind]} onChange={(event) => setWorkBudget((current) => ({ ...current, [kind]: Number(event.target.value) }))} />
      </label>)}
      <p className="field-hint">Distinct papers count once across retries and resume, including supplemental references. One comparison pass compares the read set against the research questions, not every pair or model chunk. Source/context safety remains automatic.</p>
      <button className="button primary" disabled={busy || Object.entries(workBudget).some(([kind, value]) =>
        !Number.isSafeInteger(value) || value < Math.max(1, review.work_used?.[kind as keyof typeof workBudget] ?? 0))}
        onClick={() => void control({ action: review.approval === 'plan' ? 'approve_plan' : 'extend_budget', work_budget: workBudget })}>
        {review.approval === 'plan' ? 'Approve plan and work limits' : 'Extend work limits'}
      </button>
    </div> : null}
    <div className="button-row review-controls">
      {review.approval === 'analysis' && review.status === 'awaiting_approval' ? <>
        <button className="button primary" disabled={busy}
          onClick={() => void control({ action: 'extend_analysis' })}>Approve up to 20 more source pages</button>
        <button className="button secondary" disabled={busy || review.evidence_count === 0}
          onClick={() => void control({ action: 'write_report' })}>Write report with current evidence</button>
      </> : null}
      {!review.references_skipped && review.status !== 'completed'
        && REVIEW_STAGES.indexOf(review.stage) <= REVIEW_STAGES.indexOf('reference_collection')
        ? <button className="button secondary" disabled={busy}
          onClick={() => void control({ action: 'skip_references' })}>Skip references</button> : null}
      {review.status !== 'completed' && review.status !== 'paused' ? <button className="button secondary" disabled={busy} onClick={() => void control({ action: 'pause' })}>Pause review</button> : null}
      {review.status !== 'completed' ? <button className="button secondary" disabled={busy} onClick={() => void control({ action: 'stop_stage' })}>Stop stage and use valid partial results</button> : null}
      {(['paused', 'blocked', 'completed'].includes(review.status) || (review.status === 'awaiting_approval' && review.approval !== 'budget')) ? <>
        {review.approval !== 'budget' ? <label>Resume from<select value={resumeStage} onChange={(event) => {
          const stage = REVIEW_STAGES.find((item) => item === event.target.value);
          if (stage) setResumeStage(stage);
        }}>{REVIEW_STAGES.slice(0, REVIEW_STAGES.indexOf(review.stage) + 1).map((stage) => <option key={stage} value={stage}>{label(stage)}</option>)}</select></label> : null}
        <button className="button primary" disabled={busy || !validSummary(summary)
          || !validParallelRequests(mainParallelRequests) || !main.provider_profile_id || !main.model} onClick={() => void control({
          action: 'resume', stage: review.approval === 'budget' ? null : resumeStage,
          ...(canChangeModels && (main.provider_profile_id !== review.main_model.provider_profile_id
            || main.model !== review.main_model.model) ? { main_model: main } : {}),
          ...(canChangeModels && mainParallelRequests !== (review.main_parallel_requests ?? 1)
            ? { main_parallel_requests: mainParallelRequests } : {}),
          ...(['paused', 'blocked', 'completed'].includes(review.status) ? { summary_model: summary } : {}),
        })}>
          {review.approval === 'plan' || review.approval === 'budget' || review.approval === 'analysis'
            ? 'Return to pending approval' : 'Resume review'}
        </button>
      </> : null}
    </div>
    {review.references_skipped ? <p className="field-hint">Reference expansion skipped. Initial-paper work and completed results are retained; the review continues to synthesis without further reference lookup or collection.</p> : null}
    <ReviewActivityPanel key={`activity:${review.id}`} review={review} />
    {review.output_path ? <div className="review-saved">
      <Link className="button primary small" to={review.track_id
        ? `/tracks?id=${encodeURIComponent(review.track_id)}&section=literature&review=${encodeURIComponent(review.id)}&revision=${review.revision}`
        : `/library/notes?path=${encodeURIComponent(review.output_path)}`}>Open saved review</Link>
      <p className="field-hint">{review.output_path}</p>
    </div> : null}
    {review.selected_count > 0 && REVIEW_STAGES.indexOf(review.stage) >= REVIEW_STAGES.indexOf('reading_plan')
      ? <CapturedPages key={review.id} review={review} /> : null}
    {REVIEW_STAGES.indexOf(review.stage) >= REVIEW_STAGES.indexOf('references')
      ? <ReferenceTable review={review} busy={busy} control={control} /> : null}
    {review.candidate_count ? <section className="review-shortlist" aria-label="Paper shortlist">
      <div className="review-heading"><div><h3>Paper shortlist</h3><p className="field-hint">Highly relevant and relevant papers are automatically collected and read. Somewhat relevant papers remain metadata only.</p></div><label>Order by<select value={sort} onChange={(event) => {
        const value = event.target.value;
        if (value === 'relevance' || value === 'year' || value === 'title') { setSort(value); setOffset(0); }
      }}><option value="relevance">Relevance</option><option value="year">Newest first</option><option value="title">Title</option></select></label></div>
      {loading ? <Loading label="Loading candidates..." /> : null}
      {!loading && page?.total === 0 ? <p>No papers remain in the shortlist. Review the exclusions in the evidence export or start a new review with a revised objective.</p> : null}
      <div className="review-candidates">
        {visibleCandidates.map((candidate) => <CandidateCard key={candidate.id} candidate={candidate} reviewId={review.id} revision={review.revision}
          checked={approved.has(candidate.id)} selectable={false}
          linkable={review.status !== 'running' && !busy}
          onLink={(documentId) => control({ action: 'link_paper', candidate_ids: [candidate.id], document_id: documentId })}
          onSelect={() => {}} />)}
      </div>
      <div className="button-row">
        <button className="button ghost small" disabled={loading || pendingOffset === 0} onClick={() => setOffset(Math.max(0, pendingOffset - 25))}>Previous papers</button>
        <span>{pendingTotal ? `${pendingOffset + 1}-${pendingOffset + visibleCandidates.length} of ${pendingTotal}` : '0 papers'}</span>
        <button className="button ghost small" disabled={loading || pendingOffset + 25 >= pendingTotal} onClick={() => setOffset(pendingOffset + 25)}>Next papers</button>
      </div>
    </section> : null}
  </div>;
}

function PaperSummaries({ paper }: { paper: PaperReview }) {
  const [showPackets, setShowPackets] = useState(false);
  const renderSummary = (summary: Record<string, unknown>) => {
    const sections = (summary.sections ?? []) as { title: string; content: string }[];
    const limitations = (summary.limitations ?? []) as string[];
    return <>
      {sections.map((section, index) => <div key={index}>
        <MarkdownViewer content={`#### ${section.title}\n\n${section.content}`} />
      </div>)}
      {limitations.length ? <details><summary>Limitations and coverage</summary>
        <ul>{limitations.map((value, index) => <li key={index}>{value}</li>)}</ul>
      </details> : null}
    </>;
  };
  return <>
    {paper.selective_summary ? <section aria-label="Selective paper summary">
      <h4>Selective paper summary</h4>
      <p>Admitted source spans only; not a canonical full-paper summary.</p>
      {renderSummary(paper.selective_summary)}
    </section> : null}
    {(paper.partial_summaries ?? []).length ? <details onToggle={(event) => setShowPackets(event.currentTarget.open)}>
      <summary>Paper summary packets ({paper.partial_summaries?.length})</summary>
      {showPackets ? (paper.partial_summaries ?? []).map((summary, index) => <section key={index}>
        <h4>Source packet {index + 1}</h4>{renderSummary(summary)}
      </section>) : null}
    </details> : null}
  </>;
}

function PaperReader({ reviewId, candidateId, title, onClose }: {
  reviewId: string; candidateId: string; title: string; onClose: () => void;
}) {
  const [paper, setPaper] = useState<PaperReview | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [refresh, setRefresh] = useState(0);
  const [showSources, setShowSources] = useState(false);
  const reader = useRef<HTMLElement>(null);
  useEffect(() => {
    reader.current?.scrollIntoView({ block: 'start', behavior: 'smooth' });
    reader.current?.focus({ preventScroll: true });
  }, []);
  useEffect(() => {
    let active = true;
    setPaper(null);
    setError(null);
    setShowSources(false);
    void reviewsApi.paper(reviewId, candidateId).then((result) => {
      if (active) setPaper(result);
    }).catch((reason: unknown) => {
      if (active) setError(reason);
    });
    return () => { active = false; };
  }, [reviewId, candidateId, refresh]);
  return <section className="review-paper-reader" aria-label={`Summary for ${title}`} ref={reader} tabIndex={-1}>
    <div className="review-heading">
      <h3>{title}</h3>
      <div className="button-row">
        <button className="button ghost small" onClick={() => setRefresh((value) => value + 1)}>Refresh paper</button>
        <button className="button ghost small" onClick={onClose}>Close paper</button>
      </div>
    </div>
    <ErrorNotice error={error} />
    {!paper && !error ? <Loading label="Loading paper summary..." /> : null}
    {paper ? <>
      <ErrorNotice error={paper.error} />
      {paper.selective_summary ? <PaperSummaries paper={paper} /> : <p>No summary saved yet.</p>}
      <details onToggle={(event) => setShowSources(event.currentTarget.open)}>
        <summary>Source details and page decisions</summary>
        {showSources ? <>
          {paper.operation ? <p role="status">{paper.operation}</p> : null}
          <p>Source version: {paper.source_version}. Read pages: {paper.read_pages.join(', ') || 'none'}.
            Partial pages: {paper.partial_pages.join(', ') || 'none'}.</p>
          <p>{paper.evidence_count} verified excerpts. Summary prose itself is not verified source evidence.</p>
          <ul>{paper.pages.map((page) => <li key={page.page}>Page {page.page}: {page.action}
            {page.reference_page ? ' · reference page' : ''} — {page.reason}</li>)}</ul>
          {(paper.findings ?? []).map((finding, index) => <div key={index}><p>{String(finding.finding ?? '')}</p>
            {finding.quote ? <blockquote>{String(finding.quote)}</blockquote>
              : <p>Source quotation pending; verify against the original page during analysis.</p>}
            <small>Page {String(finding.page ?? '')}</small></div>)}
        </> : null}
      </details>
    </> : null}
  </section>;
}

function CapturedPages({ review }: { review: Review }) {
  const [page, setPage] = useState<PaperProgressPage | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [offset, setOffset] = useState(0);
  const [summariesOnly, setSummariesOnly] = useState(true);
  const [selected, setSelected] = useState<{ candidate_id: string; title: string } | null>(null);
  const revision = useRef(review.revision);
  revision.current = review.revision;
  useEffect(() => {
    let active = true;
    let inFlight = false;
    let loadedRevision = -1;
    setPage(null);
    const load = async () => {
      if (inFlight || loadedRevision === revision.current) return;
      inFlight = true;
      const requestedRevision = revision.current;
      try {
        const result = await reviewsApi.paperProgress(review.id, offset, summariesOnly);
        if (active) {
          if (offset > 0 && offset >= result.total) {
            setOffset(Math.floor(Math.max(0, result.total - 1) / 25) * 25);
          } else {
            setPage(result); setError(null); loadedRevision = requestedRevision;
          }
        }
      } catch (reason) {
        if (active) setError(reason);
      } finally {
        inFlight = false;
      }
    };
    void load();
    const timer = window.setInterval(() => void load(), 3000);
    return () => { active = false; window.clearInterval(timer); };
  }, [review.id, offset, summariesOnly]);
  const total = page?.counts.total ?? 0;
  return <section aria-label="Paper summaries and progress">
    <h3>Paper collection and reading progress</h3>
    <ErrorNotice error={error} />
    {!page && !error ? <Loading label="Loading paper progress..." /> : null}
    {page && total ? <div className="review-progress" aria-label="Paper progress">
      {([
        ['PDFs collected or reused', page.counts.collected],
        ['Source text prepared', page.counts.prepared],
        ['Page decisions complete', page.counts.plan_complete],
        ['Papers read and summarized', page.counts.summarized],
      ] as const).map(([title, count]) => <div key={title}>
        <span>{title}: {count} / {total}</span>
        <progress aria-label={title} value={count} max={total} />
        <ProgressTiming key={`${review.id}:${title}`} count={count} total={total} running={review.status === 'running'} />
      </div>)}
      <p className="field-hint">Counts refer to selected papers, not the work allowance. Collection and extraction happen before page decisions and selective reading; collected does not mean read.</p>
      {page.counts.failed ? <p role="status">{page.counts.failed} {page.counts.failed === 1 ? 'paper needs' : 'papers need'} attention; choose All papers to inspect errors.</p> : null}
      {page.operations.map((paper) => <p role="status" key={paper.candidate_id}>{paper.operation}: {paper.title}</p>)}
    </div> : null}
    {selected ? <PaperReader key={`${review.id}:${selected.candidate_id}`} reviewId={review.id}
      candidateId={selected.candidate_id} title={selected.title} onClose={() => setSelected(null)} /> : null}
    <div className="review-heading">
      <h3>Paper summaries</h3>
      <label>Show <select value={summariesOnly ? 'summaries' : 'all'} onChange={(event) => {
        setSummariesOnly(event.target.value === 'summaries'); setOffset(0);
      }}>
        <option value="summaries">Summaries ready</option>
        <option value="all">All papers</option>
      </select></label>
    </div>
    <p className="field-hint">Summary text loads only when you open a paper.</p>
    <ul className="review-paper-list">
      {page?.items.map((paper) => <li key={paper.candidate_id}>
        <div><strong>{paper.title || paper.candidate_id}</strong><small>
          {paper.error ? 'Needs attention' : paper.summary_complete ? 'Summary ready' : paper.operation
            || (paper.plan_complete ? 'Waiting for summary' : paper.prepared ? 'Prepared' : paper.collected ? 'Collected' : 'Queued')}
        </small></div>
        <button className="button ghost small" aria-label={`${paper.summary_complete ? 'View summary' : 'View progress'} for ${paper.title}`}
          onClick={() => setSelected(paper)}>{paper.summary_complete ? 'View summary' : 'View progress'}</button>
      </li>)}
    </ul>
    {page && !page.total ? <p>{summariesOnly ? 'No summaries saved yet. Reading progress appears above.' : 'No selected papers yet.'}</p> : null}
    <div className="button-row">
      <button className="button ghost small" disabled={!page || offset === 0}
        onClick={() => setOffset(Math.max(0, offset - 25))}>Previous summaries</button>
      <span>{page?.total ? `${offset + 1}-${offset + page.items.length} of ${page.total}` : '0 papers'}</span>
      <button className="button ghost small" disabled={!page || page.next_offset == null}
        onClick={() => { if (page?.next_offset != null) setOffset(page.next_offset); }}>Next summaries</button>
    </div>
  </section>;
}

function ReferenceTable({ review, busy, control }: {
  review: Review; busy: boolean; control: (command: Omit<ReviewControl, 'expected_revision'>) => Promise<void>;
}) {
  const [rows, setRows] = useState<ReviewReference[]>([]);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    let active = true;
    void reviewsApi.references(review.id).then((items) => {
      if (active) { setRows(items); setError(null); }
    }).catch((reason) => { if (active) setError(reason); });
    return () => { active = false; };
  }, [review.id, review.revision]);
  const choosing = review.status === 'awaiting_approval' && review.approval === 'references';
  const collecting = review.status === 'awaiting_approval' && review.approval === 'reference_pdfs';
  return <section aria-label="Captured references">
    <h3>Captured references</h3>
    <p>Counts are distinct originally selected citing papers, not extraction attempts. Supplemental PDFs never expand this list.</p>
    <a href={reviewsApi.referencesExportUrl(review.id)} download>Export references CSV</a>
    <ErrorNotice error={error} />
    <table className="review-reference-table"><thead><tr><th>Automatic lookup</th><th>Reference</th><th>Citing original papers</th><th>Metadata / decision</th></tr></thead>
      <tbody>{rows.map((row) => <tr key={row.id}>
        <td>{row.selected ? 'Selected automatically' : 'Not shared'}</td>
        <td><strong>{row.title}</strong><p>{(row.authors ?? []).join(', ')} {row.year ?? ''}</p><p>{row.doi ?? row.arxiv_id ?? 'No printed identifier'}</p>
          <details><summary>Captured citation evidence</summary>{row.citations.map((citation, index) =>
            <p key={index}>{String(citation.title ?? citation.candidate_id)} · page {String(citation.page)} · source {String(citation.source_version)}<br />{String(citation.quote)}</p>)}</details>
        </td>
        <td>{row.citing_count}</td>
        <td>{row.metadata ? <><a href={row.metadata.url ?? undefined} target="_blank" rel="noreferrer">{row.metadata.title}</a>
          <p>{row.metadata.abstract ?? 'No abstract available'}</p></> : <p>Unresolved</p>}
          {row.screening ? <p>{label(row.screening.relevance)}: {row.screening.reason}</p> : <p>Not screened</p>}
          {row.error ? <p role="alert">{row.error}</p> : null}
          {row.document_id ? <Link to={`/library?paper=${encodeURIComponent(row.document_id)}`}>Open supplemental PDF</Link> : null}
        </td>
      </tr>)}</tbody></table>
    {choosing || collecting ? <div className="review-approval">
      <p>{choosing ? 'Shared references are looked up automatically. Continue this saved review to apply automatic selection.'
        : 'Relevant verified references are collected and read automatically. Ambiguous identities are skipped with visible notices.'}</p>
      <button className="button primary" disabled={busy} onClick={() => void control({
        action: 'resume',
      })}>Continue automatically</button>
    </div> : null}
  </section>;
}

function CandidateCard({ candidate, reviewId, revision, checked, selectable, linkable, onSelect, onLink }: {
  candidate: Candidate; reviewId: string; revision: number; checked: boolean; selectable: boolean;
  linkable: boolean;
  onSelect: (checked: boolean) => void;
  onLink: (documentId: string) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [paper, setPaper] = useState<PaperReview | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [localQuery, setLocalQuery] = useState(candidate.title);
  const [localPapers, setLocalPapers] = useState<LocalPaper[] | null>(null);
  const [localId, setLocalId] = useState('');
  const [searching, setSearching] = useState(false);
  const searchVersion = useRef(0);
  useEffect(() => () => { searchVersion.current += 1; }, []);
  const findLocal = async () => {
    const version = ++searchVersion.current;
    setSearching(true);
    setError(null);
    try {
      const matches = await reviewsApi.localPapers(localQuery.trim());
      if (version !== searchVersion.current) return;
      setLocalPapers(matches);
      setLocalId('');
    } catch (reason) {
      if (version === searchVersion.current) setError(reason);
    } finally {
      if (version === searchVersion.current) setSearching(false);
    }
  };
  useEffect(() => {
    if (!open || !candidate.selected) return;
    let active = true;
    reviewsApi.paper(reviewId, candidate.id).then((value) => { if (active) { setPaper(value); setError(null); } })
      .catch((reason) => { if (active) setError(reason); });
    return () => { active = false; };
  }, [open, candidate.id, candidate.selected, reviewId, revision]);
  return <article className="review-candidate">
    <div className="review-candidate-row">
    <button type="button" className="review-candidate-select" aria-label={`Explore ${candidate.title}`} aria-pressed={checked}
      disabled={!selectable} onClick={() => onSelect(!checked)}>
      <strong>{candidate.title}</strong>
      <span className="field-hint"><span className="review-rating" data-relevance={candidate.screening?.relevance}>{candidate.screening ? label(candidate.screening.relevance).toLowerCase() : 'Not screened'}</span> {candidate.year ?? 'Year unknown'} - {candidate.screening?.kind ?? candidate.source_kind ?? 'Unclassified'}</span>
      {candidate.screening ? <span className="field-hint">{candidate.screening.reason}</span> : null}
    </button>
    <button type="button" className="button secondary review-candidate-expand" aria-label={`Details for ${candidate.title}`}
      aria-expanded={open} aria-controls={`candidate-details-${candidate.id}`} onClick={() => setOpen(!open)}>
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
        <path d={open ? 'M6 9l6 6 6-6' : 'M9 6l6 6-6 6'} />
      </svg>
    </button>
    </div>
    {candidate.document_id ? <p className="field-hint"><Link to={`/library?paper=${encodeURIComponent(candidate.document_id)}`}>Open local paper</Link></p> : null}
    <div id={`candidate-details-${candidate.id}`} hidden={!open}>
      {candidate.url && /^https?:\/\//i.test(candidate.url) ? <a href={candidate.url} target="_blank" rel="noreferrer">Publication source</a> : null}
      <p>{(candidate.authors ?? []).join(', ')}</p><p>{candidate.abstract ?? 'No abstract available; relevance is uncertain.'}</p>
      <ul>{(candidate.discovery ?? []).map((trail, index) => <li key={index}>{trail}</li>)}</ul>
      <ErrorNotice error={candidate.error ?? error} />
      {linkable ? <div className="review-local-source">
        <p>Missing or incorrect PDF? <Link to="/library/papers">Upload it in the paper library</Link>, then explicitly link its local copy here. Titles are never matched automatically.</p>
        <label>Find local paper<input value={localQuery} maxLength={1000} onChange={(event) => setLocalQuery(event.target.value)} /></label>
        <button className="button secondary small" disabled={searching || !localQuery.trim()} onClick={() => void findLocal()}>Find local copy</button>
        {localPapers?.length ? <div className="button-row">
          <label>Local paper<select value={localId} onChange={(event) => setLocalId(event.target.value)}>
            <option value="">Select the exact paper</option>
            {localPapers.map((item) => <option key={item.id} value={item.id}>{item.title} - {item.source_filename}</option>)}
          </select></label>
          <button className="button secondary small" disabled={!localId || searching} onClick={() => void onLink(localId)}>Link local paper</button>
        </div> : localPapers ? <p>No matches. Try a narrower title or a phrase from the paper.</p> : null}
      </div> : null}
      {paper ? <><p>{paper.evidence_count} verified excerpts. Fully read pages: {paper.read_pages.join(', ') || 'none'}.
        {' '}Partially read pages: {paper.partial_pages.join(', ') || 'none'}.</p>
        <ul>{paper.pages.map((page) => <li key={page.page}>Page {page.page}: {page.action}{page.reference_page ? ' · reference page' : ''}{paper.read_pages.includes(page.page) && page.action === 'defer' ? ' (subsequently read for follow-up)' : ''} - {page.reason}</li>)}</ul>
        <PaperSummaries paper={paper} />
      </> : null}
    </div>
  </article>;
}
