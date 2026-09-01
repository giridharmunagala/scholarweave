/**
 * A small, dependency-free syntax highlighter.
 *
 * Model answers are mostly prose with short code fences, so a full grammar
 * engine would cost far more than it returns. This tokeniser recognises the
 * shapes that actually carry meaning in a snippet — comments, strings,
 * numbers, keywords, call sites — and leaves everything else as plain text.
 * Colours are applied by CSS through the `--syn-*` tokens, so highlighting
 * follows whichever theme is active.
 */

export type TokenKind =
  | 'plain'
  | 'comment'
  | 'string'
  | 'number'
  | 'keyword'
  | 'type'
  | 'constant'
  | 'function'
  | 'operator'
  | 'punct'
  | 'tag'
  | 'attr'
  | 'inserted'
  | 'deleted';

export interface Token {
  text: string;
  kind: TokenKind;
}

interface LanguageSpec {
  lineComments: string[];
  blockComments: Array<[string, string]>;
  /** Quote characters that open a string literal. */
  quotes: string[];
  /** Triple-quoted or otherwise multi-line string delimiters. */
  longStrings: string[];
  keywords: Set<string>;
  types: Set<string>;
  constants: Set<string>;
  /** Identifiers that look like `name(` are rendered as calls. */
  callSites: boolean;
  /** Highlights `word:` as a property, which suits JSON, YAML and CSS. */
  propertyKeys: boolean;
  caseInsensitiveKeywords: boolean;
}

const words = (value: string): Set<string> => new Set(value.split(/\s+/).filter(Boolean));

const BASE: LanguageSpec = {
  lineComments: ['//'],
  blockComments: [['/*', '*/']],
  quotes: ['"', "'", '`'],
  longStrings: [],
  keywords: new Set(),
  types: new Set(),
  constants: new Set(),
  callSites: true,
  propertyKeys: false,
  caseInsensitiveKeywords: false,
};

const JS_KEYWORDS =
  'as async await break case catch class const continue debugger default delete do else export extends finally for from function get if implements import in instanceof interface let new of package private protected public return satisfies set static super switch this throw try typeof var void while with yield declare namespace enum abstract readonly keyof infer';

const JS: LanguageSpec = {
  ...BASE,
  keywords: words(JS_KEYWORDS),
  types: words(
    'string number boolean object symbol bigint any unknown never void undefined Array Promise Record Map Set Date RegExp Error JSON Math Object String Number Boolean',
  ),
  constants: words('true false null undefined NaN Infinity this super globalThis console window document'),
};

const PYTHON: LanguageSpec = {
  ...BASE,
  lineComments: ['#'],
  blockComments: [],
  quotes: ['"', "'"],
  longStrings: ['"""', "'''"],
  keywords: words(
    'and as assert async await break class continue def del elif else except finally for from global if import in is lambda match case nonlocal not or pass raise return try while with yield',
  ),
  types: words(
    'int float str bool bytes list dict set tuple frozenset complex object type Any Optional Union Callable Iterable Sequence Mapping List Dict Set Tuple',
  ),
  constants: words('True False None self cls __name__ __main__ NotImplemented Ellipsis'),
};

const SHELL: LanguageSpec = {
  ...BASE,
  lineComments: ['#'],
  blockComments: [],
  quotes: ['"', "'", '`'],
  keywords: words(
    'if then elif else fi for while until do done case esac function in select return break continue export local readonly source alias unset shift trap exec eval set',
  ),
  types: words('echo printf cd ls cat grep sed awk find curl wget git npm pip python node docker kubectl make sudo mkdir rm cp mv chmod chown tar ssh'),
  constants: words('true false'),
  callSites: false,
};

const SQL: LanguageSpec = {
  ...BASE,
  lineComments: ['--'],
  quotes: ['"', "'"],
  keywords: words(
    'select from where group by order having limit offset insert into values update set delete create table view index alter drop add column primary key foreign references join left right inner outer full cross on as union all distinct case when then else end with recursive returning and or not in exists between like ilike is asc desc using default constraint unique check cascade begin commit rollback',
  ),
  types: words('int integer bigint smallint serial text varchar char boolean date timestamp timestamptz numeric decimal real double precision json jsonb uuid array bytea'),
  constants: words('true false null current_timestamp current_date'),
  caseInsensitiveKeywords: true,
};

const CSS_SPEC: LanguageSpec = {
  ...BASE,
  lineComments: [],
  quotes: ['"', "'"],
  keywords: words(
    'important media supports keyframes import charset font-face from to and not only screen print var calc url linear-gradient radial-gradient color-mix rgb rgba hsl hsla clamp min max',
  ),
  constants: words('inherit initial unset revert none auto transparent currentColor'),
  callSites: false,
  propertyKeys: true,
};

const JSON_SPEC: LanguageSpec = {
  ...BASE,
  lineComments: [],
  blockComments: [],
  quotes: ['"'],
  keywords: new Set(),
  constants: words('true false null'),
  callSites: false,
  propertyKeys: true,
};

const YAML_SPEC: LanguageSpec = {
  ...BASE,
  lineComments: ['#'],
  blockComments: [],
  quotes: ['"', "'"],
  constants: words('true false null yes no on off ~'),
  callSites: false,
  propertyKeys: true,
};

const GO: LanguageSpec = {
  ...BASE,
  quotes: ['"', '`', "'"],
  keywords: words(
    'break case chan const continue default defer else fallthrough for func go goto if import interface map package range return select struct switch type var',
  ),
  types: words('bool string int int8 int16 int32 int64 uint uintptr byte rune float32 float64 complex64 complex128 error any'),
  constants: words('true false nil iota'),
};

const RUST: LanguageSpec = {
  ...BASE,
  quotes: ['"', "'"],
  keywords: words(
    'as async await break const continue crate dyn else enum extern fn for if impl in let loop match mod move mut pub ref return self Self static struct super trait type unsafe use where while',
  ),
  types: words('bool char str String i8 i16 i32 i64 i128 isize u8 u16 u32 u64 u128 usize f32 f64 Vec Option Result Box Rc Arc HashMap HashSet'),
  constants: words('true false None Some Ok Err'),
};

const JAVA_LIKE: LanguageSpec = {
  ...BASE,
  keywords: words(
    'abstract assert break case catch class const continue default do else enum extends final finally for goto if implements import instanceof interface native new package private protected public return static strictfp super switch synchronized this throw throws transient try volatile while var record sealed yield',
  ),
  types: words('boolean byte char double float int long short void String Object List Map Set Optional Stream Integer Double Boolean Long'),
  constants: words('true false null this super'),
};

const C_LIKE: LanguageSpec = {
  ...BASE,
  keywords: words(
    'auto break case const continue default do else enum extern for goto if inline register restrict return sizeof static struct switch typedef union volatile while class namespace template typename using public private protected virtual override new delete this operator constexpr noexcept nullptr',
  ),
  types: words('bool char double float int long short signed unsigned void size_t uint8_t uint16_t uint32_t uint64_t int8_t int16_t int32_t int64_t string vector map set auto'),
  constants: words('true false NULL nullptr'),
};

const ALIASES: Record<string, string> = {
  js: 'javascript',
  jsx: 'javascript',
  mjs: 'javascript',
  cjs: 'javascript',
  node: 'javascript',
  ts: 'typescript',
  tsx: 'typescript',
  py: 'python',
  python3: 'python',
  sh: 'bash',
  shell: 'bash',
  zsh: 'bash',
  console: 'bash',
  terminal: 'bash',
  psql: 'sql',
  postgres: 'sql',
  postgresql: 'sql',
  mysql: 'sql',
  sqlite: 'sql',
  yml: 'yaml',
  golang: 'go',
  rs: 'rust',
  kt: 'java',
  kotlin: 'java',
  cs: 'java',
  csharp: 'java',
  'c++': 'cpp',
  cxx: 'cpp',
  cc: 'cpp',
  h: 'cpp',
  hpp: 'cpp',
  scss: 'css',
  less: 'css',
  htm: 'html',
  xml: 'html',
  svg: 'html',
  vue: 'html',
  jsonc: 'json',
  json5: 'json',
  patch: 'diff',
};

const SPECS: Record<string, LanguageSpec> = {
  javascript: JS,
  typescript: JS,
  python: PYTHON,
  bash: SHELL,
  sql: SQL,
  css: CSS_SPEC,
  json: JSON_SPEC,
  yaml: YAML_SPEC,
  go: GO,
  rust: RUST,
  java: JAVA_LIKE,
  c: C_LIKE,
  cpp: C_LIKE,
};

/** Languages this module can add colour to; anything else renders as plain text. */
export function canHighlight(language: string | null | undefined): boolean {
  const id = normalizeLanguage(language);
  return id === 'html' || id === 'diff' || id in SPECS;
}

export function normalizeLanguage(language: string | null | undefined): string {
  const raw = (language ?? '').trim().toLowerCase();
  if (!raw) return '';
  return ALIASES[raw] ?? raw;
}

/** Human label for the chip shown on a code block. */
export function languageLabel(language: string | null | undefined): string {
  const id = normalizeLanguage(language);
  const LABELS: Record<string, string> = {
    javascript: 'JavaScript',
    typescript: 'TypeScript',
    python: 'Python',
    bash: 'Shell',
    sql: 'SQL',
    css: 'CSS',
    html: 'HTML',
    json: 'JSON',
    yaml: 'YAML',
    go: 'Go',
    rust: 'Rust',
    java: 'Java',
    c: 'C',
    cpp: 'C++',
    diff: 'Diff',
  };
  if (LABELS[id]) return LABELS[id];
  return id ? id.toUpperCase() : 'Text';
}

const IDENT = /[A-Za-z_$][\w$]*/y;
const NUMBER = /(?:0[xXbBoO][0-9a-fA-F_]+|\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?)[a-zA-Z%]*/y;

export function tokenize(code: string, language: string | null | undefined): Token[] {
  const id = normalizeLanguage(language);
  if (id === 'diff') return tokenizeDiff(code);
  if (id === 'html') return tokenizeMarkup(code);
  const spec = SPECS[id];
  if (!spec) return [{ text: code, kind: 'plain' }];
  return merge(tokenizeGeneric(code, spec));
}

function tokenizeGeneric(code: string, spec: LanguageSpec): Token[] {
  const out: Token[] = [];
  const push = (text: string, kind: TokenKind) => {
    if (text) out.push({ text, kind });
  };
  let i = 0;

  while (i < code.length) {
    const rest = code.slice(i);

    const long = spec.longStrings.find((delim) => rest.startsWith(delim));
    if (long) {
      const close = code.indexOf(long, i + long.length);
      const end = close === -1 ? code.length : close + long.length;
      push(code.slice(i, end), 'string');
      i = end;
      continue;
    }

    const block = spec.blockComments.find(([open]) => rest.startsWith(open));
    if (block) {
      const close = code.indexOf(block[1], i + block[0].length);
      const end = close === -1 ? code.length : close + block[1].length;
      push(code.slice(i, end), 'comment');
      i = end;
      continue;
    }

    const line = spec.lineComments.find((open) => rest.startsWith(open));
    if (line) {
      const newline = code.indexOf('\n', i);
      const end = newline === -1 ? code.length : newline;
      push(code.slice(i, end), 'comment');
      i = end;
      continue;
    }

    const quote = spec.quotes.find((mark) => rest.startsWith(mark));
    if (quote) {
      const end = findStringEnd(code, i + quote.length, quote);
      push(code.slice(i, end), 'string');
      i = end;
      continue;
    }

    const char = code[i];

    if (/\d/.test(char) || (char === '.' && /\d/.test(code[i + 1] ?? ''))) {
      NUMBER.lastIndex = i;
      const match = NUMBER.exec(code);
      if (match) {
        push(match[0], 'number');
        i += match[0].length;
        continue;
      }
    }

    if (/[A-Za-z_$@#-]/.test(char)) {
      IDENT.lastIndex = /[@#-]/.test(char) ? i + 1 : i;
      const match = IDENT.exec(code);
      if (match) {
        const start = /[@#-]/.test(char) ? i : match.index;
        const word = code.slice(start, match.index + match[0].length);
        push(word, classifyWord(word, code, match.index + match[0].length, spec));
        i = start + word.length;
        continue;
      }
    }

    if (/[+\-*/%=<>!&|^~?:]/.test(char)) {
      push(char, 'operator');
      i += 1;
      continue;
    }

    if (/[()[\]{},;.]/.test(char)) {
      push(char, 'punct');
      i += 1;
      continue;
    }

    push(char, 'plain');
    i += 1;
  }

  return out;
}

function classifyWord(word: string, code: string, end: number, spec: LanguageSpec): TokenKind {
  const lookup = spec.caseInsensitiveKeywords ? word.toLowerCase() : word;
  if (spec.constants.has(lookup)) return 'constant';
  if (spec.keywords.has(lookup)) return 'keyword';
  if (spec.types.has(lookup)) return 'type';

  const next = code.slice(end).match(/^\s*(.)/)?.[1];
  if (spec.propertyKeys && next === ':') return 'attr';
  if (spec.callSites && next === '(') return 'function';
  // Conventional casing carries real information: Types are capitalised,
  // CONSTANTS are shouted.
  if (/^[A-Z][A-Z0-9_]*$/.test(word) && word.length > 1) return 'constant';
  if (/^[A-Z]/.test(word)) return 'type';
  return 'plain';
}

function findStringEnd(code: string, from: number, quote: string): number {
  let i = from;
  while (i < code.length) {
    const char = code[i];
    if (char === '\\') {
      i += 2;
      continue;
    }
    if (char === quote) return i + 1;
    // An unterminated single-line string should not swallow the rest of the file.
    if (char === '\n' && quote !== '`') return i;
    i += 1;
  }
  return code.length;
}

function tokenizeMarkup(code: string): Token[] {
  const out: Token[] = [];
  const pattern = /<!--[\s\S]*?-->|<\/?[A-Za-z][\w:-]*|\/?>|"[^"]*"|'[^']*'|[A-Za-z_:][\w:.-]*=/g;
  let cursor = 0;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(code))) {
    if (match.index > cursor) out.push({ text: code.slice(cursor, match.index), kind: 'plain' });
    const text = match[0];
    if (text.startsWith('<!--')) out.push({ text, kind: 'comment' });
    else if (text.startsWith('<') || text === '>' || text === '/>') out.push({ text, kind: 'tag' });
    else if (text.startsWith('"') || text.startsWith("'")) out.push({ text, kind: 'string' });
    else out.push({ text, kind: 'attr' });
    cursor = match.index + text.length;
  }

  if (cursor < code.length) out.push({ text: code.slice(cursor), kind: 'plain' });
  return merge(out);
}

function tokenizeDiff(code: string): Token[] {
  return code.split(/(?<=\n)/).map((line) => {
    if (/^\+\+\+|^---|^diff |^index |^@@/.test(line)) return { text: line, kind: 'comment' as TokenKind };
    if (line.startsWith('+')) return { text: line, kind: 'inserted' as TokenKind };
    if (line.startsWith('-')) return { text: line, kind: 'deleted' as TokenKind };
    return { text: line, kind: 'plain' as TokenKind };
  });
}

function merge(tokens: Token[]): Token[] {
  const out: Token[] = [];
  for (const token of tokens) {
    const last = out[out.length - 1];
    if (last && last.kind === token.kind) last.text += token.text;
    else out.push({ ...token });
  }
  return out;
}
