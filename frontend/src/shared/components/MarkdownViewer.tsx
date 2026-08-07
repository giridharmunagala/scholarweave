import { useState, type ComponentPropsWithoutRef } from 'react';
import ReactMarkdown from 'react-markdown';
import type { Components } from 'react-markdown';
import rehypeKatex from 'rehype-katex';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import 'katex/dist/katex.min.css';

const MARKDOWN_COMPONENTS: Components = { img: MarkdownImage };

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
