import { useEffect, useRef, useState } from 'react';
import { apiUrl, json, request } from '../../api/client';
import type { components } from '../../api/schema.generated';
import { Icon } from '../../shared/components/Icons';
import { MarkdownViewer } from '../../shared/components/MarkdownViewer';
import { EmptyState, ErrorNotice, LibraryTabs, Loading, PageHeader, Panel, StatusPill } from '../../shared/components/Ui';
import { PaperSummaryPanel } from './PaperSummaryPanel';
import '../library.css';

type Document = components['schemas']['DocumentResponse'];
type DocumentSummary = components['schemas']['DocumentSummaryResponse'];
type ArtifactContent = components['schemas']['ArtifactContentResponse'];
type IngestionOptions = components['schemas']['IngestionOptionsResponse'];
type PaperFolder = components['schemas']['PaperFolderResponse'];
type IngestionMode = IngestionOptions['recommended_mode'];
type IngestionProgress = {
  completed: number;
  total: number;
  percent: number;
};

function ingestionStatus(document: Document): string {
  const value = document.metadata.ingestion;
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return 'Extracting text and searchable chunks. Large papers can take several minutes.';
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
  const [folders, setFolders] = useState<PaperFolder[]>([]);
  const [activeFolder, setActiveFolder] = useState<string>('all');
  const [newFolderName, setNewFolderName] = useState('');
  const [selected, setSelected] = useState<Document | null>(null);
  const [ingestionOptions, setIngestionOptions] = useState<IngestionOptions | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [remotePdfUrl, setRemotePdfUrl] = useState('');
  const [remotePdfTitle, setRemotePdfTitle] = useState('');
  const [selectedPage, setSelectedPage] = useState(1);
  const [viewerMode, setViewerMode] = useState<'pdf' | 'text'>('pdf');
  const [retrievedText, setRetrievedText] = useState<string | null>(null);
  const [showImport, setShowImport] = useState(false);
  const ingestionRequest = useRef<AbortController | null>(null);
  const textRequestId = useRef(0);

  const load = async (selectedId?: string | null) => {
    const [items, nextFolders] = await Promise.all([
      request<DocumentSummary[]>('/documents'),
      request<PaperFolder[]>('/paper-folders'),
    ]);
    const targetId = selectedId === null ? items[0]?.id : selectedId ?? selected?.id ?? items[0]?.id;
    const detail = targetId
      ? await request<Document>(`/documents/${encodeURIComponent(targetId)}`)
      : null;
    setDocuments(items);
    setFolders(nextFolders);
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

  useEffect(() => {
    textRequestId.current += 1;
    setSelectedPage(1);
    setViewerMode('pdf');
    setRetrievedText(null);
  }, [selected?.id]);

  if (loading) return <Loading label="Loading papers…" />;

  const upload = async (file: File) => {
    setBusyAction('upload');
    setError(null);
    const form = new FormData();
    form.append('file', file);
    try {
      const created = await request<Document>('/documents', { method: 'POST', body: form });
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

  const deleteDocument = async (document: DocumentSummary) => {
    if (!window.confirm(`Delete "${document.title}" and all of its saved files? This cannot be undone.`)) {
      return;
    }
    setBusyAction(`delete:${document.id}`);
    setError(null);
    try {
      await request<void>(`/documents/${encodeURIComponent(document.id)}`, { method: 'DELETE' });
      const deletedSelection = selected?.id === document.id;
      if (deletedSelection) setSelected(null);
      await load(deletedSelection ? null : selected?.id);
    } catch (nextError) {
      setError(nextError);
    } finally {
      setBusyAction(null);
    }
  };

  const deleteFolder = async (folder: PaperFolder) => {
    const paperCount = documents.filter((document) => folderId(document) === folder.id).length;
    const paperLabel = `${paperCount} paper${paperCount === 1 ? '' : 's'}`;
    if (!window.confirm(`Delete the "${folder.name}" folder? Its ${paperLabel} will remain in Papers.`)) {
      return;
    }
    setBusyAction(`delete-folder:${folder.id}`);
    setError(null);
    try {
      await request<void>(`/paper-folders/${encodeURIComponent(folder.id)}`, {
        method: 'DELETE',
      });
      setActiveFolder('unfiled');
      await load(selected?.id);
    } catch (nextError) {
      setError(nextError);
    } finally {
      setBusyAction(null);
    }
  };

  const createFolder = async () => {
    const name = newFolderName.trim();
    if (!name) return;
    setBusyAction('create-folder');
    setError(null);
    try {
      const folder = await request<PaperFolder>(
        '/paper-folders',
        json('POST', { name }),
      );
      setFolders((current) => [...current, folder].sort((a, b) => a.name.localeCompare(b.name)));
      setNewFolderName('');
      setActiveFolder(folder.id);
    } catch (nextError) {
      setError(nextError);
    } finally {
      setBusyAction(null);
    }
  };

  const movePaper = async (document: Document, folderId: string | null) => {
    const action = `folder:${document.id}`;
    setBusyAction(action);
    setError(null);
    try {
      const updated = await request<Document>(
        `/documents/${encodeURIComponent(document.id)}/folder`,
        json('PUT', { folder_id: folderId }),
      );
      setSelected(updated);
      setDocuments((items) => items.map((item) => (item.id === updated.id ? updated : item)));
    } catch (nextError) {
      setError(nextError);
    } finally {
      setBusyAction(null);
    }
  };

  const showRetrievedText = async () => {
    if (!selected) return;
    const artifact = selected.artifacts.find(
      (item) => item.kind === 'extracted_markdown' && item.media_type === 'text/markdown',
    );
    if (!artifact) return;
    setViewerMode('text');
    if (retrievedText !== null) return;
    const action = `text:${selected.id}`;
    const requestId = textRequestId.current + 1;
    textRequestId.current = requestId;
    setBusyAction(action);
    setError(null);
    try {
      const preview = await request<ArtifactContent>(`/artifacts/${artifact.id}/content`, {
        cache: 'no-store',
      });
      if (typeof preview.content !== 'string') {
        throw new Error('The retrieved paper text is not available as text.');
      }
      if (textRequestId.current === requestId) {
        setRetrievedText(preview.content);
      }
    } catch (nextError) {
      if (textRequestId.current === requestId) {
        setViewerMode('pdf');
        setError(nextError);
      }
    } finally {
      setBusyAction((current) => (current === action ? null : current));
    }
  };

  const ingesting = Boolean(
    selected && (selected.status === 'processing' || busyAction === `ingest:${selected.id}`),
  );
  const operationBusy = busyAction !== null;
  const recommendedMode = ingestionOptions?.recommended_mode ?? 'embedded';
  const pageProgress = selected ? ingestionProgress(selected) : null;
  const importOpen = showImport;
  const sourcePdf = selected?.artifacts.find((artifact) => artifact.kind === 'source_pdf');
  const extractedText = selected?.artifacts.find(
    (artifact) => artifact.kind === 'extracted_markdown' && artifact.media_type === 'text/markdown',
  );
  const folderId = (document: DocumentSummary): string | null => {
    const value = document.metadata.folder_id;
    return typeof value === 'string' ? value : null;
  };
  const visibleDocuments = documents.filter((document) => {
    if (activeFolder === 'all') return true;
    if (activeFolder === 'unfiled') return folderId(document) === null;
    return folderId(document) === activeFolder;
  });
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
            <form
              className="paper-folder-form"
              onSubmit={(event) => {
                event.preventDefault();
                void createFolder();
              }}
            >
              <input
                aria-label="New paper folder"
                placeholder="New folder"
                value={newFolderName}
                disabled={operationBusy}
                onChange={(event) => setNewFolderName(event.target.value)}
              />
              <button
                className="button small secondary"
                type="submit"
                disabled={operationBusy || !newFolderName.trim()}
              >
                Add
              </button>
            </form>
            <nav className="paper-folder-list" aria-label="Paper folders">
              <button
                type="button"
                className={`paper-folder-target${activeFolder === 'all' ? ' active' : ''}`}
                onClick={() => setActiveFolder('all')}
              >
                <span>All papers</span>
                <small>{documents.length}</small>
              </button>
              <button
                type="button"
                className={`paper-folder-target${activeFolder === 'unfiled' ? ' active' : ''}`}
                onClick={() => setActiveFolder('unfiled')}
              >
                <span>Papers</span>
                <small>{documents.filter((document) => folderId(document) === null).length}</small>
              </button>
              {folders.map((folder) => (
                <div className="paper-folder-row" key={folder.id}>
                  <button
                    type="button"
                    className={`paper-folder-target${activeFolder === folder.id ? ' active' : ''}`}
                    onClick={() => setActiveFolder(folder.id)}
                  >
                    <span>{folder.name}</span>
                    <small>
                      {documents.filter((document) => folderId(document) === folder.id).length}
                    </small>
                  </button>
                  <button
                    type="button"
                    className="button danger icon paper-folder-delete"
                    aria-label={`Delete folder ${folder.name}`}
                    title={`Delete ${folder.name}`}
                    disabled={operationBusy}
                    onClick={() => void deleteFolder(folder)}
                  >
                    <Icon name="trash" size={14} />
                  </button>
                </div>
              ))}
            </nav>
            {visibleDocuments.map((document) => (
              <div
                className={selected?.id === document.id ? 'paper-row active' : 'paper-row'}
                key={document.id}
              >
                <button
                  type="button"
                  className="paper-row-main"
                  onClick={() => {
                    void request<Document>(`/documents/${encodeURIComponent(document.id)}`)
                      .then(setSelected)
                      .catch(setError);
                  }}
                >
                  <div>
                    <strong>{document.title}</strong>
                    <small>{document.source_filename}</small>
                  </div>
                </button>
                <button
                  type="button"
                  className="button danger icon paper-row-delete"
                  aria-label={`Delete paper ${document.title}`}
                  title={`Delete ${document.title}`}
                  disabled={operationBusy || document.status === 'processing'}
                  onClick={() => void deleteDocument(document)}
                >
                  <Icon name="trash" size={14} />
                </button>
              </div>
            ))}
            {!documents.length ? <p>Upload or download a PDF to start the research library.</p> : null}
            {documents.length && !visibleDocuments.length ? <p>No papers in this folder.</p> : null}
          </div>
        </Panel>
        <Panel title={selected?.title ?? 'Document details'} className="paper-detail-panel">
          {selected ? (
            <div className="stack" aria-busy={ingesting}>
              <div className="button-row">
                <StatusPill value={ingesting ? 'processing' : selected.status} />
                <label className="paper-folder-select">
                  <span>Folder</span>
                  <select
                    aria-label="Paper folder"
                    value={folderId(selected) ?? ''}
                    disabled={operationBusy}
                    onChange={(event) => {
                      void movePaper(selected, event.target.value || null);
                    }}
                  >
                    <option value="">Papers</option>
                    {folders.map((folder) => (
                      <option value={folder.id} key={folder.id}>{folder.name}</option>
                    ))}
                  </select>
                </label>
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
                  <Icon name="trash" size={13} />
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
              {!ingesting ? (
                <PaperSummaryPanel
                  documentId={selected.id}
                  ready={selected.status === 'ready' && selected.chunks.length > 0}
                />
              ) : null}
              {!ingesting && sourcePdf ? (
                <div className="paper-view-switcher" role="group" aria-label="Paper view">
                  <button
                    className={`button small${viewerMode === 'pdf' ? '' : ' secondary'}`}
                    type="button"
                    aria-pressed={viewerMode === 'pdf'}
                    onClick={() => setViewerMode('pdf')}
                  >
                    PDF
                  </button>
                  <button
                    className={`button small${viewerMode === 'text' ? '' : ' secondary'}`}
                    type="button"
                    aria-pressed={viewerMode === 'text'}
                    disabled={!extractedText || busyAction === `text:${selected.id}`}
                    title={extractedText ? 'Show the text retrieved during ingestion' : 'Ingest this paper to retrieve its text'}
                    onClick={() => void showRetrievedText()}
                  >
                    {busyAction === `text:${selected.id}` ? 'Loading text…' : 'Retrieved text'}
                  </button>
                </div>
              ) : null}
              {!ingesting && sourcePdf && viewerMode === 'pdf' ? (
                <section className="paper-pdf-viewer">
                  <nav className="paper-page-rail" aria-label="PDF pages">
                    {Array.from({ length: selected.page_count ?? 1 }, (_, index) => index + 1).map((page) => (
                      <button
                        type="button"
                        className={selectedPage === page ? 'active' : ''}
                        aria-label={`Go to page ${page}`}
                        aria-current={selectedPage === page ? 'page' : undefined}
                        key={page}
                        onClick={() => setSelectedPage(page)}
                      >
                        {page}
                      </button>
                    ))}
                  </nav>
                  <iframe
                    key={`${sourcePdf.id}:${selectedPage}`}
                    title={`${selected.title}, page ${selectedPage}`}
                    src={`${apiUrl(`/artifacts/${sourcePdf.id}/raw?sha256=${sourcePdf.sha256}`)}#page=${selectedPage}&view=FitH`}
                  />
                </section>
              ) : null}
              {!ingesting && viewerMode === 'text' && retrievedText !== null ? (
                <section className="retrieved-text-view" aria-label="Retrieved paper text">
                  <MarkdownViewer content={retrievedText} />
                </section>
              ) : null}
            </div>
          ) : (
            <EmptyState
              icon="papers"
              title="No paper selected"
              description="Pick a paper on the left to read the full PDF."
            />
          )}
        </Panel>
      </div>
    </div>
  );
}
