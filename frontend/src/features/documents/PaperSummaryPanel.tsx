import { useEffect, useState } from 'react';
import { subscribeToRun } from '../../api/events';
import { Link } from '../../app/router';
import { Icon } from '../../shared/components/Icons';
import { MarkdownViewer } from '../../shared/components/MarkdownViewer';
import { ErrorNotice, Loading, Panel, StatusPill } from '../../shared/components/Ui';
import { resolveModelReference } from '../chat/ChatModelPicker';
import { reasoningEffortsForModel, ReasoningEffortSelect, type ReasoningEffort } from '../chat/ReasoningEffortSelect';
import { providersApi } from '../providers/api';
import { summaryApi, type SummaryContent, type SummaryRequest, type SummaryRun, type SummaryVersion } from './summaryApi';

const TERMINAL_EVENTS = new Set(['run.completed', 'run.failed', 'run.cancelled']);

export function PaperSummaryPanel({
  documentId,
  ready,
}: {
  documentId: string;
  ready: boolean;
}) {
  const [versions, setVersions] = useState<SummaryVersion[]>([]);
  const [active, setActive] = useState<SummaryRun | null>(null);
  const [opened, setOpened] = useState<SummaryContent | null>(null);
  const [promotedId, setPromotedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [mode, setMode] = useState<NonNullable<SummaryRequest['mode']>>('reviewed');
  const [reasoningEffort, setReasoningEffort] = useState<ReasoningEffort | null>(null);
  const [supportedEfforts, setSupportedEfforts] = useState<ReasoningEffort[] | null>(null);

  const load = async () => setVersions(await summaryApi.versions(documentId));

  useEffect(() => {
    let active = true;
    Promise.all([providersApi.list(), providersApi.settings()])
      .then(([providers, settings]) => {
        if (active) setSupportedEfforts(reasoningEffortsForModel(providers, resolveModelReference({}, settings)));
      })
      .catch((nextError) => { if (active) setError(nextError); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    setLoading(true);
    setOpened(null);
    setActive(null);
    load().catch(setError).finally(() => setLoading(false));
  }, [documentId]);

  useEffect(() => {
    if (!active) return;
    return subscribeToRun(
      active.run.id,
      -1,
      (event) => {
        if (!TERMINAL_EVENTS.has(event.event_type)) return;
        if (event.event_type === 'run.failed') {
          const failure = event.payload.error;
          const message = typeof failure === 'string' ? failure
            : failure && typeof failure === 'object' && 'message' in failure && typeof failure.message === 'string'
              ? failure.message : 'Paper summary generation failed.';
          setError(new Error(message));
        }
        load()
          .then(() => setActive(null))
          .catch(setError);
      },
      () => setError(new Error('The summary progress connection was interrupted. Reopen this paper to refresh its saved versions.')),
    );
  }, [active?.run.id, documentId]);

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      setActive(await summaryApi.start(documentId, {
        mode, ...(reasoningEffort ? { reasoning_effort: reasoningEffort } : {}),
      }));
    } catch (nextError) {
      setError(nextError);
    } finally {
      setBusy(false);
    }
  };

  const open = async (version: SummaryVersion) => {
    setBusy(true);
    setError(null);
    try {
      setOpened(await summaryApi.get(documentId, version.id));
    } catch (nextError) {
      setError(nextError);
    } finally {
      setBusy(false);
    }
  };

  const promote = async (version: SummaryVersion) => {
    setBusy(true);
    setError(null);
    try {
      await summaryApi.promote(documentId, version.id);
      setPromotedId(version.id);
    } catch (nextError) {
      setError(nextError);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel
      title="Paper summary"
      description="Reviewed summaries retain evidence coverage. Quick overviews are explicitly partial."
      actions={(
        <button className="button small" type="button" disabled={!ready || busy || Boolean(active)} onClick={() => void start()}>
          <Icon name="sparkle" size={13} />
          {active ? 'Summarizing…' : versions.length ? 'Rerun summary' : 'Create summary'}
        </button>
      )}
    >
      {error ? <ErrorNotice error={error} /> : null}
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
      {active ? (
        <div className="summary-running" role="status">
          <span className="spinner tiny" aria-hidden="true" />
          <span>
            Extracting evidence and reviewing the draft
          </span>
        </div>
      ) : null}
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
              <button className="button secondary small" type="button" disabled={busy} onClick={() => void promote(version)}>
                Use this version
              </button>
            </div>
          ))}
          {!versions.length && !active ? <p className="muted">No summary versions yet.</p> : null}
        </div>
      )}
      {opened ? (
        <details className="summary-preview" open>
          <summary>Summary from {new Date(opened.version.created_at).toLocaleString()}</summary>
          <div className="library-handoff">
            <Link
              className="button secondary small"
              title="Open an editable draft in a new research chat"
              to={`/?research=${encodeURIComponent(`Analyze the existing saved summary at workspace path "${opened.version.path}" (paper ID: ${documentId}, summary version: ${opened.version.id}). Explain its findings, assess its evidence and limitations, and answer here in chat. Do not generate another saved summary or overwrite existing work.`)}`}
            >
              Analyze summary
            </Link>
            <small className="muted">Opens a draft you can edit before sending.</small>
          </div>
          <p className="muted">{opened.version.review_summary}</p>
          <details className="library-details">
            <summary>Summary provenance</summary>
            <p className="muted">Prompt revision: {opened.version.prompt_revision ?? 'unknown'}</p>
            {opened.version.source_version ? (
              <p className="muted">Source revision: {opened.version.source_version.slice(0, 12)}</p>
            ) : null}
            {opened.version.model ? (
              <p className="muted">
                Model: {(typeof opened.version.model === 'string' ? opened.version.model : opened.version.model.model).split('/').pop()}
              </p>
            ) : null}
          </details>
          {opened.version.canonical_updated === false ? (
            <p className="muted">Saved as a separate version; the canonical paper summary was not replaced.</p>
          ) : null}
          <MarkdownViewer content={opened.content} />
        </details>
      ) : null}
    </Panel>
  );
}
