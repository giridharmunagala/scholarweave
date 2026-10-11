import {
  Children,
  isValidElement,
  memo,
  useEffect,
  useRef,
  useState,
  type ComponentPropsWithoutRef,
  type ReactNode,
} from 'react';
import ReactMarkdown from 'react-markdown';
import type { Components } from 'react-markdown';
import rehypeKatex from 'rehype-katex';
import rehypeRaw from 'rehype-raw';
import rehypeSanitize, { defaultSchema } from 'rehype-sanitize';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import 'katex/dist/katex.min.css';
import { ChartBlock } from './Chart';
import { isChartLanguage } from './chartSpec';
import { CodeBlock } from './CodeBlock';
import { Icon } from './Icons';

const MARKDOWN_COMPONENTS: Components = {
  img: MarkdownImage,
  table: MarkdownTable,
  pre: MarkdownPre,
};
const SAFE_HTML_SCHEMA = {
  ...defaultSchema,
  tagNames: [
    ...(defaultSchema.tagNames ?? []),
    'abbr',
    'mark',
  ],
  attributes: {
    ...defaultSchema.attributes,
    abbr: [
      ...(defaultSchema.attributes?.abbr ?? []),
      'title',
    ],
    div: [
      ...(defaultSchema.attributes?.div ?? []),
      ['className', 'math', 'math-display'],
    ],
    span: [
      ...(defaultSchema.attributes?.span ?? []),
      ['className', 'math', 'math-inline'],
    ],
  },
};

export const MarkdownViewer = memo(function MarkdownViewer({ content, workspacePath, onNavigate }: {
  content: string;
  workspacePath?: string;
  onNavigate?: (to: string) => void;
}) {
  const components: Components = workspacePath ? {
    ...MARKDOWN_COMPONENTS,
    a: ({ href, children, node: _node, ...props }) => {
      const target = href ? workspaceLink(href, workspacePath) : href;
      const internal = !!target && /^\/(tracks\?|library(?:\/notes)?\?)/.test(target);
      return <a {...props} href={target}
        aria-disabled={href && !target ? true : undefined}
        title={href && !target ? 'Invalid workspace link: the path leaves the workspace or is malformed.' : props.title}
        onClick={(event) => {
          if (internal && onNavigate && target && !props.target && !event.defaultPrevented && event.button === 0
            && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) {
            event.preventDefault();
            onNavigate(target);
          }
        }}>{children}</a>;
    },
  } : MARKDOWN_COMPONENTS;
  return (
    <article className="markdown-viewer">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[
          rehypeRaw,
          [rehypeSanitize, SAFE_HTML_SCHEMA],
          rehypeKatex,
        ]}
        components={components}
      >
        {normalizeModelMath(content)}
      </ReactMarkdown>
    </article>
  );
});

function workspaceLink(href: string, fromPath: string): string | undefined {
  if (/^(?:[a-z][a-z0-9+.-]*:|[/?#])/i.test(href)) return href;
  const hashAt = href.indexOf('#');
  const hash = hashAt < 0 ? '' : href.slice(hashAt);
  const rawPath = (hashAt < 0 ? href : href.slice(0, hashAt)).split('?')[0];
  let relative: string;
  try {
    relative = decodeURIComponent(rawPath).replace(/\\/g, '/');
  } catch (error) {
    if (error instanceof URIError) return undefined;
    throw error;
  }
  if (relative.startsWith('/') || /^[a-z]+:/i.test(relative)) return undefined;
  const parts = fromPath.replace(/\\/g, '/').split('/').slice(0, -1);
  for (const part of relative.split('/')) {
    if (!part || part === '.') continue;
    if (part === '..') {
      if (!parts.length) return undefined;
      parts.pop();
    } else parts.push(part);
  }
  const path = parts.join('/');
  const track = /^projects\/tracks\/[^/]+?--([a-zA-Z0-9_-]+)\/(problem|literature|experiments|results|exploration\/[a-z0-9][a-z0-9_-]{0,79})\.md$/.exec(path);
  if (track) return `/tracks?id=${encodeURIComponent(track[1])}&section=${encodeURIComponent(track[2])}${hash}`;
  const paper = /^library\/papers\/[^/]+?--([a-zA-Z0-9_-]+)\/[^/]+\.pdf$/i.exec(path);
  if (paper) return `/library?paper=${encodeURIComponent(paper[1])}${hash}`;
  if (/\.md$/i.test(path)) return `/library/notes?path=${encodeURIComponent(path)}${hash}`;
  return href;
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

type MarkdownPreProps = ComponentPropsWithoutRef<'pre'> & { node?: unknown };

/**
 * A fenced block is never just text. Depending on its info string it is code
 * worth colouring, or a chart worth drawing, so the `pre` element is replaced
 * wholesale rather than styled.
 */
function MarkdownPre({ children, node: _node, ...props }: MarkdownPreProps) {
  const child = Children.toArray(children)[0];
  if (isValidElement<{ className?: string; children?: ReactNode }>(child)) {
    const language = /language-([\w+#.-]+)/.exec(child.props.className ?? '')?.[1] ?? '';
    const source = textOf(child.props.children);
    if (isChartLanguage(language)) return <ChartBlock source={source} />;
    return <CodeBlock code={source} language={language} />;
  }
  return <pre {...props}>{children}</pre>;
}

function textOf(node: ReactNode): string {
  if (node === null || node === undefined || typeof node === 'boolean') return '';
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(textOf).join('');
  if (isValidElement<{ children?: ReactNode }>(node)) return textOf(node.props.children);
  return '';
}

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
