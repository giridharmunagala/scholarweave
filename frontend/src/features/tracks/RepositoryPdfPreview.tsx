import { useEffect, useRef, useState } from 'react';
import type { PDFDocumentLoadingTask, PDFDocumentProxy, RenderTask } from 'pdfjs-dist';

export function RepositoryPdfPreview({ url, title }: { url: string; title: string }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [document, setDocument] = useState<PDFDocumentProxy | null>(null);
  const [page, setPage] = useState(1);
  const [zoom, setZoom] = useState(1);
  const [error, setError] = useState('');
  const [rendering, setRendering] = useState(false);
  const [text, setText] = useState<{ page: number; content: string } | null>(null);
  const [textError, setTextError] = useState('');
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState('');
  useEffect(() => {
    let active = true;
    let task: PDFDocumentLoadingTask | undefined;
    setDocument(null); setPage(1); setError(''); setText(null); setTextError(''); setDownloadError('');
    void Promise.all([import('pdfjs-dist'), import('pdfjs-dist/build/pdf.worker.min.mjs?url')])
      .then(([pdfjs, worker]) => {
        if (!active) return;
        pdfjs.GlobalWorkerOptions.workerSrc = worker.default;
        task = pdfjs.getDocument({ url, isEvalSupported: false, enableXfa: false });
        return task.promise;
      }).then((loaded) => { if (active && loaded) setDocument(loaded); })
      .catch((reason: unknown) => { if (active) setError(reason instanceof Error ? reason.message : String(reason)); });
    return () => { active = false; void task?.destroy(); };
  }, [url]);
  useEffect(() => {
    let active = true;
    setText(null); setTextError('');
    if (document) void document.getPage(page).then((pdfPage) => pdfPage.getTextContent())
      .then((content) => {
        if (active) setText({ page, content: content.items.map((item) =>
          'str' in item ? `${item.str}${item.hasEOL ? '\n' : ' '}` : '').join('').trim() });
      }).catch((reason: unknown) => {
        if (active) setTextError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => { active = false; };
  }, [document, page]);
  useEffect(() => {
    if (!document || !canvas.current) return;
    let active = true;
    let task: RenderTask | undefined;
    setRendering(true);
    void document.getPage(page).then(async (pdfPage) => {
      if (!active || !canvas.current) return;
      const viewport = pdfPage.getViewport({ scale: zoom });
      const target = canvas.current;
      const context = target.getContext('2d');
      if (!context) throw new Error('Canvas rendering is unavailable.');
      // Limit canvas allocation even for an unusually large page.
      if (viewport.width * viewport.height > 16_000_000) throw new Error('PDF page is too large at this zoom. Reduce zoom.');
      target.width = Math.ceil(viewport.width); target.height = Math.ceil(viewport.height);
      task = pdfPage.render({ canvasContext: context, viewport });
      await task.promise;
      if (active) setError('');
    }).catch((reason: unknown) => {
      if (active) setError(reason instanceof Error ? reason.message : String(reason));
    }).finally(() => { if (active) setRendering(false); });
    return () => { active = false; task?.cancel(); };
  }, [document, page, zoom]);
  const downloadOriginal = async () => {
    setDownloading(true); setDownloadError('');
    try {
      const response = await fetch(url);
      if (!response.ok) throw new Error(`Original PDF unavailable (${response.status}). The file may have changed or access may have been revoked. Reopen the artifact to retry.`);
      const downloadUrl = URL.createObjectURL(await response.blob());
      const anchor = globalThis.document.createElement('a');
      anchor.href = downloadUrl; anchor.download = title.split('/').pop() || 'report.pdf';
      globalThis.document.body.append(anchor); anchor.click(); anchor.remove(); URL.revokeObjectURL(downloadUrl);
    } catch (reason) {
      setDownloadError(reason instanceof Error ? reason.message : String(reason));
    } finally { setDownloading(false); }
  };
  return <section aria-label={`PDF report: ${title}`} className="repository-pdf">
    <div className="repository-actions">
      <button className="button secondary" disabled={!document || page <= 1 || rendering} onClick={() => setPage(page - 1)}>Previous PDF page</button>
      <span>Page {page} of {document?.numPages ?? '…'}</span>
      <button className="button secondary" disabled={!document || page >= document.numPages || rendering} onClick={() => setPage(page + 1)}>Next PDF page</button>
      <label>PDF zoom<select value={zoom} onChange={(event) => setZoom(Number(event.target.value))}>
        {[0.5, 0.75, 1, 1.25, 1.5, 2].map((value) => <option key={value} value={value}>{value * 100}%</option>)}
      </select></label>
      <button className="button secondary" disabled={downloading} onClick={() => void downloadOriginal()}>
        {downloading ? 'Downloading original PDF…' : 'Download original PDF'}
      </button>
    </div>
    {error && <p role="alert">{error}</p>}
    {downloadError && <p role="alert">{downloadError}</p>}
    {!document && !error && <p role="status">Loading PDF report…</p>}
    <div className="repository-pdf-canvas"><canvas key={`${page}:${zoom}`} ref={canvas} aria-hidden="true" /></div>
    <section aria-label={`Selectable PDF text, page ${page}`}>
      <h4>Page {page} text</h4>
      {textError ? <p role="alert">PDF text could not be extracted: {textError}. Download the original PDF to read it in an accessible reader.</p>
        : text?.page === page ? text.content
          ? <pre className="repository-text-preview repository-pdf-text" tabIndex={0}>{text.content}</pre>
          : <p role="status">No selectable text was found on this page. It may be scanned. Download the original PDF to use an accessible reader or OCR.</p>
          : document ? <p role="status">Loading page text…</p> : null}
    </section>
    <p className="repository-muted">Read-only preview with selectable plain text in PDF extraction order; layout may differ.
      Embedded scripts, forms, attachments and annotation links are not executed. The original download uses the same authorized, revision-checked file.</p>
  </section>;
}
