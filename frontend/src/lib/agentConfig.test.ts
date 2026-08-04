import { describe, expect, it } from 'vitest';
import {
  instructionPlaceholders,
  isChatModel,
  parseImportList,
  parseOutputSchema,
  pythonCodeError,
} from './agentConfig';

describe('isChatModel', () => {
  it('hides embedding-only models, which cannot drive an agent', () => {
    expect(isChatModel('nomic-embed-text:latest')).toBe(false);
    expect(isChatModel('embeddinggemma:300m')).toBe(false);
  });

  it('keeps generation models', () => {
    expect(isChatModel('llama3.1:8b')).toBe(true);
    expect(isChatModel('gemma4:12b')).toBe(true);
  });
});

describe('instructionPlaceholders', () => {
  it('finds every reference once, in order', () => {
    const found = instructionPlaceholders('Summarise {{input}} for {{reader}}, citing {{input}}.');
    expect(found).toEqual(['input', 'reader']);
  });

  it('tolerates padding inside the braces', () => {
    expect(instructionPlaceholders('Use {{  document_id  }}.')).toEqual(['document_id']);
  });

  it('returns nothing when there are no references', () => {
    expect(instructionPlaceholders('Just answer the question.')).toEqual([]);
  });
});

describe('parseOutputSchema', () => {
  it('treats a blank box as no structured output', () => {
    expect(parseOutputSchema('   ')).toEqual({ value: null, error: '' });
  });

  it('accepts an object schema', () => {
    const { value, error } = parseOutputSchema('{"type": "object"}');
    expect(error).toBe('');
    expect(value).toEqual({ type: 'object' });
  });

  it('rejects a bare array', () => {
    expect(parseOutputSchema('[1, 2]').error).toMatch(/JSON object/);
  });

  it('reports invalid JSON rather than throwing', () => {
    expect(parseOutputSchema('{oops').error).not.toBe('');
  });
});

describe('pythonCodeError', () => {
  it('is happy when the entrypoint is declared', () => {
    expect(pythonCodeError('def transform(inputs):\n    return 1\n', 'transform')).toBe('');
  });

  it('flags a mismatched function name', () => {
    expect(pythonCodeError('def run(inputs):\n    return 1\n', 'transform')).toMatch(/def transform/);
  });

  it('flags empty code', () => {
    expect(pythonCodeError('   ', 'transform')).toMatch(/Write a function/);
  });

  it('honours a renamed entrypoint', () => {
    expect(pythonCodeError('def prepare(inputs):\n    return 1\n', 'prepare')).toBe('');
  });
});

describe('parseImportList', () => {
  it('splits, trims and drops blanks', () => {
    expect(parseImportList(' json , math ,, re ')).toEqual(['json', 'math', 're']);
  });

  it('returns nothing for an empty box', () => {
    expect(parseImportList('')).toEqual([]);
  });
});
