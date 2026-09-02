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

  it('renders safe embedded HTML and removes executable markup', () => {
    const html = renderToStaticMarkup(
      <MarkdownViewer
        content={
          '<details open onclick="alert(1)"><summary>Derivation</summary><p>Use <em>verified</em> evidence.</p></details>\n\n<script>alert(2)</script><a href="javascript:alert(3)">Unsafe link</a>'
        }
      />,
    );

    expect(html).toContain('<details open="">');
    expect(html).toContain('<summary>Derivation</summary>');
    expect(html).toContain('<em>verified</em>');
    expect(html).not.toContain('onclick');
    expect(html).not.toContain('<script');
    expect(html).not.toContain('javascript:');
  });

  it('presents a fenced code block as a labelled, highlighted artefact', () => {
    const html = renderToStaticMarkup(
      <MarkdownViewer content={'```python\n# note\ndef run():\n    return 1\n```'} />,
    );

    expect(html).toContain('class="code-card"');
    expect(html).toContain('Python');
    expect(html).toContain('3 lines');
    expect(html).toContain('class="tok tok-comment"');
    expect(html).toContain('class="tok tok-keyword"');
  });

  it('draws a chart fence instead of printing its JSON', () => {
    const html = renderToStaticMarkup(
      <MarkdownViewer
        content={
          '```chart\n{"type":"bar","title":"Citations","labels":["2021","2022"],"series":[{"name":"Ours","data":[3,8]}]}\n```'
        }
      />,
    );

    expect(html).toContain('class="chart-card"');
    expect(html).toContain('<svg');
    expect(html).toContain('Citations');
    expect(html).toContain('2021');
    expect(html).not.toContain('"series"');
  });

  it('falls back to the source when a chart cannot be read', () => {
    const html = renderToStaticMarkup(<MarkdownViewer content={'```chart\n{oops\n```'} />);

    expect(html).toContain('chart-card-error');
    expect(html).toContain('not valid JSON');
    expect(html).toContain('{oops');
  });

  it('still renders plain fences and inline code', () => {
    const html = renderToStaticMarkup(<MarkdownViewer content={'Use `npm run dev`.\n\n```\nraw text\n```'} />);

    expect(html).toContain('<code>npm run dev</code>');
    expect(html).toContain('raw text');
    expect(html).toContain('Text');
  });
});
