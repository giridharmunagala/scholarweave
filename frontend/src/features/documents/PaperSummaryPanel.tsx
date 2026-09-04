import { useEffect, useState } from 'react';
import { subscribeToRun } from '../../api/events';
import { Icon } from '../../shared/components/Icons';
import { MarkdownViewer } from '../../shared/components/MarkdownViewer';
import { ErrorNotice, Loading, Panel, StatusPill } from '../../shared/components/Ui';
import { summaryApi, type SummaryContent, type SummaryRun, type SummaryVersion } from './summaryApi';

const TERMINAL_EVENTS = new Set(['run.completed', 'run.failed', 'run.cancelled', 'run.paused']);

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

  const load = async () => setVersions(await summaryApi.versions(documentId));

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
        load()
          .then(() => setActive(null))
          .catch(setError);
      },
      () => undefined,
    );
  }, [active?.run.id, documentId]);

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      setActive(await summaryApi.start(documentId));
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
      description="Each reviewed run updates the paper summary and keeps a version you can restore later."
      actions={(
        <button className="button small" type="button" disabled={!ready || busy || Boolean(active)} onClick={() => void start()}>
          <Icon name="sparkle" size={13} />
          {active ? 'Summarizing…' : versions.length ? 'Rerun summary' : 'Create summary'}
        </button>
      )}
    >
      {error ? <ErrorNotice error={error} /> : null}
      {!ready ? <p className="muted">Ingest and index this paper before creating a summary.</p> : null}
      {active ? (
        <div className="summary-running" role="status">
          <span className="spinner tiny" aria-hidden="true" />
          <span>
            Extracting evidence and reviewing the draft
            <small>Prompt revision {active.prompt_revision.slice(0, 10)}</small>
          </span>
        </div>
      ) : null}
      {loading ? <Loading label="Loading summary versions…" /> : (
        <div className="summary-version-list">
          {versions.map((version) => (
            <div className="summary-version" key={version.id}>
              <button type="button" onClick={() => void open(version)}>
                <strong>{new Date(version.created_at).toLocaleString()}</strong>
                <small>{version.citation_count} citations · prompt {version.prompt_revision?.slice(0, 10) ?? 'unknown'}</small>
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
          <p className="muted">{opened.version.review_summary}</p>
          <MarkdownViewer content={opened.content} />
        </details>
      ) : null}
    </Panel>
  );
}
