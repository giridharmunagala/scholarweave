import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { MarkdownViewer } from './MarkdownViewer';

describe('MarkdownViewer', () => {
  it('opens track-relative canonical summaries, notes and PDFs through the existing library', () => {
    const html = renderToStaticMarkup(<MarkdownViewer
      workspacePath="projects/tracks/adaptive--track-1/literature.md"
      content={'[summary](../../../library/papers/adaptive--paper-1/summary.md)\n\n[notes](../../../library/papers/adaptive--paper-1/notes.md)\n\n[PDF](../../../library/papers/adaptive--paper-1/source.pdf)\n\n[experiment](experiments.md#exp-001)'}
    />);
    expect(html).toContain('href="/library/notes?path=library%2Fpapers%2Fadaptive--paper-1%2Fsummary.md"');
    expect(html).toContain('href="/library/notes?path=library%2Fpapers%2Fadaptive--paper-1%2Fnotes.md"');
    expect(html).toContain('href="/library?paper=paper-1"');
    expect(html).toContain('href="/tracks?id=track-1&amp;section=experiments#exp-001"');
  });

  it('keeps external sources and footnotes unchanged and rejects malformed or escaping workspace links', () => {
    const html = renderToStaticMarkup(<MarkdownViewer workspacePath="knowledge/review.md"
      content={'[source](https://example.org/paper)\n\n[footnote](#note)\n\n[escape](../../private.md)\n\n[bad](%C0%AF.md)'}
    />);
    expect(html).toContain('href="https://example.org/paper"');
    expect(html).toContain('href="#note"');
    expect(html).not.toContain('href="../../private.md"');
    expect(html).not.toContain('href="%C0%AF.md"');
    expect(html.match(/aria-disabled="true"/g)).toHaveLength(2);
  });

  it('preserves complete identifiers containing double hyphens in local reader links', () => {
    const html = renderToStaticMarkup(<MarkdownViewer
      workspacePath="projects/tracks/adaptive--track--one/literature.md"
      content={'[PDF](../../../library/papers/adaptive--paper--one/source.pdf)\n\n[problem](problem.md)'}
    />);
    expect(html).toContain('href="/library?paper=paper--one"');
    expect(html).toContain('href="/tracks?id=track--one&amp;section=problem"');
  });

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

  it('renders safe semantic HTML for highlighted research notes', () => {
    const html = renderToStaticMarkup(
      <MarkdownViewer
        content={'<details open><summary>Evidence</summary><p><mark>Supported</mark> with <kbd>Ctrl</kbd> and <abbr title="confidence interval">CI</abbr>.</p></details>'}
      />,
    );

    expect(html).toContain('<details open="">');
    expect(html).toContain('<mark>Supported</mark>');
    expect(html).toContain('<kbd>Ctrl</kbd>');
    expect(html).toContain('<abbr title="confidence interval">CI</abbr>');
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
