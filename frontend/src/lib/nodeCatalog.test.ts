import { describe, expect, it } from 'vitest';
import { categoryMeta, fuzzyScore, groupByCategory, highlightParts, searchNodes } from './nodeCatalog';
import type { NodeDefinitionResponse } from '../types/api';

function node(partial: Partial<NodeDefinitionResponse> & { type: string }): NodeDefinitionResponse {
  return {
    label: partial.type,
    description: '',
    category: 'llm',
    tags: [],
    inputs: [],
    outputs: [],
    config_schema: {},
    ...partial,
  };
}

const catalog: NodeDefinitionResponse[] = [
  node({ type: 'ollama_generate', label: 'Ollama Generate/Chat', category: 'llm' }),
  node({ type: 'ollama_embed', label: 'Ollama Embed', category: 'llm' }),
  node({ type: 'text_input', label: 'Text/Input', category: 'inputs' }),
  node({ type: 'final_output', label: 'Final Output', category: 'outputs' }),
  node({ type: 'vector_retrieve', label: 'Vector Retrieve', category: 'retrieval', tags: ['semantic'] }),
];

describe('categoryMeta', () => {
  it('returns known metadata', () => {
    expect(categoryMeta('llm').label).toBe('LLM');
    expect(categoryMeta('retrieval').icon).toBe('search');
  });

  it('falls back for unknown categories', () => {
    const meta = categoryMeta('made-up');
    expect(meta.label).toBe('made-up');
    expect(meta.order).toBe(99);
  });
});

describe('groupByCategory', () => {
  it('orders groups by the declared category order and sorts nodes by label', () => {
    const groups = groupByCategory(catalog);
    expect(groups.map((group) => group.meta.id)).toEqual(['inputs', 'retrieval', 'llm', 'outputs']);
    expect(groups[2].nodes.map((entry) => entry.label)).toEqual(['Ollama Embed', 'Ollama Generate/Chat']);
  });
});

describe('fuzzyScore', () => {
  it('ranks a label prefix above a substring match', () => {
    const prefix = fuzzyScore('ollama', catalog[0])!;
    const substring = fuzzyScore('generate', catalog[0])!;
    expect(prefix).toBeLessThan(substring);
  });

  it('matches out-of-order subsequences', () => {
    expect(fuzzyScore('ollgen', catalog[0])).not.toBeNull();
  });

  it('returns null when characters are missing', () => {
    expect(fuzzyScore('zzz', catalog[0])).toBeNull();
  });

  it('matches on tags', () => {
    expect(fuzzyScore('semantic', catalog[4])).not.toBeNull();
  });
});

describe('searchNodes', () => {
  it('returns everything for an empty term', () => {
    expect(searchNodes(catalog, '  ')).toHaveLength(catalog.length);
  });

  it('puts the best match first', () => {
    expect(searchNodes(catalog, 'ollgen')[0].type).toBe('ollama_generate');
    expect(searchNodes(catalog, 'final')[0].type).toBe('final_output');
  });
});

describe('highlightParts', () => {
  it('splits around a case-insensitive match', () => {
    expect(highlightParts('Ollama Generate/Chat', 'gen')).toEqual(['Ollama ', 'Gen', 'erate/Chat']);
  });

  it('returns the whole string when there is no match', () => {
    expect(highlightParts('Ollama Embed', 'zzz')).toEqual(['Ollama Embed', '', '']);
  });
});
