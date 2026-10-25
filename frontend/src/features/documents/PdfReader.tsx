import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { PDFDocumentProxy, PDFPageProxy } from 'pdfjs-dist';
import { Icon } from '../../shared/components/Icons';

export interface Quote { text: string; page: number }

type PdfJs = typeof import('pdfjs-dist');
let pdfjsPromise: Promise<PdfJs> | null = null;
function loadPdfJs(): Promise<PdfJs> {
  pdfjsPromise ??= Promise.all([
    import('pdfjs-dist'),
    import('pdfjs-dist/build/pdf.worker.min.mjs?url'),
  ]).then(([pdfjs, worker]) => {
    pdfjs.GlobalWorkerOptions.workerSrc = worker.default;
    return pdfjs;
  });
  return pdfjsPromise;
}

/** PDF text keeps line breaks and hyphenation from the page layout; quotes should not. */
export function cleanQuote(text: string): string {
  return text.replace(/(\w)-\s*\n\s*(\w)/g, '$1$2').replace(/\s+/g, ' ').trim();
}

const ZOOMS = [0.75, 1, 1.25, 1.5, 2, 2.5];

interface Snip { page: number; blob: Blob; url: string }

/*
 * A reader built for working with a paper rather than just looking at it:
 * selectable text with quote/note/ask actions, region snipping to an image,
 * and a remembered position.
 */
export function PdfReader({
  url,
  title,
  initialPage = 1,
  onPageChange,
  onQuote,
  onNotice,
}: {
  url: string;
  title: string;
  initialPage?: number;
  onPageChange?: (page: number, total: number) => void;
  onQuote: (action: 'note' | 'ask', quote: Quote) => void;
  onNotice: (message: string) => void;
}) {
  const [doc, setDoc] = useState<PDFDocumentProxy | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [baseWidth, setBaseWidth] = useState(612);
  const [fitScale, setFitScale] = useState(1);
  const [zoom, setZoom] = useState<number | 'fit'>('fit');
  const [page, setPage] = useState(initialPage);
  const [selection, setSelection] = useState<{ quote: Quote; x: number; y: number } | null>(null);
  const [snipping, setSnipping] = useState(false);
  const [snip, setSnip] = useState<Snip | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const restored = useRef(false);
  const pdfjsRef = useRef<PdfJs | null>(null);

  useEffect(() => {
    let live = true;
    let loaded: PDFDocumentProxy | null = null;
    setDoc(null);
    setError(null);
    restored.current = false;
    loadPdfJs()
      .then((pdfjs) => { pdfjsRef.current = pdfjs; return pdfjs.getDocument({ url }).promise; })
      .then(async (value) => {
        loaded = value;
        if (!live) { void value.destroy(); return; }
        const first = await value.getPage(1);
        setBaseWidth(first.getViewport({ scale: 1 }).width);
        setDoc(value);
      })
      .catch((reason: unknown) => { if (live) setError(reason instanceof Error ? reason.message : String(reason)); });
    return () => { live = false; void loaded?.destroy(); };
  }, [url]);

  useLayoutEffect(() => {
    const element = scrollRef.current;
    if (!element || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(() => {
      setFitScale(Math.max(0.5, Math.min(3, (element.clientWidth - 48) / baseWidth)));
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [baseWidth, doc]);

  const scale = zoom === 'fit' ? fitScale : zoom;
  const total = doc?.numPages ?? 0;

  const goTo = useCallback((target: number, behavior: ScrollBehavior = 'smooth') => {
    const element = scrollRef.current?.querySelector<HTMLElement>(`[data-page="${target}"]`);
    element?.scrollIntoView({ behavior, block: 'start' });
  }, []);

  useEffect(() => {
    if (!doc || restored.current) return;
    restored.current = true;
    if (initialPage > 1) window.requestAnimationFrame(() => goTo(Math.min(initialPage, doc.numPages), 'auto'));
  }, [doc, initialPage, goTo]);

  useEffect(() => {
    const element = scrollRef.current;
    if (!element || !doc) return;
    const onScroll = () => {
      const middle = element.getBoundingClientRect().top + element.clientHeight / 3;
      let current = 1;
      for (const node of element.querySelectorAll<HTMLElement>('[data-page]')) {
        if (node.getBoundingClientRect().top <= middle) current = Number(node.dataset.page);
        else break;
      }
      setPage((value) => (value === current ? value : current));
      setSelection(null);
    };
    element.addEventListener('scroll', onScroll, { passive: true });
    return () => element.removeEventListener('scroll', onScroll);
  }, [doc]);

  useEffect(() => { if (total) onPageChange?.(page, total); }, [page, total]);

  const captureSelection = () => {
    const active = window.getSelection();
    const container = scrollRef.current;
    if (!active || active.isCollapsed || !container || !active.anchorNode || !container.contains(active.anchorNode)) {
      setSelection(null);
      return;
    }
    const text = cleanQuote(active.toString());
    if (!text) { setSelection(null); return; }
    const anchor = active.anchorNode instanceof Element ? active.anchorNode : active.anchorNode.parentElement;
    const pageNumber = Number(anchor?.closest<HTMLElement>('[data-page]')?.dataset.page ?? page);
    const rect = active.getRangeAt(0).getBoundingClientRect();
    const box = container.getBoundingClientRect();
    setSelection({
      quote: { text, page: pageNumber },
      x: Math.min(Math.max(rect.left + rect.width / 2 - box.left, 140), box.width - 140),
      y: rect.top - box.top + container.scrollTop,
    });
  };

  const citation = (quote: Quote) => `“${quote.text}” (${title}, p. ${quote.page})`;
  const act = async (kind: 'copy' | 'cite' | 'note' | 'ask') => {
    if (!selection) return;
    const { quote } = selection;
    if (kind === 'copy' || kind === 'cite') {
      try {
        await navigator.clipboard.writeText(kind === 'copy' ? quote.text : citation(quote));
        onNotice(kind === 'copy' ? 'Quote copied' : 'Quote copied with citation');
      } catch {
        onNotice('The browser blocked clipboard access');
      }
    } else {
      onQuote(kind, quote);
    }
    window.getSelection()?.removeAllRanges();
    setSelection(null);
  };

  const finishSnip = (pageNumber: number, canvas: HTMLCanvasElement, rect: DOMRect) => {
    const ratio = canvas.width / canvas.getBoundingClientRect().width;
    const out = document.createElement('canvas');
    out.width = Math.max(1, Math.round(rect.width * ratio));
    out.height = Math.max(1, Math.round(rect.height * ratio));
    out.getContext('2d')?.drawImage(canvas, rect.x * ratio, rect.y * ratio, out.width, out.height, 0, 0, out.width, out.height);
    out.toBlob((blob) => {
      if (!blob) return;
      setSnip((previous) => { if (previous) URL.revokeObjectURL(previous.url); return { page: pageNumber, blob, url: URL.createObjectURL(blob) }; });
      setSnipping(false);
    }, 'image/png');
  };

  const copySnip = async () => {
    if (!snip) return;
    try {
      await navigator.clipboard.write([new ClipboardItem({ 'image/png': snip.blob })]);
      onNotice('Image copied');
    } catch {
      onNotice('This browser cannot copy images; use Download instead');
    }
  };
  const downloadSnip = () => {
    if (!snip) return;
    const anchor = document.createElement('a');
    anchor.href = snip.url;
    anchor.download = `${title.replace(/[^\w-]+/g, '-').slice(0, 60)}-p${snip.page}.png`;
    anchor.click();
  };
  useEffect(() => () => { if (snip) URL.revokeObjectURL(snip.url); }, [snip]);

  useEffect(() => {
    if (!snipping) return;
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') { event.preventDefault(); setSnipping(false); } };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [snipping]);

  const zoomIndex = ZOOMS.findIndex((value) => value >= scale - 0.01);
  return (
    <div className="pdf-reader">
      <div className="pdf-toolbar" role="toolbar" aria-label="Reader tools">
        <label className="pdf-page-jump">
          <span className="sr-only">Page</span>
          <input
            type="number"
            min={1}
            max={total || 1}
            value={page}
            aria-label="Current page"
            onChange={(event) => {
              const value = Number(event.target.value);
              if (value >= 1 && value <= total) { setPage(value); goTo(value); }
            }}
          />
          <small>/ {total || '…'}</small>
        </label>
        <span className="pdf-tool-sep" />
        <button type="button" className="icon-button" aria-label="Zoom out" title="Zoom out" disabled={!doc}
          onClick={() => setZoom(ZOOMS[Math.max(0, (zoomIndex < 0 ? ZOOMS.length : zoomIndex) - 1)])}>−</button>
        <button type="button" className={`pdf-zoom-label${zoom === 'fit' ? ' active' : ''}`} title="Fit to width" onClick={() => setZoom('fit')}>
          {Math.round(scale * 100)}%
        </button>
        <button type="button" className="icon-button" aria-label="Zoom in" title="Zoom in" disabled={!doc}
          onClick={() => setZoom(ZOOMS[Math.min(ZOOMS.length - 1, zoomIndex < 0 ? ZOOMS.length - 1 : zoomIndex + (ZOOMS[zoomIndex] - scale < 0.01 ? 1 : 0))])}>+</button>
        <span className="pdf-tool-sep" />
        <button
          type="button"
          className={`pdf-tool${snipping ? ' active' : ''}`}
          aria-pressed={snipping}
          title="Drag a box on the page to capture a figure, table or equation as an image"
          disabled={!doc}
          onClick={() => setSnipping((value) => !value)}
        >
          <Icon name="scan" size={14} /> {snipping ? 'Drag to capture… (Esc)' : 'Snip image'}
        </button>
      </div>

      <div
        className={`pdf-scroll${snipping ? ' snipping' : ''}`}
        ref={scrollRef}
        onMouseUp={() => window.setTimeout(captureSelection, 0)}
        onKeyUp={(event) => { if (event.shiftKey) captureSelection(); }}
      >
        {error ? <p className="notice error">Could not open this PDF: {error}</p> : null}
        {!doc && !error ? <p className="pdf-loading"><span className="spinner" aria-hidden="true" /> Opening the PDF…</p> : null}
        {doc ? Array.from({ length: doc.numPages }, (_, index) => (
          <PdfPage
            key={index + 1}
            doc={doc}
            pdfjs={pdfjsRef.current!}
            number={index + 1}
            scale={scale}
            near={Math.abs(index + 1 - page) <= 2}
            snipping={snipping}
            onSnip={finishSnip}
          />
        )) : null}
        {selection ? (
          <div className="selection-popover" role="toolbar" aria-label="Selection actions"
            style={{ left: selection.x, top: Math.max(selection.y - 46, 4) }}
            onMouseDown={(event) => event.preventDefault()}>
            <button type="button" onClick={() => void act('copy')}><Icon name="copy" size={13} />Copy</button>
            <button type="button" onClick={() => void act('cite')}>Copy with citation</button>
            <button type="button" onClick={() => void act('note')}><Icon name="file" size={13} />Add to notes</button>
            <button type="button" className="ask" onClick={() => void act('ask')}><Icon name="sparkle" size={13} />Ask</button>
          </div>
        ) : null}
      </div>

      {snip ? (
        <div className="snip-tray" role="dialog" aria-label="Captured image">
          <img src={snip.url} alt={`Capture from page ${snip.page}`} />
          <div className="snip-actions">
            <small>From page {snip.page}</small>
            <button type="button" className="button small" onClick={() => void copySnip()}><Icon name="copy" size={13} />Copy image</button>
            <button type="button" className="button small secondary" onClick={downloadSnip}><Icon name="download" size={13} />Download PNG</button>
            <button type="button" className="icon-button" aria-label="Discard capture" onClick={() => { URL.revokeObjectURL(snip.url); setSnip(null); }}>
              <Icon name="close" size={13} />
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function PdfPage({
  doc, pdfjs, number, scale, near, snipping, onSnip,
}: {
  doc: PDFDocumentProxy;
  pdfjs: PdfJs;
  number: number;
  scale: number;
  near: boolean;
  snipping: boolean;
  onSnip: (page: number, canvas: HTMLCanvasElement, rect: DOMRect) => void;
}) {
  const [size, setSize] = useState<{ width: number; height: number } | null>(null);
  const [visible, setVisible] = useState(near);
  const [drag, setDrag] = useState<{ x0: number; y0: number; x1: number; y1: number } | null>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const textRef = useRef<HTMLDivElement>(null);
  const pageRef = useRef<PDFPageProxy | null>(null);

  useEffect(() => {
    let live = true;
    void doc.getPage(number).then((value) => {
      if (!live) return;
      pageRef.current = value;
      const viewport = value.getViewport({ scale });
      setSize({ width: viewport.width, height: viewport.height });
    });
    return () => { live = false; };
  }, [doc, number, scale]);

  useEffect(() => {
    if (near) { setVisible(true); return; }
    const element = wrapRef.current;
    if (!element || typeof IntersectionObserver === 'undefined') { setVisible(true); return; }
    const observer = new IntersectionObserver(([entry]) => { if (entry.isIntersecting) setVisible(true); }, { rootMargin: '600px 0px' });
    observer.observe(element);
    return () => observer.disconnect();
  }, [near]);

  useEffect(() => {
    const value = pageRef.current;
    const canvas = canvasRef.current;
    const textLayer = textRef.current;
    if (!visible || !size || !value || !canvas || !textLayer) return;
    const viewport = value.getViewport({ scale });
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.floor(viewport.width * ratio);
    canvas.height = Math.floor(viewport.height * ratio);
    const context = canvas.getContext('2d');
    if (!context) return;
    const task = value.render({ canvasContext: context, viewport, transform: ratio !== 1 ? [ratio, 0, 0, ratio, 0, 0] : undefined });
    textLayer.replaceChildren();
    const text = new pdfjs.TextLayer({ textContentSource: value.streamTextContent(), container: textLayer, viewport });
    void text.render().catch(() => undefined);
    task.promise.catch(() => undefined);
    return () => { task.cancel(); text.cancel(); };
  }, [visible, size, scale, pdfjs]);

  const point = (event: React.PointerEvent) => {
    const box = wrapRef.current!.getBoundingClientRect();
    return { x: Math.min(Math.max(event.clientX - box.left, 0), box.width), y: Math.min(Math.max(event.clientY - box.top, 0), box.height) };
  };
  const rect = drag ? new DOMRect(Math.min(drag.x0, drag.x1), Math.min(drag.y0, drag.y1), Math.abs(drag.x1 - drag.x0), Math.abs(drag.y1 - drag.y0)) : null;

  return (
    <div
      className="pdf-page"
      data-page={number}
      ref={wrapRef}
      style={{ width: size?.width ?? 612 * scale, height: size?.height ?? 792 * scale, '--scale-factor': scale } as React.CSSProperties}
    >
      <canvas ref={canvasRef} style={{ width: '100%', height: '100%' }} />
      <div className="textLayer" ref={textRef} />
      <span className="pdf-page-number" aria-hidden="true">{number}</span>
      {snipping ? (
        <div
          className="snip-surface"
          onPointerDown={(event) => { const p = point(event); (event.target as HTMLElement).setPointerCapture(event.pointerId); setDrag({ x0: p.x, y0: p.y, x1: p.x, y1: p.y }); }}
          onPointerMove={(event) => { if (drag) { const p = point(event); setDrag({ ...drag, x1: p.x, y1: p.y }); } }}
          onPointerUp={() => {
            if (rect && rect.width > 8 && rect.height > 8 && canvasRef.current) onSnip(number, canvasRef.current, rect);
            setDrag(null);
          }}
        >
          {rect ? <span className="snip-rect" style={{ left: rect.x, top: rect.y, width: rect.width, height: rect.height }} /> : null}
        </div>
      ) : null}
    </div>
  );
}
