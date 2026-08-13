// @vitest-environment jsdom
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { ExtractionChunks } from './PapersPage';

describe('ExtractionChunks', () => {
  it('shows extracted page text by default and supplies a label for missing citations', () => {
    const container = document.createElement('div');
    container.innerHTML = renderToStaticMarkup(
      <ExtractionChunks
        chunks={[
          {
            id: 'chunk-1',
            chunk_index: 0,
            citation: '',
            metadata: {},
            page_start: 4,
            page_end: 4,
            section_title: null,
            text: 'Visible extracted text',
          },
        ]}
      />,
    );

    const extraction = container.querySelector('details');
    expect(extraction?.open).toBe(true);
    expect(extraction?.querySelector('summary')?.textContent).toBe('Page 4');
    expect(extraction?.textContent).toContain('Visible extracted text');
  });
});
