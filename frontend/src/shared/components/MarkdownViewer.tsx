import { useEffect, useRef, useState, type ComponentPropsWithoutRef } from 'react';
import ReactMarkdown from 'react-markdown';
import type { Components } from 'react-markdown';
import rehypeKatex from 'rehype-katex';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import 'katex/dist/katex.min.css';
import { Icon } from './Icons';

const MARKDOWN_COMPONENTS: Components = { img: MarkdownImage, table: MarkdownTable };

export function MarkdownViewer({ content }: { content: string }) {
  return (
    <article className="markdown-viewer">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeKatex]}
        components={MARKDOWN_COMPONENTS}
      >
        {normalizeModelMath(content)}
      </ReactMarkdown>
    </article>
  );
}

function normalizeModelMath(content: string): string {
  let fence: string | null = null;

  return content
    .split('\n')
    .map((line) => {
      const fenceMatch = line.match(/^\s*(`{3,}|~{3,})/);
      if (fenceMatch) {
        if (!fence) fence = fenceMatch[1][0];
        else if (fence === fenceMatch[1][0]) fence = null;
        return line;
      }
      if (fence) return line;

      const latexDisplay = line.match(/^\s*\\\[\s*(.*?)\s*\\\]\s*$/);
      if (latexDisplay) return `$$\n${latexDisplay[1]}\n$$`;

      const bracketDisplay = line.match(/^\s*\[\s*(.*?)\s*\]\s*$/);
      if (bracketDisplay && looksLikeLatex(bracketDisplay[1])) {
        return `$$\n${bracketDisplay[1]}\n$$`;
      }
      return normalizeInlineMath(line);
    })
    .join('\n');
}

function normalizeInlineMath(line: string): string {
  const codeSpan = /(`+)(.*?)\1/g;
  let normalized = '';
  let cursor = 0;

  for (const match of line.matchAll(codeSpan)) {
    const index = match.index ?? 0;
    normalized += normalizeInlineMathSegment(line.slice(cursor, index));
    normalized += match[0];
    cursor = index + match[0].length;
  }

  return normalized + normalizeInlineMathSegment(line.slice(cursor));
}

function normalizeInlineMathSegment(value: string): string {
  return value
    .replace(/\\\((.+?)\\\)/g, (_, math: string) => `$${math}$`)
    .replace(/\\\$(.+?)\\\$/g, (_, math: string) => `$${math}$`);
}

function looksLikeLatex(value: string): boolean {
  return /\\[A-Za-z]+|[_^{}]|(?:^|\s)[A-Za-z0-9)]*\s*[=+\-*/<>]\s*[A-Za-z0-9(]/.test(value);
}

type MarkdownImageProps = ComponentPropsWithoutRef<'img'> & { node?: unknown };

type MarkdownTableProps = ComponentPropsWithoutRef<'table'> & { node?: unknown };

/**
 * Model answers lean on tables for their numbers, so a table is framed as an artefact: it can be
 * lifted out as-is (copy), taken away (CSV), or opened up when it is wider than the transcript.
 */
function MarkdownTable({ children, node: _node, ...props }: MarkdownTableProps) {
  const tableRef = useRef<HTMLTableElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!expanded) return;
    const close = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setExpanded(false);
    };
    window.addEventListener('keydown', close);
    return () => window.removeEventListener('keydown', close);
  }, [expanded]);

  const copy = async () => {
    const text = tableRows(tableRef.current)
      .map((row) => row.join('\t'))
      .join('\n');
    try {
      await writeClipboard(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      setCopied(false);
    }
  };

  const download = () => {
    const csv = tableRows(tableRef.current)
      .map((row) => row.map(csvCell).join(','))
      .join('\n');
    const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = 'table.csv';
    link.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className={`markdown-table-card${expanded ? ' expanded' : ''}`}>
      {expanded ? (
        <button
          type="button"
          className="markdown-table-scrim"
          aria-label="Close expanded table"
          onClick={() => setExpanded(false)}
        />
      ) : null}
      <div className="markdown-table-frame">
        <div className="markdown-table-actions">
          <button type="button" title={copied ? 'Copied' : 'Copy table'} aria-label="Copy table" onClick={() => void copy()}>
            <Icon name={copied ? 'check' : 'copy'} size={15} />
          </button>
          <button type="button" title="Download CSV" aria-label="Download table as CSV" onClick={download}>
            <Icon name="download" size={15} />
          </button>
          <button
            type="button"
            title={expanded ? 'Close' : 'Expand table'}
            aria-label={expanded ? 'Close expanded table' : 'Expand table'}
            onClick={() => setExpanded((value) => !value)}
          >
            <Icon name={expanded ? 'close' : 'expand'} size={15} />
          </button>
        </div>
        <div className="markdown-table-scroll">
          <table {...props} ref={tableRef}>
            {children}
          </table>
        </div>
      </div>
    </div>
  );
}

function tableRows(table: HTMLTableElement | null): string[][] {
  if (!table) return [];
  return [...table.querySelectorAll('tr')].map((row) =>
    [...row.querySelectorAll('th, td')].map((cell) => (cell.textContent ?? '').trim()),
  );
}

function csvCell(value: string): string {
  return /[",\n]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value;
}

async function writeClipboard(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand('copy');
  textarea.remove();
  if (!copied) throw new Error('The browser denied clipboard access.');
}

function MarkdownImage({ src, alt, node: _node, ...props }: MarkdownImageProps) {
  const [failed, setFailed] = useState(false);
  if (!src) return null;
  const label = alt || 'Open image';
  if (failed) {
    return (
      <a className="markdown-image-link" href={String(src)} target="_blank" rel="noreferrer">
        {label}
      </a>
    );
  }
  return (
    <figure className="markdown-image">
      <img
        {...props}
        src={src}
        alt={alt}
        loading="lazy"
        decoding="async"
        onError={() => setFailed(true)}
      />
      {alt ? <figcaption>{alt}</figcaption> : null}
    </figure>
  );
}
