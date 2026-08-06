import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { MarkdownViewer } from './MarkdownViewer';

describe('MarkdownViewer', () => {
  it('renders GitHub-flavored markdown as document markup', () => {
    const html = renderToStaticMarkup(
      <MarkdownViewer
        content={
          '# Findings\n\n**Important** result.\n\n| Metric | Value |\n| --- | --- |\n| Accuracy | 98% |\n\n![Figure](/api/artifacts/figure/raw)'
        }
      />,
    );

    expect(html).toContain('<h1>Findings</h1>');
    expect(html).toContain('<strong>Important</strong>');
    expect(html).toContain('<table>');
    expect(html).toContain('<figure class="markdown-image">');
    expect(html).toContain('<figcaption>Figure</figcaption>');
    expect(html).not.toContain('# Findings');
  });
});
