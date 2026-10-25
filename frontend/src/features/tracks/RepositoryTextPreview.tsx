import { MarkdownViewer } from '../../shared/components/MarkdownViewer';

export function parseCsvPreview(content: string, limit = 200): { rows: string[][]; truncated: boolean } {
  const rows: string[][] = [];
  let row: string[] = [];
  let value = '';
  let quoted = false;
  let quoteClosed = false;
  for (let i = 0; i < content.length; i++) {
    const char = content[i];
    if (char === '"') {
      if (quoted && content[i + 1] === '"') { value += '"'; i++; }
      else if (quoted) { quoted = false; quoteClosed = true; }
      else if (value === '' && !quoteClosed) quoted = true;
      else throw new Error('Unexpected quote in an unquoted CSV field.');
    } else if (char === ',' && !quoted) { row.push(value); value = ''; quoteClosed = false; }
    else if ((char === '\n' || char === '\r') && !quoted) {
      if (char === '\r' && content[i + 1] === '\n') i++;
      row.push(value); rows.push(row); row = []; value = ''; quoteClosed = false;
      if (rows.length >= limit) return { rows, truncated: i < content.length - 1 };
    } else if (quoteClosed) {
      if (!/\s/.test(char)) throw new Error('Unexpected text after a quoted CSV field.');
    } else value += char;
  }
  if (quoted) throw new Error('Unclosed quoted CSV field.');
  if (row.length || value) rows.push([...row, value]);
  return { rows, truncated: false };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function notebookText(value: unknown): string {
  if (typeof value === 'string') return value;
  if (Array.isArray(value) && value.every((line) => typeof line === 'string')) return value.join('');
  return '';
}

function NotebookPreview({ path, content, onOpenRelative }: {
  path: string; content: string; onOpenRelative: (path: string) => void;
}) {
  let notebook: unknown;
  try { notebook = JSON.parse(content); }
  catch { return <><p role="status">Invalid notebook JSON; showing original text.</p><pre className="repository-text-preview">{content}</pre></>; }
  if (!isRecord(notebook) || notebook.nbformat !== 4 || !Array.isArray(notebook.cells) || !notebook.cells.every(isRecord)) {
    return <><p role="status">Unsupported notebook structure. Version 4 notebooks are supported; showing original text.</p><pre className="repository-text-preview">{content}</pre></>;
  }
  return <section aria-label="Saved notebook preview" className="repository-text-preview">
    <p className="repository-muted">Saved cells and outputs only. No kernel, execution, widgets, embedded HTML or JavaScript. Other output formats are labeled, not executed.</p>
    {notebook.cells.slice(0, 200).map((cell, index) => <article key={index}>
      <h4>Cell {index + 1} · {String(cell.cell_type)}</h4>
      {cell.cell_type === 'markdown'
        ? <RepositoryTextPreview path={`${path}.md`} content={notebookText(cell.source)} onOpenRelative={onOpenRelative} />
        : <pre>{notebookText(cell.source)}</pre>}
      {Array.isArray(cell.outputs) ? cell.outputs.map((output: unknown, outputIndex: number) => {
        if (!isRecord(output)) return <p key={outputIndex}>Unrecognized saved output.</p>;
        const data = isRecord(output.data) ? output.data : {};
        const text = notebookText(output.text) || notebookText(data['text/plain']) || notebookText(output.traceback);
        const png = notebookText(data['image/png']).replace(/\s/g, '');
        const validPng = png.startsWith('iVBORw0KGgo') && /^[A-Za-z0-9+/]*={0,2}$/.test(png);
        return <div key={outputIndex}>
          {text ? <pre>{text}</pre> : null}
          {validPng ? <img src={`data:image/png;base64,${png}`} alt={`Saved output for cell ${index + 1}`} style={{ maxWidth: '100%' }} /> : null}
          {(!text && !validPng) || Object.keys(data).some((key) => !['text/plain', 'image/png'].includes(key))
            ? <p className="repository-muted">Some saved output formats are not rendered. Use manual text view to inspect their serialized content.</p> : null}
        </div>;
      }) : null}
    </article>)}
    {notebook.cells.length > 200 ? <p role="status">Preview limited to the first 200 cells.</p> : null}
  </section>;
}

export function RepositoryTextPreview({ path, content, onOpenRelative }: {
  path: string; content: string; onOpenRelative: (path: string) => void;
}) {
  if (/\.csv$/i.test(path)) {
    let parsed;
    try { parsed = parseCsvPreview(content); }
    catch (error) { return <><p role="status">Invalid CSV: {error instanceof Error ? error.message : String(error)} Showing original text.</p><pre className="repository-text-preview">{content}</pre></>; }
    const { rows, truncated } = parsed;
    return <div className="repository-text-preview">
      <table aria-label="CSV artifact preview"><tbody>{rows.map((row, i) =>
        <tr key={i}>{row.slice(0, 50).map((cell, j) => <td key={j}>{cell}</td>)}</tr>,
      )}</tbody></table>
      {(truncated || rows.some((row) => row.length > 50)) && <p>Preview limited to 200 rows and 50 columns; manual edit shows the complete text.</p>}
    </div>;
  }
  if (/\.ipynb$/i.test(path)) return <NotebookPreview path={path} content={content} onOpenRelative={onOpenRelative} />;
  if (/\.json$/i.test(path)) {
    try { return <pre className="repository-text-preview">{JSON.stringify(JSON.parse(content), null, 2)}</pre>; }
    catch { return <><p role="status">Invalid JSON; showing original text.</p><pre className="repository-text-preview">{content}</pre></>; }
  }
  if (/\.(md|markdown)$/i.test(path)) {
    // Entities cannot become Markdown image syntax or embedded HTML, including reference-style images.
    const safe = content.replace(/</g, '&lt;').replace(/!/g, '&#33;');
    return <div className="repository-text-preview" onClickCapture={(event) => {
      const anchor = (event.target as HTMLElement).closest('a');
      if (!anchor) return;
      event.preventDefault();
      const href = anchor.getAttribute('href') || '';
      if (!href || /^(?:[a-z][a-z0-9+.-]*:|[/?#\\])/i.test(href)) return;
      try {
        const parts = path.split('/').slice(0, -1);
        for (const part of decodeURIComponent(href.split(/[?#]/)[0]).replace(/\\/g, '/').split('/')) {
          if (part === '..') { if (!parts.length) return; parts.pop(); }
          else if (part && part !== '.') parts.push(part);
        }
        onOpenRelative(parts.join('/'));
      } catch { /* Malformed link is inert, never a workspace navigation. */ }
    }}>
      <MarkdownViewer content={safe} />
      <p className="repository-muted">Relative file links open within this repository. External links, remote images and embedded HTML are disabled.</p>
    </div>;
  }
  return <pre className="repository-text-preview">{content}</pre>;
}
