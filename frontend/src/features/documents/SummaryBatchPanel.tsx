import { useEffect, useState } from 'react';
import type { components } from '../../api/schema.generated';
import { request } from '../../api/client';
import { subscribeToRun } from '../../api/events';
import { ModelSelect, type ModelSelectValue } from '../../shared/components/ModelSelect';
import { ErrorNotice, Loading, Panel, StatusPill } from '../../shared/components/Ui';
import { capabilityOptions } from '../providers/ModelDefaultsPanel';
import { providersApi, type Provider } from '../providers/api';
import { reasoningEffortsForModel, ReasoningEffortSelect, type ReasoningEffort } from '../chat/ReasoningEffortSelect';
import { summaryApi, type SummaryBatch, type SummaryRequest } from './summaryApi';

type Paper = components['schemas']['DocumentSummaryResponse'];
const TERMINAL = new Set(['completed', 'failed', 'cancelled']);
const EVENT_STATUS: Record<string, SummaryBatch['runs'][number]['run']['status']> = {
  'run.started': 'running',
  'run.completed': 'completed',
  'run.failed': 'failed',
  'run.cancelled': 'cancelled',
};

export function SummaryBatchPanel({ papers }: { papers: Paper[] }) {
  const [providers, setProviders] = useState<Provider[]>([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<string[]>([]);
  const [model, setModel] = useState<ModelSelectValue>({});
  const [mode, setMode] = useState<NonNullable<SummaryRequest['mode']>>('reviewed');
  const [reasoningEffort, setReasoningEffort] = useState<ReasoningEffort | null>(null);
  const [batch, setBatch] = useState<SummaryBatch | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const ready = papers.filter((paper) => paper.status === 'ready');

  useEffect(() => {
    let active = true;
    providersApi.list()
      .then((items) => { if (active) setProviders(items); })
      .catch((nextError) => { if (active) setError(nextError); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, []);

  const runIds = batch?.runs.map(({ run }) => run.id).join(',') ?? '';
  useEffect(() => {
    if (!batch) return;
    const subscriptions = new Map<string, () => void>();
    for (const { run } of batch.runs) {
      if (TERMINAL.has(run.status)) continue;
      subscriptions.set(run.id, subscribeToRun(run.id, -1, (event) => {
        const state = EVENT_STATUS[event.event_type];
        if (!state) return;
        if (TERMINAL.has(state)) subscriptions.get(run.id)?.();
        setBatch((current) => current ? {
          ...current,
          runs: current.runs.map((item) => item.run.id === run.id
            ? { ...item, run: { ...item.run, status: state } } : item),
        } : current);
      }, () => setError(new Error('Summary progress disconnected. Reconnecting automatically.'))));
    }
    return () => subscriptions.forEach((unsubscribe) => unsubscribe());
  }, [runIds]);

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      setBatch(await summaryApi.startBatch(selected, {
        model_reference: model, mode,
        ...(reasoningEffort ? { reasoning_effort: reasoningEffort } : {}),
      }));
      setSelected([]);
    } catch (nextError) {
      setError(nextError);
    } finally {
      setBusy(false);
    }
  };

  const cancel = async (runId: string) => {
    try {
      await request(`/runs/${encodeURIComponent(runId)}/cancel`, { method: 'POST' });
    } catch (nextError) {
      setError(nextError);
    }
  };

  const activeBatch = batch?.runs.some(({ run }) => !TERMINAL.has(run.status)) ?? false;
  return (
    <Panel title="Summary batch" description="Queue papers sequentially on one explicitly selected model.">
      {error ? <ErrorNotice error={error} /> : null}
      <p className="muted">
        Only one LLM call runs at a time across the app. Chat can take a turn between summary
        calls; the server handles model switching automatically.
      </p>
      {loading ? <Loading label="Loading summary models..." /> : (
        <ModelSelect
          options={capabilityOptions(providers, 'chat')}
          value={model}
          onChange={(value) => { setModel(value); setReasoningEffort(null); }}
          ariaLabel="Summary batch model"
          disabled={busy || activeBatch}
        />
      )}
      <label>
        Summary depth
        <select
          aria-label="Summary batch depth"
          value={mode}
          disabled={busy || activeBatch}
          onChange={(event) => setMode(event.target.value === 'overview' ? 'overview' : 'reviewed')}
        >
          <option value="reviewed">Reviewed - coverage-aware evidence and synthesis</option>
          <option value="overview">Quick overview - explicitly partial</option>
        </select>
      </label>
      <ReasoningEffortSelect
        value={reasoningEffort}
        supportedEfforts={reasoningEffortsForModel(providers, model)}
        onChange={setReasoningEffort}
        disabled={busy || activeBatch}
        defaultLabel="Off by default (when supported)"
        ariaLabel="Summary batch reasoning"
      />
      <div className="stack-tight" role="group" aria-label="Papers for summary batch">
        {ready.map((paper) => (
          <label key={paper.id}>
            <input
              type="checkbox"
              checked={selected.includes(paper.id)}
              disabled={busy || activeBatch || (!selected.includes(paper.id) && selected.length >= 50)}
              onChange={(event) => setSelected((items) => event.target.checked
                ? [...items, paper.id] : items.filter((id) => id !== paper.id))}
            />
            {paper.title}
          </label>
        ))}
        {!ready.length ? <p className="muted">Prepare papers before adding them to a summary batch.</p> : null}
      </div>
      <button
        className="button"
        type="button"
        disabled={busy || activeBatch || !selected.length || !model.model || !model.provider_profile_id}
        onClick={() => void start()}
      >
        Queue {selected.length} summaries
      </button>
      {batch ? (
        <div aria-live="polite" className="stack-tight">
          {batch.runs.map(({ run, document_id }) => (
            <div key={run.id}>
              <span>{papers.find((paper) => paper.id === document_id)?.title ?? run.agent_name} </span>
              <StatusPill value={run.status} />
              {!TERMINAL.has(run.status) ? (
                <button className="button secondary small" type="button" onClick={() => void cancel(run.id)}>Cancel</button>
              ) : null}
            </div>
          ))}
          <p className="muted">Open each paper to inspect its saved summary and evidence coverage.</p>
        </div>
      ) : null}
    </Panel>
  );
}
