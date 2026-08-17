import { describe, expect, it } from 'vitest';

import { canHighlight, languageLabel, normalizeLanguage, tokenize } from './highlight';

const kindsOf = (code: string, language: string) =>
  tokenize(code, language).map((token) => `${token.kind}:${token.text}`);

describe('highlight', () => {
  it('resolves aliases to the language that actually has a grammar', () => {
    expect(normalizeLanguage('TS')).toBe('typescript');
    expect(normalizeLanguage('py')).toBe('python');
    expect(normalizeLanguage('sh')).toBe('bash');
    expect(canHighlight('jsx')).toBe(true);
    expect(canHighlight('brainfuck')).toBe(false);
    expect(languageLabel('py')).toBe('Python');
  });

  it('leaves unknown languages as a single plain run', () => {
    expect(tokenize('lorem ipsum', 'brainfuck')).toEqual([{ text: 'lorem ipsum', kind: 'plain' }]);
  });

  it('separates comments, strings, keywords and calls in TypeScript', () => {
    const tokens = kindsOf('// note\nconst name = greet("hi");', 'typescript');

    expect(tokens).toContain('comment:// note');
    expect(tokens).toContain('keyword:const');
    expect(tokens).toContain('function:greet');
    expect(tokens).toContain('string:"hi"');
  });

  it('keeps an unterminated string from swallowing the rest of the snippet', () => {
    const tokens = tokenize('x = "oops\nconst y = 1', 'typescript');

    expect(tokens.find((token) => token.kind === 'string')?.text).toBe('"oops');
    expect(tokens.some((token) => token.kind === 'keyword' && token.text === 'const')).toBe(true);
  });

  it('handles python triple-quoted strings and hash comments', () => {
    const tokens = kindsOf('def run():\n    """doc\n    still doc"""\n    # done\n    return None', 'python');

    expect(tokens).toContain('keyword:def');
    expect(tokens).toContain('function:run');
    expect(tokens).toContain('string:"""doc\n    still doc"""');
    expect(tokens).toContain('comment:# done');
    expect(tokens).toContain('constant:None');
  });

  it('matches SQL keywords regardless of case', () => {
    const tokens = kindsOf('select id from papers where year > 2020', 'sql');

    expect(tokens).toContain('keyword:select');
    expect(tokens).toContain('keyword:from');
    expect(tokens).toContain('number:2020');
  });

  it('marks JSON property keys apart from string values', () => {
    const tokens = tokenize('{"name": "ada", "count": 2}', 'json');

    expect(tokens.some((token) => token.kind === 'attr' && token.text === '"name"')).toBe(false);
    expect(tokens.some((token) => token.kind === 'string' && token.text === '"ada"')).toBe(true);
    expect(tokens.some((token) => token.kind === 'number' && token.text === '2')).toBe(true);
  });

  it('colours diffs by line', () => {
    const tokens = tokenize('@@ -1 +1 @@\n-old\n+new\n same\n', 'diff');

    expect(tokens[0].kind).toBe('comment');
    expect(tokens[1]).toEqual({ text: '-old\n', kind: 'deleted' });
    expect(tokens[2]).toEqual({ text: '+new\n', kind: 'inserted' });
    expect(tokens[3].kind).toBe('plain');
  });

  it('separates tags, attributes and values in markup', () => {
    const tokens = kindsOf('<a href="/x" class=\'y\'>text</a><!-- hi -->', 'html');

    expect(tokens).toContain('tag:<a');
    expect(tokens).toContain('attr:href=');
    expect(tokens).toContain('string:"/x"');
    expect(tokens).toContain('tag:</a>');
    expect(tokens).toContain('comment:<!-- hi -->');
  });

  it('preserves the original text exactly', () => {
    const code = 'def f(x):\n    # keep\n    return x * 2  # trailing\n';
    expect(tokenize(code, 'python').map((token) => token.text).join('')).toBe(code);
  });
});
