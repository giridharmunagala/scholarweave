import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { apiUrl, json, request } from '../../api/client';
import type { components } from '../../api/schema.generated';
import { useAsk } from '../../app/ask';
import { Link, useLocation, useNavigate } from '../../app/router';
import { Icon } from '../../shared/components/Icons';
import { MarkdownViewer } from '../../shared/components/MarkdownViewer';
import { SettingsDrawer } from '../../shared/components/SettingsDrawer';
import { EmptyState, ErrorNotice, IconButton, Loading, StatusPill } from '../../shared/components/Ui';
import { PaperNotesDrawer, quoteMarkdown, usePaperNotes } from './PaperNotes';
import { PaperSummaryPanel, SummaryView } from './PaperSummaryPanel';
import type { SummaryContent } from './summaryApi';
import { PaperTopicsView, topicsApi, type TopicMap } from './PaperTopics';
import { PdfReader } from './PdfReader';
import { ReaderAsk } from './ReaderAsk';
import { SummaryBatchPanel } from './SummaryBatchPanel';
import { OcrBatchPanel, useOcrBatch } from './OcrBatchPanel';
import { ArxivSearch } from './ArxivSearch';
import { buildLibraryIndex, collectionTree, paperFolderId as folderId, sortPapers } from './libraryModel';
import { clearPaperDownload, downloadPaper, usePaperDownload } from './paperDownload';
import '../library.css';

type Document = components['schemas']['DocumentResponse'];
type DocumentSummary = components['schemas']['DocumentSummaryResponse'];
type DocumentSearch = components['schemas']['DocumentSearchResponse'];
type ArtifactContent = components['schemas']['ArtifactContentResponse'];
type IngestionOptions = components['schemas']['IngestionOptionsResponse'];
type PaperFolder = components['schemas']['PaperFolderResponse'];
type PaperUsefulness = components['schemas']['PaperUsefulness'];
type IngestionMode = IngestionOptions['recommended_mode'] | 'quick';
const PAGE_SIZE = 25;
const ABSTRACT_PREVIEW_LENGTH = 280;
const USEFULNESS_OPTIONS: { value: PaperUsefulness; label: string }[] = [
  { value: 'unrated', label: 'Unrated' },
  { value: 'useful', label: 'Useful' },
  { value: 'maybe_useful', label: 'Maybe useful' },
  { value: 'not_useful', label: 'Not useful' },
];
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

function DownloadProgress({ startedAt }: { startedAt: number }) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const seconds = Math.max(0, Math.floor((now - startedAt) / 1000));
  const elapsed = seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;

  return (
    <div className="notice info paper-download-progress">
      <div className="page-progress-label">
        <strong role="status">Downloading and preparing paper…</strong>
        <span role="timer" aria-live="off">{elapsed} elapsed</span>
      </div>
      <div className="paper-download-bar" role="progressbar" aria-label="Paper download and preparation" aria-describedby="paper-download-hint" />
      <p id="paper-download-hint" className="field-hint">
        Time remaining is unavailable. Large PDFs or OCR can take several minutes.
      </p>
    </div>
  );
}

interface ReadingEntry { page: number; pages: number; at: number }
function ReadingProgress({ entry, title }: { entry?: ReadingEntry; title: string }) {
  const percent = !entry ? 0 : entry.pages > 0
    ? Math.min(100, Math.max(0, Math.round((entry.page / entry.pages) * 100))) : null;
  return (
    <span className="paper-reading-progress" role="progressbar" aria-label={`Reading progress for ${title}`}
      aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent ?? undefined}
      aria-valuetext={entry ? `Page ${entry.page}${entry.pages > 0 ? ` of ${entry.pages}` : ''}` : 'Not started'}>
      <span className="paper-reading-track"><span style={{ width: `${percent ?? 0}%` }} /></span>
      <small>{percent === null ? `p. ${entry?.page}` : `${percent}%`}</small>
    </span>
  );
}

const READING_KEY = 'sw.reading';
function readingLog(): Record<string, ReadingEntry> {
  try { return JSON.parse(window.localStorage.getItem(READING_KEY) ?? '{}') as Record<string, ReadingEntry>; } catch { return {}; }
}
function recordReading(id: string, page: number, pages: number) {
  try {
    const log = readingLog();
    log[id] = { page, pages, at: Date.now() };
    const recent = Object.entries(log).sort((a, b) => b[1].at - a[1].at).slice(0, 50);
    window.localStorage.setItem(READING_KEY, JSON.stringify(Object.fromEntries(recent)));
  } catch { /* storage unavailable */ }
}

const NEW_FOLDER = '__new__';
const PAPER_STATUS_LABELS: Record<string, string> = {
  ready: 'Ready to read',
  uploaded: 'Not yet prepared',
  processing: 'Preparing',
  failed: 'Needs attention',
};

export default function PapersPage() {
  const { searchParams, pathname } = useLocation();
  const navigate = useNavigate();
  const ask = useAsk();
  const linkedPaper = searchParams.get('paper') || searchParams.get('document');
  const pageParameter = searchParams.get('page');
  const requestedPage = pageParameter && /^\d+$/.test(pageParameter) && Number.isSafeInteger(Number(pageParameter)) && Number(pageParameter) > 0
    ? Number(pageParameter) : null;
  const basePath = pathname;
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [expandedAbstracts, setExpandedAbstracts] = useState<Set<string>>(new Set());
  const [sidePanel, setSidePanel] = useState<'notes' | 'details' | 'summary' | null>(null);
  const [toast, setToast] = useState<{ message: string; action?: { label: string; run: () => void } } | null>(null);
  const [dropTarget, setDropTarget] = useState<string | null>(null);
  const [reading, setReading] = useState(readingLog);
  const paperNotes = usePaperNotes(linkedPaper);
  const linkedHighlight = searchParams.get('highlight');
  useEffect(() => { if (linkedHighlight) setSidePanel('notes'); }, [linkedPaper, linkedHighlight]);
  const [documents, setDocuments] = useState<DocumentSummary[]>([]);
  const [folders, setFolders] = useState<PaperFolder[]>([]);
  const [activeFolder, setActiveFolder] = useState<string>('all');
  const [newFolderName, setNewFolderName] = useState('');
  const [query, setQuery] = useState('');
  const [paperStatus, setPaperStatus] = useState('');
  const [usefulness, setUsefulness] = useState('');
  const [sort, setSort] = useState('newest');
  const [offset, setOffset] = useState(0);
  const [searchResults, setSearchResults] = useState<DocumentSearch[]>([]);
  const [searching, setSearching] = useState(false);
  const [selected, setSelected] = useState<Document | null>(null);
  const [readerSequence, setReaderSequence] = useState<DocumentSummary[] | null>(null);
  const [ingestionOptions, setIngestionOptions] = useState<IngestionOptions | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const download = usePaperDownload();
  const downloadStartedAt = download.status === 'running' ? download.startedAt : null;
  const [remotePdfUrl, setRemotePdfUrl] = useState(() => download.status === 'running' || download.status === 'failed' ? download.url : '');
  const [remotePdfTitle, setRemotePdfTitle] = useState(() => download.status === 'running' || download.status === 'failed' ? download.title : '');
  const [selectedPage, setSelectedPage] = useState(1);
  const [readerPage, setReaderPage] = useState<number | null>(null);
  const [viewerMode, setViewerMode] = useState<'pdf' | 'text' | 'summary'>('pdf');
  const [openedSummary, setOpenedSummary] = useState<SummaryContent | null>(null);
  const [retrievedText, setRetrievedText] = useState<string | null>(null);
  const [showImport, setShowImport] = useState(() => searchParams.get('add') === '1');
  const [showSummaryBatch, setShowSummaryBatch] = useState(false);
  const [showOcrBatch, setShowOcrBatch] = useState(false);
  const ocrBatch = useOcrBatch();
  const [ocrRefreshError, setOcrRefreshError] = useState<unknown>(null);
  const previousOcrState = useRef<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [showFolderColumn, setShowFolderColumn] = useState(() => localStorage.getItem('scholarweave.papersFolderColumn') === 'true');
  const [topicLayout, setTopicLayout] = useState(() => localStorage.getItem('scholarweave.papersLayout') === 'topics');
  const [assignmentTopics, setAssignmentTopics] = useState<{ folderId: string | null; map: TopicMap | null } | null>(null);
  const [topicsRevision, setTopicsRevision] = useState(0);
  const ingestionRequest = useRef<AbortController | null>(null);
  const textRequestId = useRef(0);
  const selectionRequestId = useRef(0);
  const currentPaperId = useRef(linkedPaper);
  currentPaperId.current = linkedPaper;
  const library = useMemo(() => buildLibraryIndex(documents, folders), [documents, folders]);
  const openPaper = (id: string) => {
    if (!linkedPaper) setReaderSequence(visibleDocuments);
    navigate(`${basePath}?${new URLSearchParams({ paper: id })}`);
  };

  const load = async (selectedId?: string | null, signal?: AbortSignal) => {
    const targetId = selectedId === null ? null : selectedId ?? currentPaperId.current;
    const requestId = targetId === currentPaperId.current ? ++selectionRequestId.current : null;
    const [items, nextFolders] = await Promise.all([
      request<DocumentSummary[]>('/documents', { signal }),
      request<PaperFolder[]>('/paper-folders', { signal }),
    ]);
    if (signal?.aborted) return;
    const detail = targetId
      ? await request<Document>(`/documents/${encodeURIComponent(targetId)}`, { signal })
      : null;
    if (signal?.aborted) return;
    setDocuments(items);
    setFolders(nextFolders);
    if (selectionRequestId.current === requestId && targetId === currentPaperId.current) setSelected(detail);
  };

  useEffect(() => {
    let active = true;
    if (!documents.length) setLoading(true);
    setSelected((current) => current?.id === linkedPaper ? current : null);
    setError(null);
    load(linkedPaper ?? null)
      .catch((reason) => { if (active) setError(reason); })
      .finally(() => { if (active) setLoading(false); });
    if (linkedPaper) setReading(readingLog());
    return () => { active = false; };
  }, [linkedPaper]);

  const ocrState = JSON.stringify([ocrBatch.id, ocrBatch.status, ocrBatch.items.map((item) => [item.document_id, item.status])]);
  useEffect(() => {
    if (ocrBatch.loading || previousOcrState.current === ocrState) return;
    const initial = previousOcrState.current === null;
    previousOcrState.current = ocrState;
    if (initial && ocrBatch.status !== 'running' && ocrBatch.status !== 'stopping') return;
    const controller = new AbortController();
    setOcrRefreshError(null);
    void load(undefined, controller.signal).catch((reason) => {
      if (!controller.signal.aborted) setOcrRefreshError(reason);
    });
    return () => controller.abort();
  }, [ocrState, ocrBatch.loading, linkedPaper]);

  useEffect(() => {
    if (download.status !== 'completed' && download.status !== 'failed') return;
    if (!clearPaperDownload(download)) return;
    if (download.status === 'completed') {
      setRemotePdfUrl('');
      setRemotePdfTitle('');
      setShowImport(false);
      navigate(`${basePath}?${new URLSearchParams({ paper: download.paper.id })}`);
    } else {
      setRemotePdfUrl(download.url);
      setRemotePdfTitle(download.title);
      setError(download.error);
    }
  }, [download, navigate, basePath]);

  useEffect(() => { if (searchParams.get('add') === '1') setShowImport(true); }, [searchParams.get('add')]);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 5000);
    return () => window.clearTimeout(timer);
  }, [toast]);

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
    setSelectedPage(requestedPage ?? 1);
    setReaderPage(requestedPage);
    setViewerMode('pdf');
    setOpenedSummary(null);
    setRetrievedText(null);
  }, [selected?.id, requestedPage]);

  useEffect(() => {
    if (!query.trim()) {
      setSearchResults([]);
      setSearching(false);
      return;
    }
    let active = true;
    const controller = new AbortController();
    setSearching(true);
    setSearchResults([]);
    setError(null);
    const timer = window.setTimeout(() => {
      const params = new URLSearchParams({ query: query.trim(), limit: String(PAGE_SIZE + 1), offset: String(offset) });
      if (activeFolder === 'unfiled') params.set('unfiled', 'true');
      else if (activeFolder !== 'all') params.set('folder_id', activeFolder);
      if (paperStatus) params.set('paper_status', paperStatus);
      if (usefulness) params.set('usefulness', usefulness);
      request<DocumentSearch[]>(`/documents/search?${params}`, { signal: controller.signal })
        .then((items) => {
          if (!active) return;
          setSearchResults(items);
          if (!items.length && offset > 0) setOffset(Math.max(0, offset - PAGE_SIZE));
        })
        .catch((reason) => { if (active) setError(reason); })
        .finally(() => { if (active) setSearching(false); });
    }, 200);
    return () => { active = false; window.clearTimeout(timer); controller.abort(); };
  }, [query, activeFolder, paperStatus, usefulness, offset, documents]);

  useEffect(() => {
    if (query.trim()) return;
    const members = activeFolder === 'all' ? [...library.byId.values()]
      : activeFolder === 'unfiled' ? library.unfiled : library.byFolder.get(activeFolder) ?? [];
    const count = members.filter((document) =>
      (!paperStatus || document.status === paperStatus) &&
      (!usefulness || (document.usefulness ?? 'unrated') === usefulness),
    ).length;
    setOffset((current) => Math.min(current, Math.max(0, Math.ceil(count / PAGE_SIZE) - 1) * PAGE_SIZE));
  }, [documents, library, activeFolder, paperStatus, usefulness, query]);

  useEffect(() => {
    if (!linkedPaper || viewerMode === 'pdf') return undefined;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.defaultPrevented || ask.open) return;
      const target = event.target instanceof Element ? event.target : null;
      if (target?.closest('input, textarea, select, [contenteditable="true"]')) return;
      setViewerMode('pdf');
      setOpenedSummary(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [linkedPaper, viewerMode, ask.open]);

  const downloadProgress = downloadStartedAt !== null ? <DownloadProgress startedAt={downloadStartedAt} /> : null;
  if (loading) return <>{downloadProgress}<Loading label="Loading papers…" /></>;

  const upload = async (file: File) => {
    setBusyAction('upload');
    setError(null);
    const form = new FormData();
    form.append('file', file);
    try {
      const created = await request<Document>('/documents', { method: 'POST', body: form });
      setShowImport(false);
      openPaper(created.id);
    } catch (nextError) {
      setError(nextError);
    } finally {
      setBusyAction(null);
    }
  };

  const downloadPdf = () => {
    const url = remotePdfUrl.trim();
    if (!url || downloadStartedAt !== null) return;
    setError(null);
    void downloadPaper(url, remotePdfTitle.trim());
  };

  const ingest = async (document: Document, mode: IngestionMode) => {
    const action = `ingest:${document.id}`;
    const processing = {
      ...document,
      status: 'processing',
      metadata: {
        ...document.metadata,
        ingestion: {
          phase: 'starting',
          phase_label:
            mode === 'quick' ? 'Starting Indra CPU OCR' : mode === 'ocr' ? 'Starting page OCR' : 'Reading embedded text',
          mode,
        },
      },
    };
    setBusyAction(action);
    setError(null);
    setSelected((current) => current?.id === document.id && currentPaperId.current === document.id ? processing : current);
    setDocuments((current) => current.map((item) => (item.id === document.id ? processing : item)));
    const controller = new AbortController();
    ingestionRequest.current = controller;
    try {
      const updated = await request<Document>(`/documents/${document.id}/ingest?mode=${mode}`, {
        method: 'POST',
        signal: controller.signal,
      });
      setSelected((current) => current?.id === document.id && currentPaperId.current === document.id ? updated : current);
      setDocuments((items) => items.map((item) => (item.id === document.id ? updated : item)));
    } catch (nextError) {
      if (controller.signal.aborted) {
        await load(document.id);
        return;
      }
      setSelected((current) => current?.id === document.id && currentPaperId.current === document.id ? document : current);
      setDocuments((current) => current.map((item) => (item.id === document.id ? document : item)));
      if (currentPaperId.current === document.id) setError(nextError);
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
      setSelected((current) => current?.id === document.id && currentPaperId.current === document.id ? updated : current);
      setDocuments((items) =>
        items.map((item) => (item.id === document.id ? updated : item)),
      );
    } catch (nextError) {
      if (currentPaperId.current === document.id) setError(nextError);
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
      setChecked((current) => { const next = new Set(current); next.delete(document.id); return next; });
      if (selected?.id === document.id) navigate(basePath);
      else await load(selected?.id ?? null);
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
      setOffset(0);
      await load(selected?.id ?? null);
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
      setOffset(0);
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
      setSelected((current) => current?.id === updated.id ? updated : current);
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
  const operationBusy = busyAction !== null || downloadStartedAt !== null
    || ocrBatch.status === 'running' || ocrBatch.status === 'stopping';
  const recommendedMode = ingestionOptions?.recommended_mode ?? 'embedded';
  const pageProgress = selected ? ingestionProgress(selected) : null;
  const sourcePdf = selected?.artifacts.find((artifact) => artifact.kind === 'source_pdf');
  const extractedText = selected?.artifacts.find(
    (artifact) => artifact.kind === 'extracted_markdown' && artifact.media_type === 'text/markdown',
  );
  const folderDocuments = activeFolder === 'all' ? [...library.byId.values()]
    : activeFolder === 'unfiled' ? library.unfiled : library.byFolder.get(activeFolder) ?? [];
  const topicScope = activeFolder === 'all' ? null : activeFolder === 'unfiled' ? undefined : activeFolder;
  const assignmentScope = topicScope ?? null;
  const assignmentMap = assignmentTopics?.folderId === assignmentScope ? assignmentTopics.map : undefined;
  const filteredDocuments = folderDocuments.filter((document) =>
    (!paperStatus || document.status === paperStatus) &&
    (!usefulness || (document.usefulness ?? 'unrated') === usefulness),
  );
  const sortedDocuments = sortPapers(filteredDocuments, sort);
  const visibleDocuments = query.trim() ? searchResults.slice(0, PAGE_SIZE) : sortedDocuments.slice(offset, offset + PAGE_SIZE);
  const hasNextPage = query.trim() ? searchResults.length > PAGE_SIZE : offset + PAGE_SIZE < sortedDocuments.length;
  const folderName = (id: string | null) => folders.find((folder) => folder.id === id)?.name ?? null;
  const chooseFolder = (id: string) => { setActiveFolder(id); setOffset(0); };
  const notify = (message: string, action?: { label: string; run: () => void }) => setToast({ message, action });
  const checkedPapers = documents.filter((document) => checked.has(document.id));
  const toggleChecked = (id: string) => setChecked((current) => {
    const next = new Set(current);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });
  const allVisibleChecked = visibleDocuments.length > 0 && visibleDocuments.every((document) => checked.has(document.id));

  const ratePaper = async (document: DocumentSummary, value: PaperUsefulness) => {
    setBusyAction(`usefulness:${document.id}`);
    setError(null);
    try {
      const updated = await request<Document>(
        `/documents/${encodeURIComponent(document.id)}/usefulness`,
        json('PUT', { usefulness: value }),
      );
      const mergeRating = <T extends DocumentSummary,>(current: T): T => ({
        ...current,
        usefulness: updated.usefulness,
        metadata: { ...current.metadata, usefulness: updated.usefulness },
        updated_at: updated.updated_at,
      });
      setDocuments((items) => items.map((item) => item.id === document.id ? mergeRating(item) : item));
      setSelected((current) => current?.id === document.id ? mergeRating(current) : current);
    } catch (reason) {
      setError(reason);
    } finally {
      setBusyAction(null);
    }
  };

  const usefulnessControl = (document: DocumentSummary) => (
    <select
      className="paper-usefulness"
      aria-label={`Usefulness of ${document.title}`}
      title="Your assessment, separate from reading progress"
      value={document.usefulness ?? 'unrated'}
      disabled={operationBusy}
      onChange={(event) => {
        const option = USEFULNESS_OPTIONS.find((item) => item.value === event.target.value);
        if (option) void ratePaper(document, option.value);
      }}
    >
      {USEFULNESS_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
    </select>
  );

  const moveMany = async (ids: string[], target: string | null) => {
    if (!ids.length) return;
    let destination = target;
    let label = target ? folderName(target) : 'Unfiled';
    if (destination === NEW_FOLDER) {
      const name = window.prompt('Name the new folder')?.trim();
      if (!name) return;
      try {
        const folder = await request<PaperFolder>('/paper-folders', json('POST', { name }));
        setFolders((current) => [...current, folder].sort((a, b) => a.name.localeCompare(b.name)));
        destination = folder.id;
        label = folder.name;
      } catch (reason) {
        setError(reason);
        return;
      }
    }
    setBusyAction('move');
    setError(null);
    let moved = 0;
    try {
      for (const id of ids) {
        const updated = await request<Document>(`/documents/${encodeURIComponent(id)}/folder`, json('PUT', { folder_id: destination }));
        setDocuments((items) => items.map((item) => (item.id === updated.id ? updated : item)));
        setSelected((current) => (current?.id === updated.id ? updated : current));
        moved += 1;
      }
      setChecked(new Set());
      notify(`Moved ${moved} paper${moved === 1 ? '' : 's'} to ${label ?? 'the folder'}`);
    } catch (reason) {
      setError(reason);
    } finally {
      setBusyAction(null);
    }
  };

  const loadAssignmentTopics = async () => {
    setBusyAction('load-topics');
    setError(null);
    try {
      const map = await topicsApi.get(assignmentScope);
      setAssignmentTopics({ folderId: assignmentScope, map });
    } catch (reason) {
      setError(reason);
    } finally {
      setBusyAction(null);
    }
  };

  const assignSelectedTopic = async (topicId: string) => {
    const ids = [...checked];
    setBusyAction('assign-topic');
    setError(null);
    try {
      const map = await topicsApi.assign(assignmentScope, topicId, ids);
      setAssignmentTopics({ folderId: assignmentScope, map });
      setTopicsRevision((revision) => revision + 1);
      setChecked((current) => new Set([...current].filter((id) => !ids.includes(id))));
      notify(`Assigned ${ids.length} paper${ids.length === 1 ? '' : 's'} to topic`);
    } catch (reason) {
      setError(reason);
    } finally {
      setBusyAction(null);
    }
  };

  const deleteMany = async () => {
    const papers = checkedPapers.filter((paper) => paper.status !== 'processing');
    if (!papers.length || !window.confirm(`Delete ${papers.length} paper${papers.length === 1 ? '' : 's'} and all of their saved files? This cannot be undone.`)) return;
    setBusyAction('delete-many');
    setError(null);
    try {
      for (const paper of papers) await request<void>(`/documents/${encodeURIComponent(paper.id)}`, { method: 'DELETE' });
      setChecked(new Set());
      await load(null);
      notify(`Deleted ${papers.length} paper${papers.length === 1 ? '' : 's'}`);
    } catch (reason) {
      setError(reason);
      await load(null);
    } finally {
      setBusyAction(null);
    }
  };

  const dragIds = (id: string) => (checked.has(id) ? [...checked] : [id]);
  const dropProps = (target: string | null, key: string) => ({
    onDragOver: (event: React.DragEvent) => { if (event.dataTransfer.types.includes('application/x-scholarweave-papers')) { event.preventDefault(); setDropTarget(key); } },
    onDragLeave: () => setDropTarget((current) => (current === key ? null : current)),
    onDrop: (event: React.DragEvent) => {
      event.preventDefault();
      setDropTarget(null);
      const ids = JSON.parse(event.dataTransfer.getData('application/x-scholarweave-papers') || '[]') as string[];
      void moveMany(ids, target);
    },
  });

  const ocrErrorNotice = ocrBatch.error || ocrRefreshError ? <div className="stack-tight">
    <ErrorNotice error={ocrBatch.error || ocrRefreshError} />
    <button className="button secondary small" type="button" onClick={() => {
      setOcrRefreshError(null);
      void Promise.all([ocrBatch.refresh(), load()]).catch(setOcrRefreshError);
    }}>Retry OCR progress refresh</button>
  </div> : null;

  if (linkedPaper) {
    const paper = selected?.id === linkedPaper ? selected : null;
    const sequence = readerSequence ?? visibleDocuments;
    const index = sequence.findIndex((document) => document.id === linkedPaper);
    const previous = index > 0 ? sequence[index - 1] : null;
    const next = index >= 0 && index < sequence.length - 1 ? sequence[index + 1] : null;
    const pdfUrl = sourcePdf ? apiUrl(`/artifacts/${sourcePdf.id}/raw?sha256=${sourcePdf.sha256}`) : null;
    const readable = paper && !ingesting && pdfUrl;
    const togglePanel = (panel: 'notes' | 'details') => setSidePanel((current) => (current === panel ? null : panel));
    return (
      <div className="reader-page">
        <div className="reader-bar">
          <Link className="icon-button" to={basePath} aria-label="Back to papers" title="Back to papers">
            <Icon name="arrowRight" size={15} className="flip" />
          </Link>
          <div className="reader-title">
            <strong title={paper?.title}>{paper?.title ?? 'Opening paper…'}</strong>
            {paper ? (
              <small>
                {folderName(folderId(paper)) ?? 'Unfiled'}
                {paper.page_count ? ` · ${paper.page_count} pages` : ''}
                {paper.status !== 'ready' && !ingesting ? ' · not indexed yet' : ''}
              </small>
            ) : null}
          </div>
          {ingesting ? <span className="busy-label"><span className="button-spinner" aria-hidden="true" />Indexing…</span> : null}
          <span className="row-spacer" />
          {paper ? <label className="reader-usefulness"><span>Usefulness</span>{usefulnessControl(paper)}</label> : null}
          <button className="icon-button" type="button" aria-label="Previous paper" title={previous ? `Previous: ${previous.title}` : 'Previous paper'} disabled={!previous} onClick={() => previous && openPaper(previous.id)}>‹</button>
          <button className="icon-button" type="button" aria-label="Next paper" title={next ? `Next: ${next.title}` : 'Next paper'} disabled={!next} onClick={() => next && openPaper(next.id)}>›</button>
          {paper && (sourcePdf || extractedText) ? (
            <div className="segmented paper-view-switcher" role="group" aria-label="Paper view">
              {sourcePdf ? <button type="button" aria-pressed={viewerMode === 'pdf'} onClick={() => setViewerMode('pdf')}>PDF</button> : null}
              {openedSummary?.version && viewerMode === 'summary' ? <button type="button" aria-pressed="true">Summary</button> : null}
              <button
                type="button"
                aria-pressed={viewerMode === 'text'}
                disabled={!extractedText || busyAction === `text:${paper.id}`}
                title={extractedText ? 'Show the text retrieved during ingestion' : 'Index this paper to retrieve its text'}
                onClick={() => void showRetrievedText()}
              >
                {busyAction === `text:${paper.id}` ? 'Loading…' : 'Text'}
              </button>
            </div>
          ) : null}
          {paper ? (
            <button className="button small ask-button" type="button" title="Ask the assistant about this paper (Ctrl J)" onClick={() => ask.ask(undefined, { context: { document_id: paper.id, title: paper.title, page: viewerMode === 'pdf' ? readerPage : null } })}>
              <Icon name="sparkle" size={13} />Ask a question
            </button>
          ) : null}
          <button className="button secondary small" type="button" aria-pressed={sidePanel === 'notes'} onClick={() => togglePanel('notes')}>
            <Icon name="file" size={13} />Notes
          </button>
          <button
            className="icon-button settings-trigger"
            type="button"
            aria-label="Papers settings"
            title="Papers settings"
            aria-haspopup="dialog"
            aria-expanded={settingsOpen}
            aria-controls="paper-reader-settings"
            onClick={() => setSettingsOpen((value) => !value)}
          >
            <Icon name="settings" size={20} /><span>Settings</span>
          </button>
        </div>
        <SettingsDrawer id="paper-reader-settings" title="Paper settings" open={settingsOpen} onClose={() => setSettingsOpen(false)}>
          <div className="stack-tight">
            <button className="button secondary" type="button" aria-pressed={sidePanel === 'details'} onClick={() => { togglePanel('details'); setSettingsOpen(false); }}>
              Details
            </button>
            {paper ? (
              <button className="button secondary" type="button" aria-pressed={sidePanel === 'summary'} onClick={() => { setSidePanel((value) => value === 'summary' ? null : 'summary'); setSettingsOpen(false); }}>
                Summarize paper
              </button>
            ) : null}
            {paper ? (
              <>
                <button
                  className="button secondary"
                  type="button"
                  disabled={operationBusy || !ingestionOptions?.quick_ocr_available}
                  title={ingestionOptions?.quick_ocr_available
                    ? 'Fast local Indra extraction; reuses completed pages and leaves the batch engine unchanged.'
                    : 'Download the Indra CPU models first; see Document OCR settings.'}
                  onClick={() => { setSettingsOpen(false); void ingest(paper, 'quick'); }}
                >
                  <Icon name="scan" size={13} />Quick CPU OCR
                </button>
                <p className="field-hint">For quick reading. Verify equations, table values and reading order against the PDF.</p>
              </>
            ) : null}
          </div>
        </SettingsDrawer>
        {error ? <ErrorNotice error={error} /> : null}
        {ocrErrorNotice}
        {paper && Array.isArray(paper.metadata.ocr_indra_pages) && paper.metadata.ocr_indra_pages.length > 0 ? (
          <p className="notice info" role="status">
            Indra CPU extraction on pages {paper.metadata.ocr_indra_pages.join(', ')}.
            {' '}For quick reading only: check equations, table values and reading order against the PDF.
            {' '}Use Re-run with OCR for a full extraction with the configured engine.
          </p>
        ) : null}
        {downloadProgress}
        {paper && ingesting ? (
          <div className="notice info ingestion-progress" role="status" aria-live="polite">
            <div className="ingestion-progress-summary">
              <span className="spinner" aria-hidden="true" />
              <span>{ingestionStatus(paper)} {ingestionDetail(paper)}</span>
              <span className="row-spacer" />
              <button className="button danger small" type="button" disabled={busyAction === `stop:${paper.id}`} onClick={() => void stopIngestion(paper)}>
                <Icon name="stop" size={13} />{busyAction === `stop:${paper.id}` ? 'Stopping…' : 'Stop'}
              </button>
            </div>
            {pageProgress ? (
              <div className="page-progress">
                <div className="page-progress-label">
                  <strong>Pages completed</strong>
                  <span>{pageProgress.completed} / {pageProgress.total} ({pageProgress.percent}%)</span>
                </div>
                <progress aria-label="OCR pages completed" max={pageProgress.total} value={pageProgress.completed} />
              </div>
            ) : null}
          </div>
        ) : null}
        <div className={`reader-body${sidePanel ? ' with-side' : ''}`}>
          <section className="reader-main" aria-busy={ingesting}>
            {!paper && !error ? <Loading label="Opening paper…" /> : null}
            {readable && viewerMode === 'pdf' ? (
              <PdfReader
                key={`${paper.id}:${requestedPage ?? 'remembered'}`}
                url={pdfUrl}
                title={paper.title}
                initialPage={requestedPage ?? reading[paper.id]?.page ?? 1}
                onPageChange={(page, total) => { setReaderPage(page); recordReading(paper.id, page, total); }}
                onNotice={(message) => notify(message)}
                onQuote={(action, quote) => {
                  if (action === 'ask') {
                    ask.ask(`> ${quote.text.split('\n').join('\n> ')}\n\n`, { context: { document_id: paper.id, title: paper.title, page: quote.page } });
                    return;
                  }
                  paperNotes.append(quoteMarkdown(quote))
                    .then(() => notify('Added to notes', sidePanel === 'notes' ? undefined : { label: 'Show notes', run: () => setSidePanel('notes') }))
                    .catch((reason: unknown) => notify(reason instanceof Error ? reason.message : 'Could not add to notes'));
                }}
              />
            ) : null}
            {paper && viewerMode === 'summary' && openedSummary ? (
              <SummaryView documentId={paper.id} summary={openedSummary} closeLabel="Back to paper" onClose={() => { setViewerMode('pdf'); setOpenedSummary(null); }} />
            ) : null}
            {paper && viewerMode === 'text' && retrievedText !== null ? (
              <section className="retrieved-text-view" aria-label="Retrieved paper text">
                <MarkdownViewer content={retrievedText} imageSources={Object.fromEntries(
                  paper.artifacts.filter((artifact) =>
                    artifact.kind === 'extracted_image' && typeof artifact.metadata?.markdown_path === 'string',
                  ).map((artifact) => [
                    String(artifact.metadata.markdown_path),
                    apiUrl(`/artifacts/${artifact.id}/raw?sha256=${artifact.sha256}`),
                  ]),
                )} />
              </section>
            ) : null}
            {paper && viewerMode !== 'summary' ? (
              <ReaderAsk
                key={`ask-${paper.id}`}
                paper={paper}
                page={viewerMode === 'pdf' ? readerPage : null}
                folder={folders.find((folder) => folder.id === folderId(paper)) ?? null}
              />
            ) : null}
            {paper && !ingesting && !pdfUrl && viewerMode === 'pdf' ? (
              <EmptyState icon="papers" title="No PDF stored for this paper" description={extractedText ? 'Choose Text above to read its extracted content, or open Notes.' : 'Its notes are still available from Notes.'} />
            ) : null}
          </section>
          <PaperNotesDrawer paperNotes={paperNotes} open={sidePanel === 'notes'} onClose={() => setSidePanel(null)} />
          {sidePanel === 'summary' && paper ? <aside className="notes-drawer paper-details-drawer" aria-label="Paper summary">
            <button className="button small secondary" onClick={() => setSidePanel(null)}>Close summary tools</button>
            <h2>Summarize this paper</h2>
            <p>Get an explanation in chat, or generate a saved summary below. Saved summaries use the indexed paper and remain separate from targeted research briefs.</p>
            <button className="button" onClick={() => ask.ask('Summarize this paper: explain its motivation, approach, findings and limitations. Cite source evidence and disclose unread coverage.',
              { context: { document_id: paper.id, title: paper.title } })}>Quick summary in chat</button>
            <PaperSummaryPanel documentId={paper.id} ready={paper.status === 'ready' && paper.chunks.length > 0}
              onOpen={(summary) => { setOpenedSummary(summary); setViewerMode('summary'); }} />
          </aside> : null}
          {sidePanel === 'details' && paper ? (
            <aside className="notes-drawer paper-details-drawer" aria-label="Paper details">
              <header>
                <strong>Details</strong>
                <button type="button" className="icon-button" aria-label="Close details" onClick={() => setSidePanel(null)}><Icon name="close" size={13} /></button>
              </header>
              <div className="stack-tight">
                <div className="detail-line"><span>Status</span><StatusPill value={ingesting ? 'processing' : paper.status} /></div>
                <label className="detail-line">
                  <span>Folder</span>
                  <select aria-label="Paper folder" value={folderId(paper) ?? ''} disabled={operationBusy} onChange={(event) => void movePaper(paper, event.target.value || null)}>
                    <option value="">Unfiled papers</option>
                    {folders.map((folder) => <option value={folder.id} key={folder.id}>{folder.name}</option>)}
                  </select>
                </label>
                <div className="detail-line"><span>File</span><small>{paper.source_filename}</small></div>
                <div className="detail-line"><span>Acquired</span><time dateTime={paper.created_at}>{new Date(paper.created_at).toLocaleString()}</time></div>
                {pdfUrl ? <a className="button small secondary" href={pdfUrl} target="_blank" rel="noreferrer">Open PDF in a new tab</a> : null}
              </div>
              {!ingesting ? (
                <details className="library-details" key={paper.id} open={paper.status !== 'ready'}>
                  <summary>Extraction and indexing</summary>
                  <div className="button-row">
                    <button className={`button small${recommendedMode === 'embedded' ? '' : ' secondary'}`} type="button" disabled={operationBusy || ingestionOptions === null} onClick={() => void ingest(paper, 'embedded')}>
                      <Icon name="play" size={13} />{paper.status === 'ready' ? 'Re-index embedded text' : 'Use embedded text'}
                    </button>
                    <button
                      className={`button small${recommendedMode === 'ocr' ? '' : ' secondary'}`}
                      type="button"
                      title={ingestionOptions && !ingestionOptions.ocr_available ? `${ingestionOptions.ocr_engine} OCR runtime is unavailable` : 'Render every page and run OCR'}
                      disabled={operationBusy || ingestionOptions === null || !ingestionOptions.ocr_available}
                      onClick={() => void ingest(paper, 'ocr')}
                    >
                      <Icon name="scan" size={13} />{paper.status === 'ready' ? 'Re-run with OCR' : 'Run OCR'}
                    </button>
                  </div>
                  <div className="ingestion-recommendation">
                    {ingestionOptions ? (
                      <>
                        <strong>Suggested: {recommendedMode === 'embedded' ? 'Use embedded text' : 'Run OCR'}</strong>
                        <span>
                          Selectable text detected on {ingestionOptions.embedded_text_pages} of {ingestionOptions.total_pages} pages.
                          {ingestionOptions.ocr_engine === 'surya' ? ' Surya OCR preserves layout, Markdown, tables, equations and figures even when selectable text exists.' : ''}
                          {!ingestionOptions.ocr_available ? ` ${ingestionOptions.ocr_engine} OCR is currently unavailable.` : ''}
                        </span>
                      </>
                    ) : <span>Inspecting the PDF text layer…</span>}
                  </div>
                  {Array.isArray(paper.metadata.ocr_fallback_pages) && paper.metadata.ocr_fallback_pages.length > 0 ? (
                    <p role="status">OCR could not recover pages {paper.metadata.ocr_fallback_pages.join(', ')}. Embedded text was used where available; unreadable pages were skipped. See the extraction manifest for details.</p>
                  ) : null}
                  <div className="card-meta">
                    <span>{paper.page_count ?? '—'} pages</span>
                    <span>{paper.chunks.length} chunks</span>
                    <span>{paper.artifacts.length} artifacts</span>
                  </div>
                </details>
              ) : null}
              {!ingesting ? (
                <details className="paper-summary-tools" key={`summary:${paper.id}`}>
                  <summary>Saved summaries</summary>
                  <PaperSummaryPanel documentId={paper.id} ready={paper.status === 'ready' && paper.chunks.length > 0} onOpen={(summary) => { setOpenedSummary(summary); setViewerMode('summary'); }} />
                </details>
              ) : null}
              <button
                className="button danger small"
                type="button"
                aria-label={`Delete paper ${paper.title}`}
                disabled={operationBusy || paper.status === 'processing'}
                onClick={() => void deleteDocument(paper)}
              >
                <Icon name="trash" size={13} />{busyAction === `delete:${paper.id}` ? 'Deleting…' : 'Delete paper'}
              </button>
            </aside>
          ) : null}
        </div>
        <Toast toast={toast} onClose={() => setToast(null)} />
      </div>
    );
  }

  const recent = Object.entries(reading)
    .filter(([id]) => library.byId.has(id))
    .sort((a, b) => b[1].at - a[1].at)
    .slice(0, 4);
  const renderRow = (document: DocumentSummary, topicControl?: ReactNode) => {
      const hit = query.trim() ? searchResults.find((result) => result.id === document.id) : undefined;
      const progress = reading[document.id];
      const isChecked = checked.has(document.id);
      const abstract = document.abstract?.trim();
      const expanded = expandedAbstracts.has(document.id);
      const longAbstract = !!abstract && abstract.length > ABSTRACT_PREVIEW_LENGTH;
      return (
        <div
          className={`paper-table-row${showFolderColumn ? ' with-folder' : ''}${isChecked ? ' checked' : ''}`}
          key={document.id}
          draggable
          onDragStart={(event) => {
            event.dataTransfer.setData('application/x-scholarweave-papers', JSON.stringify(dragIds(document.id)));
            event.dataTransfer.effectAllowed = 'move';
          }}
        >
          <input type="checkbox" aria-label={`Select ${document.title}`} checked={isChecked} onChange={() => toggleChecked(document.id)} />
          <div className="paper-row-content">
            <button type="button" className="paper-row-main" onClick={() => openPaper(document.id)}>
              <strong>{document.title}</strong>
            </button>
            <p className={`paper-abstract${abstract ? '' : ' muted'}`} id={`paper-abstract-${document.id}`}>
              {abstract ? <><span className="paper-abstract-label">Abstract: </span>
                {longAbstract && !expanded ? `${abstract.slice(0, ABSTRACT_PREVIEW_LENGTH).trimEnd()}…` : abstract}
              </> : 'No abstract available.'}
            </p>
            <div className="paper-row-meta">
              <small>
                {document.status !== 'ready' ? <StatusPill value={document.status} /> : null}
                {document.page_count ? `${document.page_count} pages · ` : ''}{document.source_filename}
              </small>
              <small>Acquired <time dateTime={document.created_at}>{new Date(document.created_at).toLocaleString()}</time></small>
              {longAbstract ? <button type="button" className="paper-abstract-toggle"
                aria-expanded={expanded} aria-controls={`paper-abstract-${document.id}`}
                aria-label={`${expanded ? 'Show less abstract' : 'Read full abstract'} for ${document.title}`}
                onClick={() => setExpandedAbstracts((current) => {
                  const next = new Set(current);
                  if (next.has(document.id)) next.delete(document.id); else next.add(document.id);
                  return next;
                })}>{expanded ? 'Show less' : 'Read full abstract'}</button> : null}
            </div>
            {topicControl}
            {hit ? <small className="paper-search-excerpt">{hit.citation ? `${hit.citation}: ` : 'Title match: '}{hit.excerpt}</small> : null}
          </div>
          {showFolderColumn ? <small className="paper-table-folder">{folderName(folderId(document)) ?? '—'}</small> : null}
          <span className="paper-table-read">
            <ReadingProgress entry={progress} title={document.title} />
          </span>
          {usefulnessControl(document)}
        </div>
      );
  };

  const canTopics = topicScope !== undefined && folderDocuments.length > 0;
  const topicView = topicLayout && canTopics && !query.trim() && sortedDocuments.length > 0;
  const readyCount = documents.filter((document) => document.status === 'ready').length;

  return (
    <div className="page papers-home">
      <aside className="papers-actions" aria-label="Library actions">
        <button className="button" type="button" aria-expanded={showImport} onClick={() => setShowImport((value) => !value)}>
          <Icon name="plus" size={15} />Add paper
        </button>
        <button className="button secondary" type="button" aria-expanded={showOcrBatch}
          onClick={() => setShowOcrBatch((value) => !value)}>
          {ocrBatch.status === 'running' || ocrBatch.status === 'stopping' ? 'OCR batch progress' : 'Batch OCR...'}
        </button>
        {recent.length && !query.trim() ? (
          <section className="continue-reading" aria-label="Continue reading">
            <h2>Continue reading</h2>
            <div className="continue-grid">
              {recent.map(([id, entry]) => {
                const paper = library.byId.get(id)!;
                return (
                  <button type="button" className="continue-card" key={id} onClick={() => openPaper(id)}>
                    <strong>{paper.title}</strong>
                    <small>Page {entry.page}{entry.pages > 0 ? ` of ${entry.pages}` : ''}</small>
                    <ReadingProgress entry={entry} title={paper.title} />
                  </button>
                );
              })}
            </div>
          </section>
        ) : null}
      </aside>
      <div className="papers-browser">
      {error ? <ErrorNotice error={error} /> : null}
      {ocrErrorNotice}
      {downloadProgress}
      {showImport ? (
        <div className="paper-import" role="region" aria-label="Add a paper">
          <label className="button secondary upload-button">
            <Icon name="upload" size={15} />
            {busyAction === 'upload' ? 'Uploading…' : 'Upload PDF'}
            <input type="file" accept=".pdf,application/pdf" disabled={operationBusy} onChange={(event) => event.target.files?.[0] && void upload(event.target.files[0])} />
          </label>
          <span className="muted">or</span>
          <form className="source-form" onSubmit={(event) => { event.preventDefault(); void downloadPdf(); }}>
            <input aria-label="PDF URL" placeholder="Paste a PDF link, e.g. https://arxiv.org/pdf/…" type="url" value={remotePdfUrl} disabled={operationBusy} onChange={(event) => setRemotePdfUrl(event.target.value)} />
            <input aria-label="Paper title" placeholder="Title (optional)" value={remotePdfTitle} disabled={operationBusy} onChange={(event) => setRemotePdfTitle(event.target.value)} />
            <button className="button" type="submit" disabled={operationBusy || !remotePdfUrl.trim()}>
              <Icon name="download" size={15} />{downloadStartedAt !== null ? 'Downloading…' : 'Download PDF'}
            </button>
          </form>
          <IconButton icon="close" label="Close add paper" size={13} onClick={() => setShowImport(false)} />
        </div>
      ) : null}
      {showImport ? <ArxivSearch actionLabel="Use PDF link" disabled={operationBusy} onChoose={(paper) => {
        setRemotePdfUrl(`https://arxiv.org/pdf/${paper.arxiv_id}`);
        setRemotePdfTitle(paper.title);
        setToast({ message: 'PDF link filled in. Click Download PDF above to add this paper to your library.' });
      }} /> : null}
      {showSummaryBatch ? (
        <section className="summary-batch-shell" aria-label="Summarize papers">
          <div className="summary-batch-head">
            <button className="button small secondary" type="button" onClick={() => setShowSummaryBatch(false)}>
              <Icon name="arrowRight" size={13} className="flip" />Back to papers
            </button>
            <strong>Summarize papers</strong>
          </div>
          <SummaryBatchPanel papers={checkedPapers.length ? checkedPapers : query.trim() ? visibleDocuments : filteredDocuments} />
        </section>
      ) : null}
      {showOcrBatch ? <section className="summary-batch-shell" aria-label="OCR papers">
        <div className="summary-batch-head">
          <button className="button small secondary" type="button" onClick={() => setShowOcrBatch(false)}>Close OCR tools</button>
        </div>
        <OcrBatchPanel papers={checkedPapers.length ? checkedPapers : query.trim() ? visibleDocuments : filteredDocuments}
          disabled={operationBusy} />
      </section> : null}

      <header className="papers-toolbar papers-library-controls" aria-label="Paper controls">
      <nav className="folder-chips" aria-label="Paper folders">
        {[
          { id: 'all', name: 'All papers', count: documents.length, target: undefined },
          { id: 'unfiled', name: 'Unfiled', count: library.unfiled.length, target: null },
        ].map((chip) => (
          <button
            type="button"
            key={chip.id}
            className={`folder-chip${activeFolder === chip.id ? ' active' : ''}${dropTarget === chip.id ? ' drop' : ''}`}
            aria-current={activeFolder === chip.id ? 'true' : undefined}
            onClick={() => chooseFolder(chip.id)}
            {...(chip.target === null ? dropProps(null, chip.id) : {})}
          >
            {chip.name}<small>{chip.count}</small>
          </button>
        ))}
        <div className="collection-tree" role="group" aria-label="Collection hierarchy">
        {collectionTree(folders).map((folder) => (
          <span className={`folder-chip-wrap${dropTarget === folder.id ? ' drop' : ''}`} style={{ marginInlineStart: `${folder.depth * 1.1}rem` }} key={folder.id} {...dropProps(folder.id, folder.id)}>
            <button
              type="button"
              className={`folder-chip${activeFolder === folder.id ? ' active' : ''}`}
              aria-current={activeFolder === folder.id ? 'true' : undefined}
              aria-label={folder.name}
              onClick={() => chooseFolder(folder.id)}
            >
              <Icon name="papers" size={12} />{folder.label}<small>{library.byFolder.get(folder.id)?.length ?? 0}</small>
            </button>
            <IconButton icon="close" tone="danger" rowAction size={11} label={`Delete folder ${folder.name}`} disabled={operationBusy} onClick={() => void deleteFolder(folder)} />
          </span>
        ))}
        </div>
        <form className="paper-folder-form" onSubmit={(event) => { event.preventDefault(); void createFolder(); }}>
          <input aria-label="New paper folder" placeholder="+ Collection (Parent/Child)" title="Use Parent/Child for nested collections. Membership never moves paper files." value={newFolderName} disabled={operationBusy} onChange={(event) => setNewFolderName(event.target.value)} />
          {newFolderName.trim() ? <button className="button small secondary" type="submit" disabled={operationBusy}>Add</button> : null}
        </form>
      </nav>

      <div className="library-filter-row">
        <input type="search" aria-label="Search papers" placeholder="Search titles and full text" value={query} maxLength={1000} onChange={(event) => { setQuery(event.target.value); setOffset(0); }} />
        <select aria-label="Paper status" value={paperStatus} onChange={(event) => { setPaperStatus(event.target.value); setOffset(0); }}>
          <option value="">All statuses</option>
          <option value="ready">Ready to read</option>
          <option value="uploaded">Not yet prepared</option>
          <option value="processing">Preparing</option>
          <option value="failed">Needs attention</option>
        </select>
        <select aria-label="Filter by usefulness" value={usefulness} onChange={(event) => { setUsefulness(event.target.value); setOffset(0); }}>
          <option value="">All usefulness labels</option>
          {USEFULNESS_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
        </select>
        <select aria-label="Sort papers" value={query.trim() ? 'relevance' : sort} disabled={!!query.trim()} onChange={(event) => { setSort(event.target.value); setOffset(0); }}>
          {query.trim() ? <option value="relevance">Relevance</option> : null}
          <option value="newest">Acquired: newest first</option>
          <option value="oldest">Acquired: oldest first</option>
          <option value="title">Title A-Z</option>
        </select>
        {paperStatus ? <span className="muted" role="status">Status: {PAPER_STATUS_LABELS[paperStatus] ?? paperStatus}</span> : null}
        {usefulness ? <span className="muted" role="status">Usefulness: {USEFULNESS_OPTIONS.find((option) => option.value === usefulness)?.label}</span> : null}
        {query || paperStatus || usefulness ? (
          <button className="button ghost small" type="button" onClick={() => { setQuery(''); setPaperStatus(''); setUsefulness(''); setOffset(0); }}>Clear</button>
        ) : null}
      </div>
      <div className="papers-view-controls">
        <small className="muted">
          {documents.length} paper{documents.length === 1 ? '' : 's'}
          {documents.length && readyCount < documents.length ? ` · ${documents.length - readyCount} not indexed yet` : ''}
        </small>
        <label className="papers-folder-toggle">
          <input type="checkbox" checked={showFolderColumn} onChange={(event) => {
            setShowFolderColumn(event.target.checked);
            localStorage.setItem('scholarweave.papersFolderColumn', String(event.target.checked));
          }} />
          Show folder column
        </label>
        <div className="papers-view-actions">
          {canTopics ? (
            <div className="segmented" role="group" aria-label="Paper layout">
              {(['list', 'topics'] as const).map((layout) => (
                <button type="button" key={layout} aria-pressed={(layout === 'topics') === topicLayout} disabled={!!query.trim()} title={layout === 'topics' ? 'Group papers into topics' : 'Show a flat list'} onClick={() => { setTopicLayout(layout === 'topics'); localStorage.setItem('scholarweave.papersLayout', layout); }}>
                  {layout === 'list' ? 'List' : 'Topics'}
                </button>
              ))}
            </div>
          ) : null}
          <button className="button secondary" type="button" aria-expanded={showSummaryBatch} onClick={() => setShowSummaryBatch((value) => !value)}>
            Summarize…
          </button>
        </div>
      </div>
      </header>

      {checked.size ? (
        <div className="bulk-bar" role="toolbar" aria-label="Selected papers">
          <strong>{checked.size} selected</strong>
          <select
            aria-label="Move selected papers"
            value=""
            disabled={operationBusy}
            onChange={(event) => { const value = event.target.value; if (value) void moveMany([...checked], value === 'unfiled' ? null : value); }}
          >
            <option value="">Move to…</option>
            <option value={NEW_FOLDER}>New folder…</option>
            <option value="unfiled">Unfiled</option>
            {folders.map((folder) => <option value={folder.id} key={folder.id}>{folder.name}</option>)}
          </select>
          {assignmentMap?.topics.length ? (
            <select aria-label="Assign selected papers to topic" value="" disabled={operationBusy}
              onChange={(event) => { if (event.target.value) void assignSelectedTopic(event.target.value); }}>
              <option value="">{busyAction === 'assign-topic' ? 'Assigning topic…' : 'Assign topic…'}</option>
              {assignmentMap.topics.map((topic) => <option value={topic.id} key={topic.id}>{topic.name}</option>)}
            </select>
          ) : (
            <button className="button small secondary" type="button" disabled={operationBusy}
              onClick={() => void loadAssignmentTopics()}>
              {busyAction === 'load-topics' ? 'Loading topics…' : 'Assign topic…'}
            </button>
          )}
          {assignmentMap === null || assignmentMap?.topics.length === 0 ? (
            <small className="muted" role="status">No saved topics for this view. Create topics in All papers or this folder first.</small>
          ) : null}
          <button className="button small secondary" type="button" onClick={() => setShowSummaryBatch(true)}>Summarize</button>
          <button className="button small secondary" type="button" onClick={() => setShowOcrBatch(true)}>OCR non-OCR papers</button>
          <button className="button small secondary" type="button" onClick={() => ask.ask(`Compare these papers: ${checkedPapers.map((paper) => `"${paper.title}" (paper ID: ${paper.id})`).join('; ')}. Read their existing text and notes, and contrast their questions, methods, evidence and limitations.`)}>
            <Icon name="sparkle" size={13} />Ask about these
          </button>
          <button className="button small danger" type="button" disabled={operationBusy} onClick={() => void deleteMany()}>
            <Icon name="trash" size={13} />Delete
          </button>
          <span className="row-spacer" />
          <button className="button ghost small" type="button" onClick={() => setChecked(new Set())}>Clear selection</button>
        </div>
      ) : null}

      {topicView ? <PaperTopicsView key={topicScope ?? 'all'} folderId={topicScope ?? null} revision={topicsRevision} papers={sortedDocuments} renderRow={renderRow} /> : (
      <div className="paper-table" aria-label="Paper results" aria-busy={searching}>
        {visibleDocuments.length ? (
          <div className={`paper-table-head${showFolderColumn ? ' with-folder' : ''}`}>
            <input
              type="checkbox"
              aria-label="Select all shown papers"
              checked={allVisibleChecked}
              onChange={() => setChecked((current) => {
                const next = new Set(current);
                for (const document of visibleDocuments) { if (allVisibleChecked) next.delete(document.id); else next.add(document.id); }
                return next;
              })}
            />
            <span>Title</span>
            {showFolderColumn ? <span>Folder</span> : null}
            <span>Read</span>
            <span>Usefulness</span>
          </div>
        ) : null}
        {searching ? <Loading label="Searching papers…" /> : null}
        {visibleDocuments.map((document) => renderRow(document))}
        {!documents.length ? (
          <EmptyState icon="papers" title="Your library is empty" description="Add a PDF to start reading, annotating and asking questions about it." />
        ) : null}
        {!!documents.length && !visibleDocuments.length && !searching ? (
          <p className="muted">{query.trim() || paperStatus || usefulness ? 'No matching papers. Try fewer terms or change the folder, status or usefulness filter.' : 'No papers in this folder yet. Drag papers onto a folder to file them.'}</p>
        ) : null}
      </div>
      )}
      {!topicView && (offset > 0 || hasNextPage) ? (
        <div className="library-pagination">
          <button className="button secondary small" type="button" disabled={offset === 0 || searching} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>Previous</button>
          <small>Page {Math.floor(offset / PAGE_SIZE) + 1}{!query.trim() ? ` of ${Math.max(1, Math.ceil(sortedDocuments.length / PAGE_SIZE))}` : ''}</small>
          <button className="button secondary small" type="button" disabled={!hasNextPage || searching} onClick={() => setOffset(offset + PAGE_SIZE)}>Next</button>
        </div>
      ) : null}
      <Toast toast={toast} onClose={() => setToast(null)} />
      </div>
    </div>
  );
}

function Toast({ toast, onClose }: { toast: { message: string; action?: { label: string; run: () => void } } | null; onClose: () => void }) {
  if (!toast) return null;
  return (
    <div className="toast" role="status">
      <span>{toast.message}</span>
      {toast.action ? <button type="button" className="button ghost small" onClick={() => { toast.action?.run(); onClose(); }}>{toast.action.label}</button> : null}
      <button type="button" className="icon-button" aria-label="Dismiss" onClick={onClose}><Icon name="close" size={12} /></button>
    </div>
  );
}
