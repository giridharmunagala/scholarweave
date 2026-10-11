import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useLocation, useNavigate, useNavigationGuard } from '../../app/router';
import { ErrorNotice, Loading, StatusPill } from '../../shared/components/Ui';
import { ModelSelect } from '../../shared/components/ModelSelect';
import { MarkdownViewer } from '../../shared/components/MarkdownViewer';
import { LiteratureReviews } from '../reviews/LiteratureReviews';
import { tracksApi, type Track } from '../tracks/api';
import { capabilityOptions } from '../providers/ModelDefaultsPanel';
import { providersApi, type Provider } from '../providers/api';
import { chatApi, deepWorkApi, type Conversation, type ConversationDetail, type ModelReference, type Run } from '../chat/api';
import { buildSessionObservability } from '../chat/sessionObservability';
import { buildRunStages } from '../chat/runStages';
import { RunActivity } from '../chat/RunActivity';
import { SessionOverview } from '../chat/SessionMonitor';
import '../chat/chat.css';
import './deep-work.css';

const activeRun = (run: Run) => !['completed', 'failed', 'cancelled'].includes(run.status);
const findings = (run: Run | undefined) => typeof run?.final_output === 'string' ? run.final_output : '';

export default function DeepWorkPage() {
  const { search } = useLocation();
  const navigate = useNavigate();
  const params = new URLSearchParams(search);
  const trackId = params.get('track') ?? '';
  const conversationId = params.get('conversation') ?? '';
  const tasks = params.get('view') === 'tasks';
  const [tracks, setTracks] = useState<Track[]>([]);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => { let active = true;
    tracksApi.list().then((items) => { if (active) setTracks(items); })
      .catch((reason) => { if (active) setError(reason); });
    return () => { active = false; };
  }, []);
  const destination = (view: string, track = trackId) => `/deep-work?${new URLSearchParams({
    view, ...(track ? { track } : {}), ...(conversationId ? { conversation: conversationId } : {}),
  })}`;
  return <div className="deep-work-page">
    <header className="deep-work-heading">
      <div><h1>Deep Work</h1><p>Set the objective, follow the evidence, and control work that takes time.</p></div>
      <Link className="button secondary" to="/">Back to chat</Link>
    </header>
    <ErrorNotice error={error} />
    <div className="deep-work-toolbar">
      <nav aria-label="Deep Work workflows">
        <Link to={destination('reviews')} aria-current={!tasks ? 'page' : undefined}>Literature reviews</Link>
        <Link to={destination('tasks')} aria-current={tasks ? 'page' : undefined}>Research tasks</Link>
      </nav>
      <label>Research track<select value={trackId} onChange={(event) => navigate(destination(tasks ? 'tasks' : 'reviews', event.target.value))}>
        <option value="">All work / no track for new work</option>
        {trackId && !tracks.some((track) => track.id === trackId) ? <option value={trackId}>{trackId}</option> : null}
        {tracks.map((track) => <option key={track.id} value={track.id}>{track.title}</option>)}
      </select></label>
      {trackId ? <Link to={`/tracks?id=${encodeURIComponent(trackId)}`}>Open track artifacts</Link> : null}
    </div>
    <p className="field-hint">Work continues on the local backend when you leave this page. Return here for progress and decisions; leaving does not pause it.</p>
    {tasks ? <ResearchTasks key={`${trackId}:${conversationId}`} trackId={trackId} initialId={conversationId} />
      : <LiteratureReviews key={`${trackId}:${conversationId}:${params.get('review') ?? ''}`} trackId={trackId} conversationId={conversationId || undefined} />}
  </div>;
}

function ResearchTasks({ trackId, initialId }: { trackId: string; initialId: string }) {
  const [items, setItems] = useState<Conversation[]>([]);
  const [selectedId, setSelectedId] = useState(initialId);
  const [current, setCurrent] = useState<ConversationDetail | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [model, setModel] = useState<ModelReference>({});
  const [objective, setObjective] = useState('');
  const [instruction, setInstruction] = useState('');
  const [web, setWeb] = useState(true);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [receipt, setReceipt] = useState('');
  const version = useRef(0);
  const mounted = useRef(true);
  const trace = useRef<HTMLDetailsElement>(null);
  useNavigationGuard(!!objective.trim() || !!instruction.trim() || busy, 'Leave Deep Work and discard unsent instructions? Running work will continue.', !busy);
  useEffect(() => {
    mounted.current = true;
    let inFlight = false;
    const load = async () => {
      if (inFlight) return;
      inFlight = true;
      const request = version.current;
      try {
        const list = await deepWorkApi.list();
        if (mounted.current && request === version.current) setItems(list);
      } catch (reason) { if (mounted.current) setError(reason); }
      finally { inFlight = false; }
    };
    void load();
    void Promise.all([providersApi.list(), providersApi.settings()]).then(([list, settings]) => {
      if (!mounted.current) return;
      setProviders(list);
      setModel(settings.default_model_references.chat ?? {});
    }).catch((reason) => { if (mounted.current) setError(reason); });
    const timer = window.setInterval(() => void load(), 3000);
    return () => { mounted.current = false; window.clearInterval(timer); };
  }, []);
  useEffect(() => {
    let active = true;
    let inFlight = false;
    setCurrent(null);
    setRuns([]);
    setLoading(!!selectedId);
    const load = async () => {
      if (!selectedId || inFlight) return;
      inFlight = true;
      const request = version.current;
      try {
        const [detail, history] = await Promise.all([chatApi.get(selectedId), chatApi.runs(selectedId)]);
        if (!active || request !== version.current) return;
        setCurrent(detail);
        setRuns(history);
        setLoading(false);
      } catch (reason) { if (active) { setError(reason); setLoading(false); } }
      finally { inFlight = false; }
    };
    void load();
    const timer = window.setInterval(() => void load(), 3000);
    return () => { active = false; window.clearInterval(timer); };
  }, [selectedId]);
  const latest = runs[runs.length - 1];
  const running = runs.find(activeRun);
  const summary = useMemo(() => buildSessionObservability(runs), [runs]);
  const perform = async (action: () => Promise<void>) => {
    if (busy) return;
    version.current += 1;
    setBusy(true); setError(null); setReceipt('');
    try { await action(); }
    catch (reason) { if (mounted.current) setError(reason); }
    finally { if (mounted.current) { version.current += 1; setBusy(false); } }
  };
  const updateRun = (run: Run) => {
    if (mounted.current) setRuns((history) => [...history.filter((item) => item.id !== run.id), run]);
  };
  const start = () => perform(async () => {
    const conversation = await deepWorkApi.create(model, trackId || undefined);
    if (!mounted.current) return;
    setItems((list) => [conversation, ...list.filter((item) => item.id !== conversation.id)]);
    setSelectedId(conversation.id);
    // Preserve the objective for retry if the run cannot start after creation.
    setInstruction(objective);
    setObjective('');
    let result;
    try {
      result = await deepWorkApi.send(conversation.id, objective, { response_effort: 'thorough', web_enabled: web });
    } catch (reason) {
      if (mounted.current) {
        setCurrent({ ...conversation, items: [] });
        setLoading(false);
      }
      throw reason;
    }
    if (!mounted.current) return;
    setCurrent({ ...result.conversation, items: [] });
    updateRun(result.run);
    setLoading(false);
    setInstruction('');
  });
  const submitInstruction = () => perform(async () => {
    if (!current) return;
    if (running) {
      const result = await chatApi.steer(running.id, instruction);
      if (mounted.current) setReceipt(`Instruction ${result.status}: ${result.content}`);
    } else {
      const api = current.kind === 'deep_work' ? deepWorkApi : chatApi;
      const result = await api.send(current.id, instruction, { response_effort: 'thorough', web_enabled: web });
      updateRun(result.run);
    }
    if (mounted.current) setInstruction('');
  });
  const visible = items.filter((item) => !trackId || item.track_id === trackId);
  const selectTask = (id: string) => {
    if ((instruction.trim() || objective.trim()) && !window.confirm('Discard unsent instructions and switch tasks?')) return;
    setSelectedId(id); setInstruction(''); setObjective(''); setReceipt('');
  };
  return <section className="deep-work-tasks" aria-label="Research task workspace">
    <aside>
      <h2>Research tasks</h2>
      <p>Open-ended investigation with a work plan and focused workers. For automatic paper reading with approved scope and work limits, use Literature reviews.</p>
      <button className="button secondary" disabled={busy} onClick={() => selectTask('')}>New task</button>
      {visible.map((item) => <button className="deep-work-task" key={item.id} disabled={busy}
        aria-pressed={selectedId === item.id} onClick={() => selectTask(item.id)}>
        <strong>{item.title}</strong><StatusPill value={item.status} /><small>{item.last_message_preview}</small>
      </button>)}
    </aside>
    <div className="deep-work-detail">
      <ErrorNotice error={error} />
      {loading ? <Loading label="Loading task activity..." /> : null}
      {!selectedId ? <form onSubmit={(event) => { event.preventDefault(); void start(); }}>
        <h2>Start a research task</h2>
        <label>Objective<textarea rows={5} required value={objective} onChange={(event) => setObjective(event.target.value)}
          placeholder="What should be investigated? Include the scope, constraints and desired outcome." /></label>
        <label>Research model<ModelSelect ariaLabel="Research model" options={capabilityOptions(providers, 'chat')} value={model} onChange={setModel} /></label>
        <label className="deep-work-toggle"><input type="checkbox" checked={web} onChange={(event) => setWeb(event.target.checked)} /> Allow web access</label>
        <button className="button primary" disabled={busy || !objective.trim() || !model.model || !model.provider_profile_id}>Start task</button>
      </form> : current ? <>
        <header className="deep-work-heading"><h2>{current.title}</h2><StatusPill value={running?.status ?? latest?.status ?? current.status} /></header>
        {current.track_id ? <Link to={`/tracks?id=${encodeURIComponent(current.track_id)}`}>Open this task's track</Link> : null}
        <p>Model: {current.model_reference.model ?? 'Configured default'}</p>
        <Link className="button secondary" to={`/deep-work?view=reviews&conversation=${encodeURIComponent(current.id)}`}>
          Automatic paper decisions and work limits for this task
        </Link>
        <div className="button-row">
          {running ? <>
            <button className="button secondary" disabled={busy} onClick={() => void perform(async () => {
              updateRun(await chatApi.cancelRun(running.id));
            })}>Stop task</button>
            <button className="button secondary" disabled={busy} onClick={() => void perform(async () => {
              const result = await chatApi.stopAndAnswer(running.id);
              updateRun(result.stopped_run); updateRun(result.answer_run);
            })}>Stop and summarize findings</button>
          </> : null}
        </div>
        {latest ? <RunActivity stages={buildRunStages(latest)} workPlan={summary.workPlan}
          onOpenActivity={() => { if (trace.current) { trace.current.open = true; trace.current.scrollIntoView?.({ block: 'nearest' }); } }} /> : null}
        <SessionOverview summary={summary} />
        {latest?.error ? <ErrorNotice error={latest.error} /> : null}
        {current ? <Link to={`/deep-work?view=tasks&conversation=${encodeURIComponent(current.id)}`}>Link to this task</Link> : null}
        <section aria-label="Findings"><h3>Findings</h3>
          {findings(latest) ? <MarkdownViewer content={findings(latest)} /> : <p>No final findings yet. Activity and the work plan show progress.</p>}
        </section>
        <form onSubmit={(event) => { event.preventDefault(); void submitInstruction(); }}>
          <label>{running ? 'Steer ongoing work' : 'Continue or answer a question'}<textarea rows={3} required
            value={instruction} onChange={(event) => setInstruction(event.target.value)} /></label>
          {!running ? <label className="deep-work-toggle"><input type="checkbox" checked={web} onChange={(event) => setWeb(event.target.checked)} /> Allow web access for the next run</label> : null}
          <button className="button primary" disabled={busy || !instruction.trim()}>{running ? 'Send instruction' : 'Continue task'}</button>
          {receipt ? <p role="status">{receipt}</p> : null}
        </form>
        <details ref={trace}><summary>Run history and event log</summary>
          {runs.map((run) => <section key={run.id}><h3>{run.created_at} - {run.status}</h3>
            <details><summary>Original instructions</summary><pre>{typeof run.input === 'string' ? run.input : JSON.stringify(run.input, null, 2)}</pre></details>
            {findings(run) ? <MarkdownViewer content={findings(run)} /> : null}
            <ol>{run.events.map((event) => <li key={event.sequence}><strong>{event.event_type}</strong>
              <pre>{JSON.stringify(event.payload, null, 2)}</pre></li>)}</ol>
          </section>)}
        </details>
      </> : null}
    </div>
  </section>;
}
