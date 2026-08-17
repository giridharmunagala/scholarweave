import { useState } from 'react';
import { Icon } from './Icons';
import { canHighlight, languageLabel, tokenize } from './highlight';

/**
 * A fenced code block presented as an artefact: labelled by language, liftable
 * with one click, and syntax-coloured through the active theme's palette.
 */
export function CodeBlock({ code, language }: { code: string; language: string }) {
  const [copied, setCopied] = useState(false);
  const [wrapped, setWrapped] = useState(false);
  const body = code.replace(/\n$/, '');
  const lines = body.split('\n').length;

  const copy = async () => {
    try {
      await writeClipboard(body);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div className="code-card" data-language={language || 'text'}>
      <div className="code-card-head">
        <span className="code-card-lang">{languageLabel(language)}</span>
        <span className="code-card-lines">{lines === 1 ? '1 line' : `${lines} lines`}</span>
        <span className="spacer" />
        <button
          type="button"
          className={wrapped ? 'active' : undefined}
          title={wrapped ? 'Stop wrapping long lines' : 'Wrap long lines'}
          aria-pressed={wrapped}
          aria-label="Toggle line wrapping"
          onClick={() => setWrapped((value) => !value)}
        >
          <Icon name="menu" size={14} />
        </button>
        <button
          type="button"
          title={copied ? 'Copied' : 'Copy code'}
          aria-label="Copy code"
          onClick={() => void copy()}
        >
          <Icon name={copied ? 'check' : 'copy'} size={14} />
        </button>
      </div>
      <pre className={`code-card-body${wrapped ? ' wrapped' : ''}`}>
        <code>{renderTokens(body, language)}</code>
      </pre>
    </div>
  );
}

function renderTokens(code: string, language: string) {
  if (!canHighlight(language)) return code;
  return tokenize(code, language).map((token, index) =>
    token.kind === 'plain' ? (
      token.text
    ) : (
      <span className={`tok tok-${token.kind}`} key={index}>
        {token.text}
      </span>
    ),
  );
}

async function writeClipboard(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand('copy');
  textarea.remove();
  if (!copied) throw new Error('The browser denied clipboard access.');
}
