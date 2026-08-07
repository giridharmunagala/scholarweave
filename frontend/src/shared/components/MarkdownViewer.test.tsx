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

  it('renders dollar and model-emitted bracket math with KaTeX', () => {
    const html = renderToStaticMarkup(
      <MarkdownViewer
        content={
          'Inline math: $q_t$, \\(k_i\\), and similarity sim\\$\\cdot,\\cdot\\$.\n\n[ \\text{Attention}(q_t)=\\sum_{i\\le t}\\text{weight}(q_t,k_i)v_i ]\n\n[Important]'
        }
      />,
    );

    expect(html.match(/class="katex"/g)).toHaveLength(4);
    expect(html).toContain('class="katex-display"');
    expect(html).toContain('Attention');
    expect(html).not.toContain('sim\\$');
    expect(html).toContain('<p>[Important]</p>');
  });

  it('does not normalize math-like content inside code', () => {
    const html = renderToStaticMarkup(
      <MarkdownViewer content={'Inline `\\(x\\)` and `\\$y\\$`.\n\n```text\n[ x = y ]\n```'} />,
    );

    expect(html).toContain('\\(x\\)');
    expect(html).toContain('\\$y\\$');
    expect(html).toContain('[ x = y ]');
    expect(html).not.toContain('class="katex');
  });
});
