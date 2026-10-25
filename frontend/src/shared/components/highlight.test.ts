import { describe, expect, it } from 'vitest';

import { tokenize } from './highlight';

describe('highlight', () => {
  it('leaves unknown languages as a single plain run', () => {
    expect(tokenize('lorem ipsum', 'brainfuck')).toEqual([{ text: 'lorem ipsum', kind: 'plain' }]);
  });

  it('keeps an unterminated string from swallowing the rest of the snippet', () => {
    const tokens = tokenize('x = "oops\nconst y = 1', 'typescript');

    expect(tokens.find((token) => token.kind === 'string')?.text).toBe('"oops');
    expect(tokens.some((token) => token.kind === 'keyword' && token.text === 'const')).toBe(true);
  });

  it('preserves the original text exactly', () => {
    const code = 'def f(x):\n    # keep\n    return x * 2  # trailing\n';
    expect(tokenize(code, 'python').map((token) => token.text).join('')).toBe(code);
  });
});
