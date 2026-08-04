import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from '../lib/router';
import { useConfirm } from '../components/common/ConfirmDialog';
import { EmptyState } from '../components/common/EmptyState';
import { ErrorNotice } from '../components/common/ErrorNotice';
import { Icon } from '../components/common/Icon';
import { SkeletonList } from '../components/common/Skeleton';
import { StatusBadge } from '../components/common/StatusBadge';
import { toMessage, useToast } from '../components/common/Toast';
import { WorkflowPicker, type WorkflowChoice } from '../components/workflow/WorkflowPicker';
import { api } from '../lib/api';
import { formatBytes, formatDateTime } from '../lib/format';
import { workflowEditorPath } from '../lib/workflowRoutes';
import type { ArtifactContentResponse, DocumentResponse } from '../types/api';

const PAGE_PREVIEW_COUNT = 8;

function metadataRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function numberSet(value: unknown): Set<number> {
  return new Set(Array.isArray(value) ? value.filter((item): item is number => typeof item === 'number') : []);
}

export function PapersPage() {
  const { documentId } = useParams();
  const navigate = useNavigate();
  const toast = useToast();
  const confirm = useConfirm();
  const fileInput = useRef<HTMLInputElement | null>(null);

  const [documents, setDocuments] = useState<DocumentResponse[]>([]);
  const [selectedDocument, setSelectedDocument] = useState<DocumentResponse | null>(null);
  const [artifactContent, setArtifactContent] = useState<ArtifactContentResponse | null>(null);
  const [title, setTitle] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [filter, setFilter] = useState('');
  const [busy, setBusy] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [pageFilter, setPageFilter] = useState<'all' | 'attention'>('all');
  const [showAllPages, setShowAllPages] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);

  const refreshDocuments = async (selectedId?: string) => {
    const list = await api.listDocuments();
    setDocuments(list);
    const nextId = selectedId || documentId || list[0]?.id;
    if (nextId) {
      setSelectedDocument(await api.getDocument(nextId));
    } else {
      setSelectedDocument(null);
    }
  };

  useEffect(() => {
    let active = true;
    setLoading(true);
    refreshDocuments()
      .then(() => active && setError(''))
      .catch((err) => active && setError(toMessage(err, 'Failed to load documents')))
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, [documentId]);

  const selected = useMemo(
    () => documents.find((document) => document.id === selectedDocument?.id) ?? selectedDocument,
    [documents, selectedDocument],
  );

  useEffect(() => {
    setPageFilter('all');
    setShowAllPages(false);
  }, [selected?.id]);

  const visibleDocuments = useMemo(() => {
    const term = filter.trim().toLowerCase();
    if (!term) return documents;
    return documents.filter((document) => `${document.title} ${document.source_filename}`.toLowerCase().includes(term));
  }, [documents, filter]);

  const chooseFile = (next: File | null) => {
    if (next && next.type !== 'application/pdf' && !next.name.toLowerCase().endsWith('.pdf')) {
      toast.failure('That file is not a PDF', 'Only PDF documents can be ingested.');
      return;
    }
    setFile(next);
  };

  const submitUpload = async () => {
    if (!file) {
      toast.failure('Choose a PDF first', 'Drop a file into the upload area or browse for one.');
      return;
    }
    setBusy('upload');
    setError('');
    try {
      const created = await api.uploadDocument(file, title || undefined);
      await refreshDocuments(created.id);
      navigate(`/papers/${created.id}`);
      toast.success(`Uploaded ${created.title}`, 'Run ingestion to extract its text and figures.');
      setFile(null);
      setTitle('');
      if (fileInput.current) fileInput.current.value = '';
    } catch (err) {
      const message = toMessage(err, 'Upload failed');
      setError(message);
      toast.failure('Upload failed', message);
    } finally {
      setBusy('');
    }
  };

  const ingest = async (id: string) => {
    setBusy(`ingest:${id}`);
    setError('');
    try {
      const detail = await api.ingestDocument(id);
      await refreshDocuments(id);
      setSelectedDocument(detail);
      toast.success(`Ingested ${detail.title}`, `${detail.chunks.length} chunks are ready to query.`);
    } catch (err) {
      const message = toMessage(err, 'Ingest failed');
      setError(message);
      toast.failure('Ingest failed', message);
    } finally {
      setBusy('');
    }
  };

  const deleteDocument = async (document: DocumentResponse) => {
    const confirmed = await confirm({
      title: `Delete “${document.title}”?`,
      description: 'The PDF and every artifact extracted from it will be removed from local storage. This cannot be undone.',
      confirmLabel: 'Delete paper',
    });
    if (!confirmed) return;
    setBusy(`delete:${document.id}`);
    setError('');
    try {
      await api.deleteDocument(document.id);
      const list = await api.listDocuments();
      setDocuments(list);
      setArtifactContent(null);
      const next = list[0] ?? null;
      setSelectedDocument(next);
      navigate(next ? `/papers/${next.id}` : '/papers');
      toast.success(`Deleted ${document.title}`);
    } catch (err) {
      const message = toMessage(err, 'Delete failed');
      setError(message);
      toast.failure('Delete failed', message);
    } finally {
      setBusy('');
    }
  };

  const enhancePage = async (document: DocumentResponse, pageNumber: number) => {
    setBusy(`enhance:${pageNumber}`);
    setError('');
    try {
      const templates = await api.listWorkflowTemplates();
      const workflow = templates.find((template) => template.name === 'Enhance OCR page');
      if (!workflow) {
        throw new Error('The page enhancement agent is unavailable.');
      }
      const run = await api.createRun({ workflow, inputs: { document_id: document.id, page_number: pageNumber } });
      navigate(`/runs/${run.id}`);
    } catch (err) {
      const message = toMessage(err, 'Failed to start page enhancement');
      setError(message);
      toast.failure('Could not start the rewrite', message);
      setBusy('');
    }
  };

  const enhancedPages = numberSet(selected?.metadata.llm_enhanced_pages);
  const validatedPages = numberSet(selected?.metadata.llm_validated_pages);
  const enhancementNotes = metadataRecord(selected?.metadata.llm_enhancement_notes);
  const validationStatuses = metadataRecord(selected?.metadata.llm_validation_statuses);
  const qualityRatings = metadataRecord(selected?.metadata.ocr_quality_ratings);
  const qualityIssues = metadataRecord(selected?.metadata.ocr_quality_issues);

  const pageRows = useMemo(() => {
    if (!selected?.page_count) return [];
    return Array.from({ length: selected.page_count }, (_, index) => index + 1).map((pageNumber) => {
      const note = enhancementNotes[String(pageNumber)];
      const isEnhanced = enhancedPages.has(pageNumber);
      const isValidated = validatedPages.has(pageNumber);
      const validationStatus = validationStatuses[String(pageNumber)];
      const quality = qualityRatings[String(pageNumber)];
      const issues = qualityIssues[String(pageNumber)];
      const issueText = Array.isArray(issues) ? issues.filter((issue): issue is string => typeof issue === 'string').join(' ') : '';
      const statusText = isValidated
        ? 'The rewritten page passed validation'
        : validationStatus === 'failed'
          ? typeof note === 'string'
            ? note
            : 'Validation rejected the rewrite; OCR text was retained'
          : quality === 'good'
            ? 'OCR quality is good; the original OCR text was kept'
            : quality === 'average'
              ? `OCR quality is average; the original OCR text was kept.${issueText ? ` ${issueText}` : ''}`
              : isEnhanced
                ? 'LLM enhancement applied (legacy page without validation)'
                : typeof note === 'string'
                  ? note
                  : 'OCR text available';
      const needsAttention = quality === 'poor' || quality === 'average' || validationStatus === 'failed';
      return {
        pageNumber,
        isEnhanced,
        isValidated,
        statusText,
        needsAttention,
        qualityLabel: typeof quality === 'string' ? quality : null,
        qualityTone: quality === 'good' ? 'success' : quality === 'average' ? 'warning' : quality === 'poor' ? 'danger' : 'muted',
      };
    });
  }, [enhancedPages, enhancementNotes, qualityIssues, qualityRatings, selected, validatedPages, validationStatuses]);

  const attentionCount = pageRows.filter((row) => row.needsAttention).length;
  const filteredPages = pageFilter === 'attention' ? pageRows.filter((row) => row.needsAttention) : pageRows;
  const visiblePages = showAllPages ? filteredPages : filteredPages.slice(0, PAGE_PREVIEW_COUNT);

  return (
    <div className="page-stack page-grid-two">
      <section className="panel stack gap-lg">
        <section className="stack gap-sm">
          <div className="panel-subheader">
            <h3>Add a paper</h3>
          </div>
          <div
            className={`dropzone${dragging ? ' dragging' : ''}${file ? ' has-file' : ''}`}
            role="button"
            tabIndex={0}
            onClick={() => fileInput.current?.click()}
            onKeyDown={(event) => {
              if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                fileInput.current?.click();
              }
            }}
            onDragOver={(event) => {
              event.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(event) => {
              event.preventDefault();
              setDragging(false);
              chooseFile(event.dataTransfer.files?.[0] ?? null);
            }}
          >
            <Icon name={file ? 'file' : 'upload'} size={20} />
            {file ? (
              <>
                <strong>{file.name}</strong>
                <span>{formatBytes(file.size)} · click to choose a different file</span>
              </>
            ) : (
              <>
                <strong>Drop a PDF here</strong>
                <span>or click to browse your files</span>
              </>
            )}
            <input
              ref={fileInput}
              type="file"
              accept="application/pdf"
              onChange={(event) => chooseFile(event.target.files?.[0] ?? null)}
            />
          </div>
          <div className="field-stack">
            <label className="field-label" htmlFor="paper-title">
              Title
            </label>
            <input
              id="paper-title"
              className="input"
              value={title}
              placeholder={file ? file.name.replace(/\.pdf$/i, '') : 'Taken from the PDF if left blank'}
              onChange={(event) => setTitle(event.target.value)}
            />
          </div>
          <button type="button" className="button primary block" disabled={busy === 'upload' || !file} onClick={submitUpload}>
            <Icon name="upload" size={14} />
            {busy === 'upload' ? 'Uploading…' : 'Upload document'}
          </button>
        </section>

        <section className="stack gap-sm">
          <div className="panel-subheader">
            <h3>Library</h3>
            <button type="button" className="button subtle icon-only sm" title="Refresh list" aria-label="Refresh list" onClick={() => void refreshDocuments(selected?.id)}>
              <Icon name="refresh" size={14} />
            </button>
          </div>
          {documents.length > 4 ? (
            <label className="search-field">
              <Icon name="search" size={13} />
              <input value={filter} placeholder="Filter papers" onChange={(event) => setFilter(event.target.value)} />
              {filter ? (
                <button type="button" aria-label="Clear filter" onClick={() => setFilter('')}>
                  <Icon name="close" size={12} />
                </button>
              ) : null}
            </label>
          ) : null}
          {error ? <ErrorNotice message={error} /> : null}
          {loading ? <SkeletonList rows={3} /> : null}
          <div className="stack gap-sm scroll-area">
            {visibleDocuments.map((document) => (
              <button
                type="button"
                key={document.id}
                className={`list-item ${selected?.id === document.id ? 'active' : ''}`}
                onClick={async () => {
                  navigate(`/papers/${document.id}`);
                  setSelectedDocument(await api.getDocument(document.id));
                  setArtifactContent(null);
                }}
              >
                <div className="list-item-main">
                  <strong className="truncate">{document.title}</strong>
                  <p className="truncate">{document.source_filename}</p>
                </div>
                <StatusBadge status={document.status} />
              </button>
            ))}
            {!loading && documents.length === 0 ? (
              <EmptyState icon="papers" title="No papers uploaded" description="Drop a PDF above to get started." />
            ) : null}
            {!loading && documents.length > 0 && visibleDocuments.length === 0 ? (
              <p className="empty-state">No papers matched “{filter}”.</p>
            ) : null}
          </div>
        </section>
      </section>

      <section className="stack gap-lg">
        <section className="panel">
          <div className="panel-header">
            <div>
              <p className="eyebrow">Document detail</p>
              <h2>{selected?.title || 'Select a paper'}</h2>
            </div>
            {selected ? <StatusBadge status={selected.status} /> : null}
          </div>
          {selected ? (
            <div className="stack gap-md">
              {selected.status !== 'ready' ? (
                <div className="info-banner">
                  <Icon name="info" size={15} />
                  <div className="notice-body">
                    <strong>This paper has not been ingested yet</strong>
                    <p>Run ingestion to extract text, tables, and figures before using it with an agent.</p>
                  </div>
                </div>
              ) : null}
              <div className="button-row wrap">
                <button
                  type="button"
                  className={selected.status === 'ready' ? 'button' : 'button primary'}
                  disabled={busy === `ingest:${selected.id}`}
                  onClick={() => ingest(selected.id)}
                >
                  <Icon name="sparkle" size={14} />
                  {busy === `ingest:${selected.id}` ? 'Ingesting…' : selected.status === 'ready' ? 'Re-ingest' : 'Ingest document'}
                </button>
                <button
                  type="button"
                  className="button"
                  onClick={() => setPickerOpen(true)}
                >
                  <Icon name="workflow" size={14} />
                  Use this paper with an agent
                </button>
                <button
                  type="button"
                  className="button danger"
                  disabled={busy === `delete:${selected.id}`}
                  onClick={() => void deleteDocument(selected)}
                >
                  <Icon name="trash" size={14} />
                  {busy === `delete:${selected.id}` ? 'Deleting…' : 'Delete'}
                </button>
              </div>
              <dl className="definition-grid">
                <div>
                  <dt>Pages</dt>
                  <dd>{selected.page_count ?? '—'}</dd>
                </div>
                <div>
                  <dt>Chunks</dt>
                  <dd>{selected.chunks.length}</dd>
                </div>
                <div>
                  <dt>Artifacts</dt>
                  <dd>{selected.artifacts.length}</dd>
                </div>
                <div>
                  <dt>Created</dt>
                  <dd>{formatDateTime(selected.created_at)}</dd>
                </div>
                <div>
                  <dt>Updated</dt>
                  <dd>{formatDateTime(selected.updated_at)}</dd>
                </div>
              </dl>
            </div>
          ) : (
            <EmptyState
              icon="papers"
              title="Nothing selected"
              description="Pick a paper from the library to review its artifacts, chunks, and per-page OCR quality."
            />
          )}
        </section>

        {selected?.page_count ? (
          <section className="panel">
            <div className="panel-subheader">
              <div>
                <h3>Page quality</h3>
                <p className="muted-text small">
                  OCR quality is triaged per page; only poor pages are rewritten by the large vision model. Force a rewrite of any page below.
                </p>
              </div>
            </div>
            {pageRows.length > PAGE_PREVIEW_COUNT || attentionCount > 0 ? (
              <div className="segmented-control compact" role="group" aria-label="Filter pages">
                <button type="button" className={pageFilter === 'all' ? 'active' : ''} onClick={() => setPageFilter('all')}>
                  All {pageRows.length}
                </button>
                <button
                  type="button"
                  className={pageFilter === 'attention' ? 'active' : ''}
                  disabled={attentionCount === 0}
                  onClick={() => setPageFilter('attention')}
                >
                  Needs attention {attentionCount}
                </button>
              </div>
            ) : null}
            <div className="page-enhancement-list">
              {visiblePages.map((row) => (
                <article className="page-enhancement-item" key={row.pageNumber}>
                  <div>
                    <span className="page-enhancement-head">
                      <strong>Page {row.pageNumber}</strong>
                      {row.qualityLabel ? <span className={`status-badge ${row.qualityTone}`}>{`OCR ${row.qualityLabel}`}</span> : null}
                      {row.isValidated ? <span className="status-badge success">Rewritten</span> : null}
                    </span>
                    <p className={row.isValidated || row.qualityTone === 'success' ? 'success-text' : 'muted-text'}>{row.statusText}</p>
                  </div>
                  <button
                    type="button"
                    className="button sm"
                    disabled={busy.startsWith('enhance:')}
                    onClick={() => void enhancePage(selected, row.pageNumber)}
                  >
                    <Icon name="sparkle" size={12} />
                    {busy === `enhance:${row.pageNumber}` ? 'Starting…' : row.isEnhanced ? 'Rewrite again' : 'Rewrite'}
                  </button>
                </article>
              ))}
              {filteredPages.length === 0 ? <p className="empty-state">No pages match this filter.</p> : null}
            </div>
            {filteredPages.length > PAGE_PREVIEW_COUNT ? (
              <button type="button" className="button subtle block" onClick={() => setShowAllPages((prev) => !prev)}>
                <Icon name={showAllPages ? 'chevronDown' : 'chevronRight'} size={13} />
                {showAllPages ? 'Show fewer pages' : `Show all ${filteredPages.length} pages`}
              </button>
            ) : null}
          </section>
        ) : null}

        <section className="panel">
          <div className="panel-subheader">
            <h3>Artifacts</h3>
            {selected?.artifacts.length ? <span className="tiny-tag">{selected.artifacts.length}</span> : null}
          </div>
          {!selected?.artifacts.length ? (
            <p className="empty-state">Artifacts appear here once ingestion completes.</p>
          ) : (
            <div className="stack gap-sm">
              {selected.artifacts.map((artifact) => (
                <button
                  key={artifact.id}
                  type="button"
                  className={`list-item ${artifactContent?.artifact.id === artifact.id ? 'active' : ''}`}
                  onClick={async () => setArtifactContent(await api.getArtifactContent(artifact.id))}
                >
                  <div className="list-item-main">
                    <strong>{artifact.kind}</strong>
                    <p className="truncate">{artifact.relative_path}</p>
                  </div>
                  <span className="tiny-tag">{formatBytes(artifact.size_bytes)}</span>
                </button>
              ))}
            </div>
          )}
          {artifactContent ? (
            <div className="preview-block" style={{ marginTop: '0.75rem' }}>
              <div className="preview-header">
                <strong>{artifactContent.artifact.kind}</strong>
                <span className="muted-text small">{artifactContent.artifact.media_type}</span>
              </div>
              <pre>
                {typeof artifactContent.content === 'string'
                  ? artifactContent.content
                  : JSON.stringify(artifactContent.content, null, 2)}
              </pre>
            </div>
          ) : null}
        </section>

        <section className="panel">
          <div className="panel-subheader">
            <h3>Chunks</h3>
            {selected?.chunks.length ? <span className="tiny-tag">{selected.chunks.length}</span> : null}
          </div>
          {!selected?.chunks.length ? (
            <p className="empty-state">No chunks available until ingest completes.</p>
          ) : (
            <div className="chunk-list">
              {selected.chunks.map((chunk) => (
                <article key={chunk.id} className="chunk-card">
                  <div className="chunk-card-header">
                    <strong>{chunk.section_title || `Chunk ${chunk.chunk_index + 1}`}</strong>
                    <span className="tiny-tag">{chunk.citation}</span>
                  </div>
                  <p className="muted-text small">
                    Pages {chunk.page_start}–{chunk.page_end}
                  </p>
                  <p>{chunk.text}</p>
                </article>
              ))}
            </div>
          )}
        </section>
      </section>

      <WorkflowPicker
        open={pickerOpen}
        onClose={() => setPickerOpen(false)}
        title="Run an agent with this paper"
        description={
          selected
            ? `“${selected.title}” will be filled in wherever the agent asks for a document.`
            : undefined
        }
        highlightInputKey="document_id"
        allowBlank
        onSelect={(choice: WorkflowChoice) =>
          navigate(workflowEditorPath(choice, { documentId: selected?.id }))
        }
      />
    </div>
  );
}
