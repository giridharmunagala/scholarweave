import { useCallback, useEffect, useRef, useState } from 'react';
import { subscribeToRun } from '../../api/events';
import { Link } from '../../app/router';
import { Icon } from '../../shared/components/Icons';
import { MarkdownViewer } from '../../shared/components/MarkdownViewer';
import { ErrorNotice, Loading, Panel, StatusPill } from '../../shared/components/Ui';
import { modelReferenceLabel } from '../chat/ChatModelPicker';
import { reasoningEffortsForModel, ReasoningEffortSelect, type ReasoningEffort } from '../chat/ReasoningEffortSelect';
import { defaultSummaryModel, modelSupportsVision, providersApi } from '../providers/api';
import { summaryApi, type SummaryContent, type SummaryRequest, type SummaryRun, type SummaryVersion } from './summaryApi';

const TERMINAL_EVENTS = new Set(['run.completed', 'run.failed', 'run.cancelled']);

export function PaperSummaryPanel({
  documentId,
  ready,
  onOpen,
}: {
  documentId: string;
  ready: boolean;
  /** When set, the host shows opened summaries in its own reading area instead of inline. */
  onOpen?: (summary: SummaryContent) => void;
}) {
  const [versions, setVersions] = useState<SummaryVersion[]>([]);
  const [activeRuns, setActiveRuns] = useState<SummaryRun[]>([]);
  const active = activeRuns[0] ?? null;
  const [opened, setOpened] = useState<SummaryContent | null>(null);
  const [promotedId, setPromotedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [restored, setRestored] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [mode, setMode] = useState<NonNullable<SummaryRequest['mode']>>('reviewed');
  const [reasoningEffort, setReasoningEffort] = useState<ReasoningEffort | null>(null);
  const [supportedEfforts, setSupportedEfforts] = useState<ReasoningEffort[] | null>(null);
  const [modelLabel, setModelLabel] = useState('');
  const [vision, setVision] = useState<boolean | null>(null);
  const [replacement, setReplacement] = useState<{ version: SummaryVersion; sha256: string; content: string } | null>(null);
  const currentDocument = useRef(documentId);
  currentDocument.current = documentId;
  const starting = useRef(false);

  const load = useCallback(async () => {
    const [versions, runs] = await Promise.all([summaryApi.versions(documentId), summaryApi.activeRuns(documentId)]);
    if (currentDocument.current !== documentId) return;
    setVersions(versions);
    setActiveRuns(runs);
    setRestored(true);
  }, [documentId]);

  useEffect(() => {
    let active = true;
    Promise.all([providersApi.list(), providersApi.settings()])
      .then(([providers, settings]) => {
        if (!active) return;
        const reference = defaultSummaryModel(settings);
        setSupportedEfforts(reasoningEffortsForModel(providers, reference));
        setModelLabel(modelReferenceLabel(reference, providers));
        setVision(modelSupportsVision(providers, reference));
      })
      .catch((nextError) => { if (active) setError(nextError); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    let live = true;
    setLoading(true);
    setRestored(false);
    setError(null);
    setOpened(null);
    setPromotedId(null);
    setReplacement(null);
    setActiveRuns([]);
    load().catch((reason) => { if (live) setError(reason); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [documentId]);

  const runIds = activeRuns.map(({ run }) => run.id).join(',');
  useEffect(() => {
    const subscriptions = activeRuns.map(({ run }) => subscribeToRun(run.id, -1, (event) => {
        if (event.event_type === 'run.started') {
          setActiveRuns((current) => current.map((item) => item.run.id === run.id
            ? { ...item, run: { ...item.run, status: 'running' } } : item));
          return;
        }
        if (!TERMINAL_EVENTS.has(event.event_type)) return;
        if (event.event_type === 'run.failed') {
          const failure = event.payload.error;
          const message = typeof failure === 'string' ? failure
            : failure && typeof failure === 'object' && 'message' in failure && typeof failure.message === 'string'
              ? failure.message : 'Paper summary generation failed.';
          setError(new Error(message));
        }
        setActiveRuns((current) => current.filter((item) => item.run.id !== run.id));
        load().catch(setError);
      },
      () => setError(new Error('The summary progress connection was interrupted. Refresh progress to reconnect.')),
    ));
    return () => subscriptions.forEach((unsubscribe) => unsubscribe());
  }, [runIds, documentId]);

  const start = async () => {
    if (starting.current || busy || loading || !restored || active) return;
    starting.current = true;
    setBusy(true);
    setError(null);
    try {
      const run = await summaryApi.start(documentId, {
        mode, ...(reasoningEffort ? { reasoning_effort: reasoningEffort } : {}),
      });
      if (currentDocument.current === documentId) setActiveRuns([run]);
    } catch (nextError) {
      setError(nextError);
      await load().catch(() => undefined);
    } finally {
      starting.current = false;
      setBusy(false);
    }
  };

  const open = async (version: SummaryVersion) => {
    setBusy(true);
    setError(null);
    try {
      const content = await summaryApi.get(documentId, version.id);
      if (onOpen) onOpen(content);
      else setOpened(content);
    } catch (nextError) {
      setError(nextError);
    } finally {
      setBusy(false);
    }
  };

  const prepareReplacement = async (version: SummaryVersion) => {
    setBusy(true);
    setError(null);
    try {
      if (!version.canonical_path) throw new Error('This version has no canonical summary path. Reload its saved versions.');
      const current = await summaryApi.canonical(version.canonical_path);
      if (!current.sha256 || typeof current.content !== 'string') throw new Error('The canonical summary cannot be safely replaced. Reopen it in Notes.');
      setReplacement({ version, sha256: current.sha256, content: current.content });
    } catch (nextError) {
      setError(nextError);
    } finally {
      setBusy(false);
    }
  };

  const promote = async () => {
    if (!replacement || busy) return;
    setBusy(true);
    setError(null);
    try {
      await summaryApi.promote(documentId, replacement.version.id, replacement.sha256);
      setPromotedId(replacement.version.id);
      setReplacement(null);
      await load();
    } catch (nextError) {
      setError(nextError);
      setReplacement(null);
    } finally {
      setBusy(false);
    }
  };

  const cancel = async (runId: string) => {
    setBusy(true);
    setError(null);
    try { await summaryApi.cancel(runId); await load(); }
    catch (nextError) { setError(nextError); }
    finally { setBusy(false); }
  };

  return (
    <Panel
      title="Paper summary"
      description="Reviewed summaries retain evidence coverage. Quick overviews are explicitly partial."
      actions={(
        <button className="button small" type="button" disabled={!ready || loading || !restored || busy || Boolean(active)} onClick={() => void start()}>
          <Icon name="sparkle" size={13} />
          {active ? 'Summarizing…' : versions.length ? 'Rerun summary' : 'Create summary'}
        </button>
      )}
    >
      <p className="field-hint">
        Summary model: {modelLabel || 'Not configured'}. <Link to="/settings">Model settings</Link>.
      </p>
      {vision !== null ? <p className="field-hint" role="status">
        {vision
          ? 'Vision available: PDF page images are included alongside text as context allows. Any visual coverage limits are recorded.'
          : 'Text-only summary: this model does not advertise vision. Figures and plots are not viewed.'}
      </p> : null}
      {error ? <ErrorNotice error={error} /> : null}
      <button className="button secondary small" type="button" disabled={busy || loading} onClick={() => void load().catch(setError)}>Refresh summary progress</button>
      <details className="library-details">
        <summary>Summary options · {mode === 'reviewed' ? 'Reviewed' : 'Quick overview'}</summary>
        <label>
          Summary depth
          <select
            aria-label="Paper summary depth"
            value={mode}
            disabled={busy || Boolean(active)}
            onChange={(event) => setMode(event.target.value === 'overview' ? 'overview' : 'reviewed')}
          >
            <option value="reviewed">Reviewed summary</option>
            <option value="overview">Quick overview (partial)</option>
          </select>
        </label>
        <ReasoningEffortSelect
          value={reasoningEffort}
          supportedEfforts={supportedEfforts}
          onChange={setReasoningEffort}
          disabled={busy || Boolean(active)}
          defaultLabel="Off by default (when supported)"
          ariaLabel="Paper summary reasoning"
        />
        {active ? <p className="muted">Prompt revision: {active.prompt_revision.slice(0, 10)}</p> : null}
      </details>
      {!ready ? <p className="muted">Ingest and index this paper before creating a summary.</p> : null}
      {activeRuns.map(({ run }) => (
        <div className="summary-running" role="status" key={run.id}>
          <span className="spinner tiny" aria-hidden="true" />
          <span>
            {run.status === 'pending' ? 'Summary queued' : 'Extracting evidence and reviewing the draft'}
          </span>
          <button className="button secondary small" type="button" disabled={busy} onClick={() => void cancel(run.id)}>Cancel summary</button>
        </div>
      ))}
      {replacement ? <section className="notice info" role="alertdialog" aria-label="Replace canonical summary" aria-describedby="summary-replacement-warning">
        <p id="summary-replacement-warning">Replace the canonical summary with this version? The current content, including human edits, will be preserved as a separate saved version. Changes made after this confirmation was opened will prevent replacement.</p>
        <details><summary>Current canonical summary</summary><MarkdownViewer content={replacement.content} /></details>
        <div className="button-row">
          <button className="button" type="button" disabled={busy} onClick={() => void promote()}>Replace summary and preserve current</button>
          <button className="button secondary" type="button" disabled={busy} onClick={() => setReplacement(null)}>Keep current summary</button>
        </div>
      </section> : null}
      {loading ? <Loading label="Loading summary versions…" /> : (
        <div className="summary-version-list">
          {versions.map((version) => (
            <div className="summary-version" key={version.id}>
              <button type="button" onClick={() => void open(version)}>
                <strong>{new Date(version.created_at).toLocaleString()}</strong>
                <small>{version.citation_count} citations</small>
                {version.coverage_complete != null ? (
                  <small>
                    Source coverage: {version.coverage_complete ? 'complete' : 'partial'}
                    {version.review_complete === false ? ' · not fully reviewed' : ''}
                  </small>
                ) : null}
              </button>
              <StatusPill value={promotedId === version.id ? 'promoted' : version.status} />
              <button className="button secondary small" type="button" disabled={busy || Boolean(replacement)} onClick={() => void prepareReplacement(version)}>
                Use this version
              </button>
            </div>
          ))}
          {!versions.length && !active ? <p className="muted">No summary versions yet.</p> : null}
        </div>
      )}
      {opened ? <SummaryView documentId={documentId} summary={opened} onClose={() => setOpened(null)} /> : null}
    </Panel>
  );
}

export function SummaryView({ documentId, summary, onClose, closeLabel = 'Close summary' }: {
  documentId: string;
  summary: SummaryContent;
  onClose: () => void;
  closeLabel?: string;
}) {
  const { version, content } = summary;
  return (
    <section className="summary-preview" aria-label="Saved summary">
      <header className="summary-preview-head">
        <button className="button small secondary" type="button" onClick={onClose}>
          <Icon name="arrowRight" size={13} className="flip" />{closeLabel}
        </button>
        <strong>Summary from {new Date(version.created_at).toLocaleString()}</strong>
        <span className="row-spacer" />
        <Link
          className="button secondary small"
          title="Open an editable draft in a new research chat"
          to={`/chat?research=${encodeURIComponent(`Analyze the existing saved summary at workspace path "${version.path}" (paper ID: ${documentId}, summary version: ${version.id}). Explain its findings, assess its evidence and limitations, and answer here in chat. Do not generate another saved summary or overwrite existing work.`)}`}
        >
          Analyze summary
        </Link>
      </header>
      {version.review_summary ? <p className="muted">{version.review_summary}</p> : null}
      <details className="library-details">
        <summary>Summary provenance</summary>
        <p className="muted">Prompt revision: {version.prompt_revision ?? 'unknown'}</p>
        {version.source_version ? <p className="muted">Source revision: {version.source_version.slice(0, 12)}</p> : null}
        {version.visuals ? <p className="muted">
          Visual coverage: {version.visuals.pages.length} of {version.visuals.total_pages} PDF pages
          {version.visuals.complete ? ' (complete)' : ' (limited by model context)'}.
        </p> : null}
        {version.model ? (
          <p className="muted">Model: {(typeof version.model === 'string' ? version.model : version.model.model).split('/').pop()}</p>
        ) : null}
      </details>
      {version.canonical_updated === false ? (
        <p className="muted">Saved as a separate version; the canonical paper summary was not replaced.</p>
      ) : null}
      <MarkdownViewer content={content} />
    </section>
  );
}