import { useState, type ComponentPropsWithoutRef } from 'react';
import ReactMarkdown from 'react-markdown';
import type { Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';

const MARKDOWN_COMPONENTS: Components = { img: MarkdownImage };

export function MarkdownViewer({ content }: { content: string }) {
  return (
    <article className="markdown-viewer">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={MARKDOWN_COMPONENTS}>
        {content}
      </ReactMarkdown>
    </article>
  );
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
