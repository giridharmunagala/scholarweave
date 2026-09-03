import { useCallback, useEffect, useRef, useState } from 'react';
import { apiUrl, json, request } from '../../api/client';
import type { components } from '../../api/schema.generated';
import { Icon } from '../../shared/components/Icons';
import { MarkdownViewer } from '../../shared/components/MarkdownViewer';
import { EmptyState, ErrorNotice, LibraryTabs, Loading, PageHeader, Panel, StatusPill } from '../../shared/components/Ui';
import '../library.css';

type Document = components['schemas']['DocumentResponse'];
type DocumentSummary = components['schemas']['DocumentSummaryResponse'];
type Artifact = Document['artifacts'][number];
type DocumentChunk = Document['chunks'][number];
type ArtifactContent = components['schemas']['ArtifactContentResponse'];
type IngestionOptions = components['schemas']['IngestionOptionsResponse'];
type IngestionMode = IngestionOptions['recommended_mode'];
type IngestionProgress = {
  completed: number;
  total: number;
  percent: number;
};
function displayKind(kind: string): string {
  return kind.split('_').join(' ');
}

function chunkLabel(chunk: DocumentChunk): string {
  if (chunk.section_title?.trim()) {
    return chunk.citation.trim()
      ? `${chunk.citation} · ${chunk.section_title}`
      : chunk.section_title;
  }
  if (chunk.citation.trim()) return chunk.citation;
  return chunk.page_start === chunk.page_end
    ? `Page ${chunk.page_start}`
    : `Pages ${chunk.page_start}–${chunk.page_end}`;
}

export function ExtractionChunks({ chunks }: { chunks: DocumentChunk[] }) {
  if (!chunks.length) return null;
  return (
    <section className="extraction-chunks">
      <span className="eyebrow">Extracted text</span>
      <div className="chunk-list">
        {chunks.map((chunk) => (
          <details className="disclosure" key={chunk.id} open>
            <summary>{chunkLabel(chunk)}</summary>
            <p>{chunk.text}</p>
          </details>
        ))}
      </div>
    </section>
  );
}

function ingestionStatus(document: Document): string {
  const value = document.metadata.ingestion;
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return 'Extracting text, figures, and searchable chunks. Large papers can take several minutes.';
  }
  const progress = value as Record<string, unknown>;
  const label = typeof progress.phase_label === 'string' ? progress.phase_label : 'Ingesting and indexing';
  const completed = typeof progress.completed_pages === 'number' ? progress.completed_pages : null;
  const total = typeof progress.total_pages === 'number' ? progress.total_pages : null;
  return completed !== null && total !== null ? `${label}: ${completed} of ${total} pages.` : `${label}.`;
}

function ingestionProgress(document: Document): IngestionProgress | null {
  const value = document.metadata.ingestion;
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const progress = value as Record<string, unknown>;
  const completed = progress.completed_pages;
  const total = progress.total_pages;
  if (
    typeof completed !== 'number' ||
    typeof total !== 'number' ||
    !Number.isFinite(completed) ||
    !Number.isFinite(total) ||
    total <= 0
  ) {
    return null;
  }
  const boundedCompleted = Math.min(Math.max(completed, 0), total);
  return {
    completed: boundedCompleted,
    total,
    percent: Math.round((boundedCompleted / total) * 100),
  };
}

function ingestionDetail(document: Document): string {
  const value = document.metadata.ingestion;
  const mode =
    value && typeof value === 'object' && !Array.isArray(value)
      ? (value as Record<string, unknown>).mode
      : null;
  return mode === 'ocr'
    ? 'Previous generated files are cleared before OCR completes.'
    : 'Previous results are hidden until this run completes.';
}

export default function PapersPage() {
  const [documents, setDocuments] = useState<DocumentSummary[]>([]);
  const [selected, setSelected] = useState<Document | null>(null);
  const [artifactPreview, setArtifactPreview] = useState<ArtifactContent | null>(null);
  const [ingestionOptions, setIngestionOptions] = useState<IngestionOptions | null>(null);
  const [previewingArtifactId, setPreviewingArtifactId] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [remotePdfUrl, setRemotePdfUrl] = useState('');
  const [remotePdfTitle, setRemotePdfTitle] = useState('');
  const [showImport, setShowImport] = useState(false);
  const ingestionRequest = useRef<AbortController | null>(null);
  const previewRequestId = useRef(0);

  const clearArtifactPreview = useCallback(() => {
    previewRequestId.current += 1;
    setArtifactPreview(null);
  }, []);

  const load = async (selectedId?: string) => {
    const items = await request<DocumentSummary[]>('/documents');
    const targetId = selectedId ?? selected?.id ?? items[0]?.id;
    const detail = targetId
      ? await request<Document>(`/documents/${encodeURIComponent(targetId)}`)
      : null;
    setDocuments(items);
    setSelected(detail);
  };

  useEffect(() => {
    load().catch(setError).finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (!selected || selected.status === 'processing') return;
    let active = true;
    setIngestionOptions(null);
    request<IngestionOptions>(`/documents/${selected.id}/ingestion-options`, { cache: 'no-store' })
      .then((options) => {
        if (active) setIngestionOptions(options);
      })
      .catch((nextError) => {
        if (active) setError(nextError);
      });
    return () => {
      active = false;
    };
  }, [selected?.id]);

  const hasProcessingDocuments = documents.some((document) => document.status === 'processing');
  useEffect(() => {
    if (!hasProcessingDocuments) return;
    const sources = documents
      .filter((document) => document.status === 'processing')
      .map((document) => {
        const source = new EventSource(
          apiUrl(`/documents/${encodeURIComponent(document.id)}/ingest/events`),
        );
        source.addEventListener('document.updated', (message) => {
          const updated = JSON.parse((message as MessageEvent<string>).data) as Document;
          setDocuments((items) =>
            items.map((item) => (item.id === updated.id ? updated : item)),
          );
          setSelected((current) => (current?.id === updated.id ? updated : current));
          if (updated.status !== 'processing') source.close();
        });
        return source;
      });
    return () => sources.forEach((source) => source.close());
  }, [
    hasProcessingDocuments,
    documents
      .filter((document) => document.status === 'processing')
      .map((document) => document.id)
      .join(','),
  ]);

  const previewArtifact = useCallback(async (artifact: Artifact) => {
    const requestId = previewRequestId.current + 1;
    previewRequestId.current = requestId;
    setPreviewingArtifactId(artifact.id);
    setError(null);
    try {
      const nextPreview = await request<ArtifactContent>(`/artifacts/${artifact.id}/content`, {
        cache: 'no-store',
      });
      if (previewRequestId.current === requestId) {
        setArtifactPreview(nextPreview);
      }
    } catch (nextError) {
      if (previewRequestId.current === requestId) {
        setError(nextError);
      }
    } finally {
      if (previewRequestId.current === requestId) {
        setPreviewingArtifactId(null);
      }
    }
  }, []);

  useEffect(() => {
    if (!selected || selected.status === 'processing') return;
    const currentPreviewIsFresh =
      artifactPreview?.artifact.document_id === selected.id &&
      selected.artifacts.some((artifact) => artifact.id === artifactPreview.artifact.id);
    if (currentPreviewIsFresh) return;
    const markdownArtifact = selected.artifacts.find(
      (artifact) => artifact.kind === 'extracted_markdown' && artifact.media_type === 'text/markdown',
    );
    if (!markdownArtifact) {
      if (artifactPreview) clearArtifactPreview();
      return;
    }
    void previewArtifact(markdownArtifact);
  }, [
    artifactPreview?.artifact.document_id,
    artifactPreview?.artifact.id,
    clearArtifactPreview,
    previewArtifact,
    selected?.artifacts,
    selected?.id,
    selected?.status,
  ]);

  if (loading) return <Loading label="Loading papers…" />;

  const upload = async (file: File) => {
    setBusyAction('upload');
    setError(null);
    const form = new FormData();
    form.append('file', file);
    try {
      const created = await request<Document>('/documents', { method: 'POST', body: form });
      clearArtifactPreview();
      await load(created.id);
    } catch (nextError) {
      setError(nextError);
    } finally {
      setBusyAction(null);
    }
  };

  const downloadPdf = async () => {
    const url = remotePdfUrl.trim();
    if (!url) return;
    setBusyAction('download-pdf');
    setError(null);
    try {
      const created = await request<Document>(
        '/documents/download',
        json('POST', { url, title: remotePdfTitle.trim() || null }),
      );
      setRemotePdfUrl('');
      setRemotePdfTitle('');
      clearArtifactPreview();
      await load(created.id);
    } catch (nextError) {
      setError(nextError);
    } finally {
      setBusyAction(null);
    }
  };

  const ingest = async (document: Document, mode: IngestionMode) => {
    const action = `ingest:${document.id}`;
    const processing = {
      ...document,
      status: 'processing',
      metadata: {
        ...document.metadata,
        ingestion: {
          phase: mode === 'ocr' ? 'cleanup' : 'starting',
          phase_label:
            mode === 'ocr' ? 'Clearing old generated files' : 'Reading embedded text',
          mode,
        },
      },
    };
    setBusyAction(action);
    setError(null);
    clearArtifactPreview();
    setSelected(processing);
    setDocuments((current) => current.map((item) => (item.id === document.id ? processing : item)));
    const controller = new AbortController();
    ingestionRequest.current = controller;
    try {
      const updated = await request<Document>(`/documents/${document.id}/ingest?mode=${mode}`, {
        method: 'POST',
        signal: controller.signal,
      });
      setSelected(updated);
      setDocuments((items) => items.map((item) => (item.id === document.id ? updated : item)));
    } catch (nextError) {
      if (controller.signal.aborted) {
        await load(document.id);
        return;
      }
      setSelected(document);
      setDocuments((current) => current.map((item) => (item.id === document.id ? document : item)));
      setError(nextError);
    } finally {
      if (ingestionRequest.current === controller) {
        ingestionRequest.current = null;
      }
      setBusyAction((current) => (current === action ? null : current));
    }
  };

  const stopIngestion = async (document: Document) => {
    const action = `stop:${document.id}`;
    setBusyAction(action);
    setError(null);
    try {
      const updated = await request<Document>(
        `/documents/${document.id}/ingest/stop`,
        { method: 'POST' },
      );
      ingestionRequest.current?.abort();
      ingestionRequest.current = null;
      setSelected(updated);
      setDocuments((items) =>
        items.map((item) => (item.id === document.id ? updated : item)),
      );
    } catch (nextError) {
      setError(nextError);
      await load(document.id);
    } finally {
      setBusyAction((current) => (current === action ? null : current));
    }
  };

  const deleteDocument = async (document: Document) => {
    setBusyAction(`delete:${document.id}`);
    setError(null);
    try {
      await request<void>(`/documents/${document.id}`, { method: 'DELETE' });
      setSelected(null);
      clearArtifactPreview();
      await load();
    } catch (nextError) {
      setError(nextError);
    } finally {
      setBusyAction(null);
    }
  };

  const ingesting = Boolean(
    selected && (selected.status === 'processing' || busyAction === `ingest:${selected.id}`),
  );
  const operationBusy = busyAction !== null;
  const recommendedMode = ingestionOptions?.recommended_mode ?? 'embedded';
  const pageProgress = selected ? ingestionProgress(selected) : null;
  const importOpen = showImport;

  return (
    <div className="page">
      <PageHeader
        title="Papers"
        description="Your PDF library. Once a paper is added the agent can search, quote and summarise it."
        actions={
          <>
            <button
              className="button secondary"
              type="button"
              aria-expanded={importOpen}
              onClick={() => setShowImport((value) => !value)}
            >
              <Icon name="download" size={16} />
              Add from URL
            </button>
            <label className="button upload-button">
              <Icon name="upload" size={16} />
              {busyAction === 'upload' ? 'Uploading…' : 'Upload PDF'}
              <input
                type="file"
                accept=".pdf,application/pdf"
                disabled={operationBusy}
                onChange={(event) => event.target.files?.[0] && void upload(event.target.files[0])}
              />
            </label>
          </>
        }
      />
      <LibraryTabs active="papers" />
      {error ? <ErrorNotice error={error} /> : null}
      {importOpen ? (
      <div className="paper-import">
        <Panel
          title="Add a PDF by URL"
          description="Public PDFs are downloaded, indexed, and retained in your library."
        >
          <form
            className="source-form"
            onSubmit={(event) => {
              event.preventDefault();
              void downloadPdf();
            }}
          >
            <input
              aria-label="PDF URL"
              placeholder="https://arxiv.org/pdf/..."
              type="url"
              value={remotePdfUrl}
              disabled={operationBusy}
              onChange={(event) => setRemotePdfUrl(event.target.value)}
            />
            <input
              aria-label="Paper title"
              placeholder="Optional paper title"
              value={remotePdfTitle}
              disabled={operationBusy}
              onChange={(event) => setRemotePdfTitle(event.target.value)}
            />
            <button className="button" type="submit" disabled={operationBusy || !remotePdfUrl.trim()}>
              <Icon name="download" size={15} />
              {busyAction === 'download-pdf' ? 'Downloading and indexing…' : 'Download PDF'}
            </button>
          </form>
        </Panel>
      </div>
      ) : null}
      <div className="papers-layout">
        <Panel
          title="Library"
          description={documents.length ? `${documents.length} paper${documents.length === 1 ? '' : 's'}` : undefined}
        >
          <div className="stack-tight">
            {documents.map((document) => (
              <button
                type="button"
                className={selected?.id === document.id ? 'paper-row active' : 'paper-row'}
                key={document.id}
                onClick={() => {
                  clearArtifactPreview();
                  void request<Document>(`/documents/${encodeURIComponent(document.id)}`)
                    .then(setSelected)
                    .catch(setError);
                }}
              >
                <div>
                  <strong>{document.title}</strong>
                  <small>{document.source_filename}</small>
                </div>
                <StatusPill value={document.status} />
              </button>
            ))}
            {!documents.length ? <p>Upload or download a PDF to start the research library.</p> : null}
          </div>
        </Panel>
        <Panel title={selected?.title ?? 'Document details'} className="paper-detail-panel">
          {selected ? (
            <div className="stack" aria-busy={ingesting}>
              <div className="button-row">
                <StatusPill value={ingesting ? 'processing' : selected.status} />
                {!ingesting ? (
                  <>
                    <button
                      className={`button small${recommendedMode === 'embedded' ? '' : ' secondary'}`}
                      type="button"
                      disabled={operationBusy || ingestionOptions === null}
                      onClick={() => void ingest(selected, 'embedded')}
                    >
                      <Icon name="play" size={13} />
                      {selected.status === 'ready' ? 'Re-index embedded text' : 'Use embedded text'}
                    </button>
                    <button
                      className={`button small${recommendedMode === 'ocr' ? '' : ' secondary'}`}
                      type="button"
                      title={
                        ingestionOptions && !ingestionOptions.ocr_available
                          ? `${ingestionOptions.ocr_engine} OCR runtime is unavailable`
                          : 'Render every page and run OCR'
                      }
                      disabled={
                        operationBusy ||
                        ingestionOptions === null ||
                        !ingestionOptions.ocr_available
                      }
                      onClick={() => void ingest(selected, 'ocr')}
                    >
                      <Icon name="scan" size={13} />
                      {selected.status === 'ready' ? 'Re-run with OCR' : 'Run OCR'}
                    </button>
                  </>
                ) : (
                  <span className="button busy-label">
                    <span className="button-spinner" aria-hidden="true" />
                    Ingesting and indexing…
                  </span>
                )}
                {ingesting ? (
                  <button
                    className="button danger small"
                    type="button"
                    disabled={busyAction === `stop:${selected.id}`}
                    onClick={() => void stopIngestion(selected)}
                  >
                    <Icon name="stop" size={13} />
                    {busyAction === `stop:${selected.id}` ? 'Stopping…' : 'Stop'}
                  </button>
                ) : null}
                <button
                  className="button danger small"
                  type="button"
                  title="Delete paper"
                  disabled={operationBusy || selected.status === 'processing'}
                  onClick={() => void deleteDocument(selected)}
                >
                  {busyAction === `delete:${selected.id}` ? 'Deleting…' : 'Delete'}
                </button>
              </div>
              {!ingesting ? (
                <div className="ingestion-recommendation">
                  {ingestionOptions ? (
                    <>
                      <strong>
                        Suggested: {recommendedMode === 'embedded' ? 'Use embedded text' : 'Run OCR'}
                      </strong>
                      <span>
                        Selectable text detected on {ingestionOptions.embedded_text_pages} of{' '}
                        {ingestionOptions.total_pages} pages.
                        {!ingestionOptions.ocr_available
                          ? ` ${ingestionOptions.ocr_engine} OCR is currently unavailable.`
                          : ''}
                      </span>
                    </>
                  ) : (
                    <span>Inspecting the PDF text layer…</span>
                  )}
                </div>
              ) : null}
              {ingesting ? (
                <div className="notice info ingestion-progress" role="status" aria-live="polite">
                  <div className="ingestion-progress-summary">
                    <span className="spinner" aria-hidden="true" />
                    <span>{ingestionStatus(selected)} {ingestionDetail(selected)}</span>
                  </div>
                  {pageProgress ? (
                    <div className="page-progress">
                      <div className="page-progress-label">
                        <strong>Pages completed</strong>
                        <span>
                          {pageProgress.completed} / {pageProgress.total} ({pageProgress.percent}%)
                        </span>
                      </div>
                      <progress
                        aria-label="OCR pages completed"
                        max={pageProgress.total}
                        value={pageProgress.completed}
                      />
                    </div>
                  ) : null}
                </div>
              ) : null}
              {!ingesting ? (
                <div className="card-meta">
                  <span>{selected.page_count ?? '—'} pages</span>
                  <span>{selected.chunks.length} chunks</span>
                  <span>{selected.artifacts.length} artifacts</span>
                </div>
              ) : null}
              {!ingesting && selected.artifacts.length ? (
                <div>
                  <span className="eyebrow">Artifacts</span>
                  <div className="button-row">
                    {selected.artifacts.map((artifact) => {
                      const previewable =
                        artifact.media_type.startsWith('text/') || artifact.media_type === 'application/json';
                      return previewable ? (
                        <button
                          className="button secondary small"
                          type="button"
                          key={artifact.id}
                          disabled={previewingArtifactId !== null}
                          aria-pressed={artifactPreview?.artifact.id === artifact.id}
                          onClick={() => void previewArtifact(artifact)}
                        >
                          {previewingArtifactId === artifact.id ? 'Loading preview…' : displayKind(artifact.kind)}
                        </button>
                      ) : (
                        <a
                          className="button secondary small"
                          target="_blank"
                          rel="noreferrer"
                          key={artifact.id}
                          href={apiUrl(`/artifacts/${artifact.id}/raw?sha256=${artifact.sha256}`)}
                        >
                          {displayKind(artifact.kind)}
                        </a>
                      );
                    })}
                  </div>
                </div>
              ) : null}
              {!ingesting && artifactPreview ? (
                <section className="artifact-preview">
                  <header className="artifact-preview-header">
                    <div>
                      <span className="eyebrow">Preview</span>
                      <strong>{displayKind(artifactPreview.artifact.kind)}</strong>
                    </div>
                    <a
                      className="button secondary small"
                      target="_blank"
                      rel="noreferrer"
                      href={apiUrl(
                        `/artifacts/${artifactPreview.artifact.id}/raw?sha256=${artifactPreview.artifact.sha256}`,
                      )}
                    >
                      Open raw
                    </a>
                  </header>
                  <div className="artifact-preview-content">
                    {artifactPreview.artifact.media_type === 'text/markdown' &&
                    typeof artifactPreview.content === 'string' ? (
                      <MarkdownViewer content={artifactPreview.content} />
                    ) : (
                      <pre className="artifact-source">
                        {typeof artifactPreview.content === 'string'
                          ? artifactPreview.content
                          : JSON.stringify(artifactPreview.content, null, 2)}
                      </pre>
                    )}
                  </div>
                </section>
              ) : null}
              {!ingesting ? <ExtractionChunks chunks={selected.chunks} /> : null}
            </div>
          ) : (
            <EmptyState
              icon="papers"
              title="No paper selected"
              description="Pick a paper on the left to inspect its ingestion status, artifacts and retrieval chunks."
            />
          )}
        </Panel>
      </div>
    </div>
  );
}
