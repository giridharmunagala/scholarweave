import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useLocation, useNavigate, useNavigationGuard } from '../../app/router';
import { ErrorNotice, IconButton, Loading, PageHeader, StatusPill } from '../../shared/components/Ui';
import { MarkdownViewer } from '../../shared/components/MarkdownViewer';
import { Icon } from '../../shared/components/Icons';
import { LiteratureReviews, ResearchHeading } from '../reviews/LiteratureReviews';
import { reviewsApi, type Review } from '../reviews/api';
import { tracksApi, type Track } from '../tracks/api';
import { chatApi, deepWorkApi, type Conversation, type ConversationDetail, type ModelReference, type Run } from '../chat/api';
import { buildSessionObservability } from '../chat/sessionObservability';
import { buildRunStages } from '../chat/runStages';
import { RunActivity } from '../chat/RunActivity';
import { SessionOverview } from '../chat/SessionMonitor';
import '../chat/chat.css';
import './deep-work.css';

const activeRun = (run: Run) => !['completed', 'failed', 'cancelled'].includes(run.status);
const findings = (run: Run | undefined) => typeof run?.final_output === 'string' ? run.final_output : '';
const WEB_PREFERENCES_KEY = 'scholarweave:deep-work-web-preferences:v1';
const LAST_RESEARCH_KEY = 'scholarweave:last-research:v1';

type WebPreference = { enabled: boolean; explicit: boolean };
type WebPreferenceMap = Record<string, WebPreference>;
type ResearchHistoryItem = {
  title: string;
  conversationId?: string;
  reviewId?: string;
  trackId?: string;
  status: string;
  updatedAt: string;
};

function readWebPreferences(): WebPreferenceMap {
  try {
    const stored: unknown = JSON.parse(window.sessionStorage.getItem(WEB_PREFERENCES_KEY) ?? '{}');
    if (!stored || typeof stored !== 'object' || Array.isArray(stored)) return {};
    return Object.fromEntries(Object.entries(stored).flatMap(([key, value]) => {
      if (!value || typeof value !== 'object' || Array.isArray(value)) return [];
      const preference = value as Partial<WebPreference>;
      if (typeof preference.enabled !== 'boolean' || typeof preference.explicit !== 'boolean') return [];
      return [[key, { enabled: preference.enabled, explicit: preference.explicit }]];
    }));
  } catch {
    return {};
  }
}

function storedWebPreference(reviewId?: string, conversationId?: string): WebPreference | undefined {
  const preferences = readWebPreferences();
  return (reviewId ? preferences[`review:${reviewId}`] : undefined) ??
    (conversationId ? preferences[`conversation:${conversationId}`] : undefined);
}

function saveWebPreference(preference: WebPreference, reviewId?: string, conversationId?: string) {
  if (!reviewId && !conversationId) return;
  const preferences = readWebPreferences();
  if (reviewId) preferences[`review:${reviewId}`] = preference;
  if (conversationId) preferences[`conversation:${conversationId}`] = preference;
  try { window.sessionStorage.setItem(WEB_PREFERENCES_KEY, JSON.stringify(preferences)); } catch { /* Storage is optional. */ }
}

function webQuery(preference: WebPreference): Record<string, string> {
  return preference.explicit ? { web: preference.enabled ? '1' : '0' } : {};
}

function initialWebPreference(params: URLSearchParams): WebPreference {
  if (params.has('web')) return { enabled: params.get('web') !== '0', explicit: true };
  return storedWebPreference(params.get('review') ?? undefined, params.get('conversation') ?? undefined) ??
    { enabled: true, explicit: false };
}

function workspaceContext(content: string, review: Review | undefined): string {
  if (!review) return content;
  return [
    content,
    'Current research context: inspect and reuse this work rather than creating a duplicate review.',
    `Review ID: ${review.id}`,
    `Research question: ${review.objective}`,
    `Reading focus: ${review.question_focus || review.objective}`,
    `Stage: ${review.stage}; status: ${review.status}; pending approval: ${review.approval || 'none'}.`,
    `Selected papers: ${review.selected_count}. Coverage: ${review.reading_mode}; targeted reading covers partial passages, not full papers.`,
    review.output_path ? `Saved synthesis: ${review.output_path}` : '',
    review.provisional_path ? `Progressive findings: ${review.provisional_path}` : '',
    'Do not bypass human approvals. Distinguish source evidence, metadata, inference and missing coverage.',
  ].filter(Boolean).join('\n\n');
}

export default function DeepWorkPage() {
  const { search } = useLocation();
  const navigate = useNavigate();
  const restoration = useMemo(() => {
    const params = new URLSearchParams(search);
    if (['new', 'review', 'conversation', 'track', 'objective', 'question'].some((key) => params.has(key))) {
      return { destination: null, error: null };
    }
    try {
      const stored = window.localStorage.getItem(LAST_RESEARCH_KEY);
      if (!stored) return { destination: null, error: null };
      const saved = new URLSearchParams(stored);
      if (!saved.get('review') && !saved.get('conversation')) throw new Error('Invalid saved research link.');
      for (const key of ['review', 'conversation', 'track', 'web']) {
        const value = saved.get(key);
        if (value && !params.has(key)) params.set(key, value);
      }
      return { destination: `/deep-work?${params}`, error: null };
    } catch (cause) {
      return { destination: null, error: new Error(
        `Could not restore the last opened research. Choose it from Saved research. ${cause instanceof Error ? cause.message : String(cause)}`,
      ) };
    }
  }, [search]);
  useEffect(() => {
    if (restoration.destination) navigate(restoration.destination, { replace: true });
  }, [restoration.destination, navigate]);
  if (restoration.destination) return <Loading label="Restoring research..." />;
  return <>
    <ErrorNotice error={restoration.error} />
    <ResearchWorkspace key={search} params={new URLSearchParams(search)} />
  </>;
}

function ResearchWorkspace({ params }: { params: URLSearchParams }) {
  const navigate = useNavigate();
  const requestedTrackId = params.get('track') ?? '';
  const requestedReview = params.get('new') === '1' ? '' : params.get('review') ?? '';
  const requestedConversation = params.get('new') === '1' ? '' : params.get('conversation') ?? '';
  const [conversationId, setConversationId] = useState(requestedConversation);
  const [reviewId, setReviewId] = useState(requestedReview);
  const [reviews, setReviews] = useState<Review[]>([]);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [current, setCurrent] = useState<ConversationDetail | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [tracks, setTracks] = useState<Track[]>([]);
  const [historyQuery, setHistoryQuery] = useState('');
  const [historyTrack, setHistoryTrack] = useState(requestedTrackId);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [historyError, setHistoryError] = useState<unknown>(null);
  const [loaded, setLoaded] = useState(false);
  const [loadingConversation, setLoadingConversation] = useState(!!requestedConversation);
  const [instruction, setInstruction] = useState('');
  const [intent, setIntent] = useState<'chat' | 'ideation'>('chat');
  const [webPreference, setWebPreference] = useState(() => initialWebPreference(params));
  const web = webPreference.enabled;
  const [busy, setBusy] = useState(false);
  const [paperBusy, setPaperBusy] = useState(false);
  const blocked = busy || paperBusy;
  const [error, setError] = useState<unknown>(null);
  const [receipt, setReceipt] = useState('');
  const preparedConversation = useRef(requestedConversation);
  const version = useRef(0);
  const mutating = useRef(false);
  const mounted = useRef(true);
  const trace = useRef<HTMLDetailsElement>(null);
  const composer = useRef<HTMLTextAreaElement>(null);
  const historyToggle = useRef<HTMLButtonElement>(null);
  const review = reviews.find((item) => item.id === reviewId);
  const trackId = review ? review.track_id ?? '' : requestedTrackId;
  const changeWeb = (enabled: boolean) => {
    const preference = { enabled, explicit: true };
    setWebPreference(preference);
    saveWebPreference(preference, review?.id || reviewId || requestedReview || undefined,
      conversationId || requestedConversation || review?.conversation_id || undefined);
  };
  const latest = runs[runs.length - 1];
  const running = runs.find(activeRun);
  const summary = useMemo(() => buildSessionObservability(runs), [runs]);
  const paperRunning = review?.reading_mode === 'targeted' && review.status === 'running';
  const hasResearch = !!review || !!conversationId;
  useNavigationGuard(!!instruction.trim() || blocked, 'Discard unsent research instructions? Running work will continue.', !blocked);

  useEffect(() => {
    saveWebPreference(webPreference, review?.id || reviewId || requestedReview || undefined,
      conversationId || requestedConversation || review?.conversation_id || undefined);
  }, [webPreference, review?.id, review?.conversation_id, reviewId, requestedReview, conversationId, requestedConversation]);

  useEffect(() => {
    if (!review && (!current || reviewId)) return;
    const linkedConversation = review?.conversation_id || conversationId;
    const selection = new URLSearchParams({
      ...(review ? { review: review.id } : {}),
      ...(linkedConversation ? { conversation: linkedConversation } : {}),
      ...(trackId ? { track: trackId } : {}),
      ...webQuery(webPreference),
    });
    try {
      window.localStorage.setItem(LAST_RESEARCH_KEY, selection.toString());
    } catch (cause) {
      setError(new Error(
        `Could not remember this research. Use its research link or Saved research to reopen it. ${cause instanceof Error ? cause.message : String(cause)}`,
      ));
    }
  }, [review?.id, review?.conversation_id, reviewId, current?.id, conversationId, trackId, webPreference]);

  useEffect(() => {
    mounted.current = true;
    let active = true;
    let inFlight = false;
    const load = async () => {
      if (inFlight || mutating.current) return;
      inFlight = true;
      const request = version.current;
      try {
        const [papers, sessions] = await Promise.all([reviewsApi.list(), deepWorkApi.list()]);
        if (requestedReview && !papers.some((item) => item.id === requestedReview)) {
          papers.unshift(await reviewsApi.get(requestedReview));
        }
        if (!active || request !== version.current) return;
        setReviews((old) => papers.map((item) => {
          const previous = old.find((value) => value.id === item.id);
          return previous && previous.revision > item.revision ? previous : item;
        }));
        setConversations(sessions);
        setHistoryError(null);
        setLoaded(true);
      } catch (reason) {
        if (active && request === version.current) { setHistoryError(reason); setLoaded(true); }
      } finally { inFlight = false; }
    };
    void load();
    void tracksApi.list().then((items) => { if (active) setTracks(items); })
      .catch((reason) => { if (active) setError(reason); });
    const timer = window.setInterval(() => void load(), 3000);
    return () => { active = false; mounted.current = false; window.clearInterval(timer); };
  }, [requestedReview]);

  useEffect(() => {
    if (review?.conversation_id && !conversationId) {
      preparedConversation.current = review.conversation_id;
      setConversationId(review.conversation_id);
    }
    if (!reviewId && conversationId) {
      const linked = reviews.find((item) => item.conversation_id === conversationId);
      if (linked) setReviewId(linked.id);
    }
  }, [review, reviewId, conversationId, reviews]);

  useEffect(() => {
    if (!review || requestedConversation || blocked || instruction.trim()) return;
    const linkedConversation = review.conversation_id || conversationId;
    if (requestedReview === review.id && !linkedConversation) return;
    navigate(`/deep-work?${new URLSearchParams({
      review: review.id, ...(linkedConversation ? { conversation: linkedConversation } : {}),
      ...(trackId ? { track: trackId } : {}), ...webQuery(webPreference),
    })}`, { replace: true });
  }, [review?.id, review?.conversation_id, conversationId, requestedReview, requestedConversation, blocked, instruction, trackId, webPreference, navigate]);

  useEffect(() => {
    let active = true;
    let inFlight = false;
    setCurrent(null); setRuns([]); setLoadingConversation(!!conversationId);
    const load = async () => {
      if (!conversationId || inFlight || mutating.current) return;
      inFlight = true;
      const request = version.current;
      try {
        const [detail, history] = await Promise.all([chatApi.get(conversationId), chatApi.runs(conversationId)]);
        if (!active || request !== version.current) return;
        setCurrent(detail); setRuns(history); setLoadingConversation(false);
      } catch (reason) {
        if (active && request === version.current) { setError(reason); setLoadingConversation(false); }
      } finally { inFlight = false; }
    };
    void load();
    const timer = window.setInterval(() => void load(), 3000);
    return () => { active = false; window.clearInterval(timer); };
  }, [conversationId]);

  const updateReview = useCallback((next: Review | undefined) => {
    if (!next) return;
    version.current += 1;
    setReviews((items) => [next, ...items.filter((item) => item.id !== next.id)]);
    setReviewId(next.id);
    if (next.conversation_id) {
      preparedConversation.current = next.conversation_id;
      setConversationId(next.conversation_id);
    }
  }, []);
  const ensureConversation = async (reference: ModelReference) => {
    if (conversationId) return conversationId;
    if (!preparedConversation.current) {
      const created = await deepWorkApi.create(reference, trackId || undefined);
      preparedConversation.current = created.id;
      setConversations((items) => [created, ...items.filter((item) => item.id !== created.id)]);
    }
    return preparedConversation.current;
  };
  const updateRun = (run: Run) => setRuns((items) => [...items.filter((item) => item.id !== run.id), run]);
  const perform = async (action: () => Promise<void>) => {
    if (blocked) return;
    version.current += 1; mutating.current = true; setBusy(true); setError(null); setReceipt('');
    try { await action(); }
    catch (reason) { if (mounted.current) setError(reason); }
    finally { mutating.current = false; if (mounted.current) { version.current += 1; setBusy(false); } }
  };
  const send = () => perform(async () => {
    const id = await ensureConversation(review?.main_model ?? current?.model_reference ?? {});
    setConversationId(id);
    const content = workspaceContext(instruction.trim(), review);
    if (running) {
      const result = await chatApi.steer(running.id, content);
      setReceipt(`Instruction ${result.status}: ${instruction.trim()}`);
    } else {
      const api = current && current.kind !== 'deep_work' ? chatApi : deepWorkApi;
      const result = await api.send(id, content, { response_effort: 'thorough', web_enabled: web, turn_intent: intent });
      setCurrent({ ...result.conversation, items: current?.items ?? [] });
      updateRun(result.run);
    }
    setInstruction('');
  });
  const pausePapers = async () => {
    if (!paperRunning || !review) return review;
    const paused = await reviewsApi.control(review.id, { action: 'pause', expected_revision: review.revision });
    updateReview(paused);
    return paused;
  };
  const pause = () => perform(async () => {
    const results = await Promise.allSettled([
      running ? chatApi.cancelRun(running.id).then(updateRun) : Promise.resolve(),
      pausePapers(),
    ]);
    const failures = results.flatMap((result) => result.status === 'rejected'
      ? [result.reason instanceof Error ? result.reason.message : String(result.reason)] : []);
    if (failures.length) throw new Error(`Research pause incomplete: ${failures.join('; ')}`);
  });
  const stopAndSummarize = () => perform(async () => {
    const stoppedReview = await pausePapers();
    if (running) {
      const result = await chatApi.stopAndAnswer(running.id);
      updateRun(result.stopped_run); updateRun(result.answer_run);
    } else {
      const id = await ensureConversation(stoppedReview?.main_model ?? current?.model_reference ?? {});
      setConversationId(id);
      const api = current && current.kind !== 'deep_work' ? chatApi : deepWorkApi;
      const result = await api.send(id, workspaceContext(
        'Summarize the findings available so far. Do not restart acquisition or reading. Disclose missing sources and partial coverage.', stoppedReview,
      ), { response_effort: 'thorough', web_enabled: false });
      updateRun(result.run);
    }
  });
  const focusComposer = () => {
    composer.current?.focus();
    composer.current?.scrollIntoView?.({ block: 'center', behavior: 'smooth' });
  };
  const discuss = (draft: string) => {
    setInstruction((previous) => previous.trim() ? `${previous.trim()}\n\n${draft}` : draft); setIntent('chat');
    focusComposer();
  };
  const openActivity = () => {
    if (!trace.current) return;
    trace.current.open = true;
    let parent = trace.current.parentElement?.closest('details');
    while (parent) { parent.open = true; parent = parent.parentElement?.closest('details'); }
    trace.current.scrollIntoView?.({ block: 'nearest' });
  };
  const history = new Map<string, ResearchHistoryItem>();
  for (const item of reviews) {
    const key = item.conversation_id ? `conversation:${item.conversation_id}` : `review:${item.id}`;
    if (!history.has(key)) history.set(key, {
      title: item.title, conversationId: item.conversation_id || undefined, reviewId: item.id,
      trackId: item.track_id || undefined, status: item.status, updatedAt: item.updated_at,
    });
  }
  for (const item of conversations) {
    const key = `conversation:${item.id}`;
    const saved = history.get(key);
    if (!saved) history.set(key, {
      title: item.title, conversationId: item.id, trackId: item.track_id || undefined,
      status: item.status, updatedAt: item.updated_at,
    });
    else history.set(key, {
      ...saved, trackId: saved.trackId || item.track_id || undefined,
      updatedAt: item.updated_at > saved.updatedAt ? item.updated_at : saved.updatedAt,
    });
  }
  const selectedHistory = conversationId ? `conversation:${conversationId}` : reviewId ? `review:${reviewId}` : '';
  const visibleHistory = [...history].filter(([, item]) =>
    (!historyTrack || item.trackId === historyTrack) && item.title.toLowerCase().includes(historyQuery.trim().toLowerCase()),
  ).sort(([, left], [, right]) => right.updatedAt.localeCompare(left.updatedAt));
  const historyUrl = (value: string) => {
    const saved = history.get(value);
    const preference = saved
      ? storedWebPreference(saved.reviewId, saved.conversationId) ?? { enabled: true, explicit: false }
      : webPreference;
    const destinationTrack = saved ? saved.trackId : historyTrack;
    return `/deep-work?${new URLSearchParams({
      ...(destinationTrack ? { track: destinationTrack } : {}), ...(saved?.conversationId ? { conversation: saved.conversationId } : {}),
      ...(saved?.reviewId ? { review: saved.reviewId } : {}), ...(!saved ? { new: '1' } : {}),
      ...webQuery(preference),
    })}`;
  };
  const trackChoices = tracks.some((item) => item.id === trackId) || !trackId
    ? tracks.map((item) => ({ id: item.id, title: item.title }))
    : [...tracks.map((item) => ({ id: item.id, title: item.title })), { id: trackId, title: trackId }];
  const activity = <details ref={trace}><summary>Run history and diagnostics</summary>
    {reviews.filter((item) => item.conversation_id && item.conversation_id === conversationId && item.id !== reviewId).map((item) =>
      <p key={item.id}><Link to={`/deep-work?${new URLSearchParams({
        conversation: conversationId, review: item.id, ...(item.track_id ? { track: item.track_id } : {}),
        ...webQuery(storedWebPreference(item.id, conversationId) ?? { enabled: true, explicit: false }),
      })}`}>Earlier paper batch: {item.title}</Link></p>)}
    {runs.length ? <SessionOverview summary={summary} /> : null}
    {runs.map((run) => <section key={run.id}>
      <h4>{run.created_at} · {run.status}</h4>
      <details><summary>Original instructions</summary><pre>{typeof run.input === 'string' ? run.input : JSON.stringify(run.input, null, 2)}</pre></details>
      <ol>{run.events.map((event) => <li key={event.sequence}><strong>{event.event_type}</strong>
        <pre>{JSON.stringify(event.payload, null, 2)}</pre></li>)}</ol>
    </section>)}
  </details>;
  const progress = latest ? <RunActivity stages={buildRunStages(latest)} workPlan={summary.workPlan} onOpenActivity={openActivity} /> : null;
  return <div className="deep-work-page" aria-label="Research workspace">
    <PageHeader title="Research workspace" description="Ask a question, choose your sources, and build an evidence-backed answer."
      actions={<>
        <button ref={historyToggle} className="button secondary research-history-toggle" aria-expanded={historyOpen}
          aria-controls="research-history" onClick={() => setHistoryOpen((open) => !open)}><Icon name="sidebar" size={16} />Research history</button>
        {hasResearch ? <button className="button ghost" onClick={focusComposer}><Icon name="chat" size={15} />Write a message</button> : null}
        <Link className="button ghost" to="/chat">Back to chat</Link>
        {!hasResearch || review ? <IconButton icon="settings" size={20} className="settings-trigger" visibleLabel="Settings" label="Research settings" onClick={() => setSettingsOpen(true)} /> : null}
      </>} />
    <ErrorNotice error={error} />
    <ErrorNotice error={historyError} />
    <div className="research-layout">
      <aside id="research-history" className={`research-history${historyOpen ? ' is-open' : ''}`} aria-label="Research history"
        onKeyDown={(event) => {
          if (event.key === 'Escape' && historyOpen) {
            setHistoryOpen(false);
            historyToggle.current?.focus();
          }
        }}>
        <div className="research-history-heading"><h2>Saved research</h2><span className="field-hint">{history.size}</span></div>
        <button className="button secondary" disabled={blocked} onClick={() => navigate(historyUrl(''))}><Icon name="plus" size={15} />New research</button>
        <input type="search" aria-label="Search saved research" placeholder="Search research..." value={historyQuery}
          onChange={(event) => setHistoryQuery(event.target.value)} />
        <div className="research-history-filters" role="group" aria-label="Filter research by track">
          <button type="button" aria-pressed={!historyTrack} onClick={() => setHistoryTrack('')}>All research</button>
          {trackChoices.map((item) => <button type="button" key={item.id} aria-pressed={historyTrack === item.id}
            onClick={() => setHistoryTrack(item.id)}><Icon name="workspace" size={14} />{item.title}</button>)}
        </div>
        {historyTrack ? <p className="field-hint">Use New research to start a question in {trackChoices.find((item) => item.id === historyTrack)?.title}.</p> : null}
        {!loaded ? <Loading label="Loading saved research..." /> : null}
        <nav className="research-history-list" aria-label="Saved research">
          {visibleHistory.map(([key, item]) => <Link key={key} to={historyUrl(key)} data-research-key={key}
            className={`research-history-item${key === selectedHistory ? ' is-current' : ''}`}
            title={item.title} aria-disabled={blocked || undefined}
            aria-current={key === selectedHistory ? 'page' : undefined}
            onClick={(event) => { if (blocked || key === selectedHistory) event.preventDefault(); }}>
            <span className="research-history-title">{item.title}</span>
            <span className="research-history-meta">
              <span>{item.status === 'awaiting_approval' ? 'Needs approval' : item.status.replace(/_/g, ' ')}</span>
              <time dateTime={item.updatedAt}>{new Date(item.updatedAt).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}</time>
            </span>
          </Link>)}
        </nav>
        {loaded && !visibleHistory.length && !historyError ? <div className="research-history-empty">
          <p>{history.size ? 'No research matches these filters.' : 'Your research will appear here after you start a question.'}</p>
          {historyQuery || historyTrack ? <button className="button ghost small" onClick={() => { setHistoryQuery(''); setHistoryTrack(''); }}>Show all research</button> : null}
        </div> : null}
      </aside>
    <main className="research-flow">
      {!loaded || loadingConversation ? <Loading label="Loading research…" /> : null}
      {loaded ? <>
      {!hasResearch && trackId ? <p className="research-track-context">New research in <Link to={`/tracks?id=${encodeURIComponent(trackId)}`}>
        {trackChoices.find((item) => item.id === trackId)?.title}</Link></p> : null}
      {review ? <ResearchHeading review={review} /> : hasResearch ? <header className="research-head"><h2>{current?.title || 'Research in progress'}</h2>
        {latest ? <StatusPill value={latest.status} /> : null}</header> : null}
      {hasResearch && (running || paperRunning) ? <div className="research-session-controls" aria-label="Research controls">
        <span className="field-hint">Research is running. You can browse the evidence while it works.</span>
        <div className="button-row">
          {running || paperRunning ? <>
            <button className="button secondary" type="button" disabled={blocked} onClick={() => void pause()}>Pause research</button>
            <button className="button secondary" type="button" disabled={blocked || loadingConversation} onClick={() => void stopAndSummarize()}>Stop and summarize findings</button>
          </> : null}
        </div>
      </div> : null}
      <LiteratureReviews embedded selectedReview={review} showSetup={!hasResearch && !requestedReview && !requestedConversation}
        trackId={trackId} conversationId={conversationId || undefined} webEnabled={web}
        initialObjective={params.get('objective') ?? params.get('question') ?? ''}
        ensureConversation={ensureConversation} onReviewChange={updateReview}
        progress={progress} activity={activity} onDiscuss={discuss} onIntentChange={setIntent} onWebEnabledChange={changeWeb}
        settingsControl={{ open: settingsOpen, onOpenChange: setSettingsOpen }}
        disabled={busy} onBusyChange={setPaperBusy} />
      {hasResearch ? <>
        {!review ? progress : null}
        {runs.length ? <section className="research-conversation" aria-label="Research conversation">
          {runs.map((run, index) => <article className="research-response" key={run.id}>
            <details open={index === runs.length - 1}>
              <summary>{typeof run.input === 'string' ? run.input.split('\n\nCurrent research context:')[0] : `Research update ${index + 1}`}</summary>
              {findings(run) ? <MarkdownViewer content={findings(run)} /> :
                <p className="field-hint">{activeRun(run) ? 'Working on this direction. Findings will appear here.' : 'No written answer was produced for this direction.'}</p>}
              <ErrorNotice error={run.error} />
            </details>
          </article>)}
        </section> : null}
        <form className="research-composer research-question-box" aria-label="Research message" onSubmit={(event) => { event.preventDefault(); void send(); }}>
          <textarea ref={composer} aria-label="Research message" rows={3} required value={instruction} disabled={busy}
            onChange={(event) => setInstruction(event.target.value)}
            placeholder={running ? 'Guide the ongoing work, ask a question, or change direction...' : 'Ask about your papers, compare findings, or explore an idea...'} />
          <div className="research-composer-actions">
            <div className="segmented" role="group" aria-label="Message focus">
              <button type="button" aria-pressed={intent === 'chat'} disabled={!!running || blocked} onClick={() => setIntent('chat')}>Research</button>
              <button type="button" aria-pressed={intent === 'ideation'} disabled={!!running || blocked} onClick={() => setIntent('ideation')}>Ideas</button>
            </div>
            {!running ? <label className="check-row"><input type="checkbox" checked={web} disabled={blocked} onChange={(event) => changeWeb(event.target.checked)} /> Web</label> : null}
            <button className="button research-send" disabled={blocked || loadingConversation || !instruction.trim()}>{running ? 'Send direction' : 'Send'}<Icon name="arrowRight" size={16} /></button>
          </div>
          {receipt ? <p role="status">{receipt}</p> : null}
        </form>
        {!review ? activity : null}
        <Link className="research-permalink" to={`/deep-work?${new URLSearchParams({
          ...(conversationId ? { conversation: conversationId } : {}), ...(review ? { review: review.id } : {}), ...(trackId ? { track: trackId } : {}),
          ...webQuery(webPreference),
        })}`}>Link to this research</Link>
      </> : null}
      </> : null}
    </main>
    </div>
  </div>;
}
