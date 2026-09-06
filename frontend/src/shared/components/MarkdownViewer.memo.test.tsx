// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { expect, it, vi } from 'vitest';
import ReactMarkdown from 'react-markdown';
import { MarkdownViewer } from './MarkdownViewer';

vi.mock('react-markdown', () => ({
  default: vi.fn(({ children }: { children: string }) => <p>{children}</p>),
}));

it('does not reparse unchanged messages on parent updates but renders changed content', () => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  const container = document.createElement('div');
  const root = createRoot(container);
  try {
    act(() => root.render(<MarkdownViewer content="Saved answer" />));
    for (let update = 0; update < 20; update += 1) {
      act(() => root.render(<MarkdownViewer content="Saved answer" />));
    }
    expect(ReactMarkdown).toHaveBeenCalledTimes(1);
    act(() => root.render(<MarkdownViewer content="Updated answer" />));
    expect(ReactMarkdown).toHaveBeenCalledTimes(2);
    expect(container.textContent).toBe('Updated answer');
  } finally {
    act(() => root.unmount());
  }
});
