import { useEffect, useState } from 'react';
import { prettyJson } from '../../lib/json';

interface JsonEditorProps {
  label: string;
  value: unknown;
  onApply: (value: Record<string, unknown>) => void;
  placeholder?: string;
  height?: 'sm' | 'md' | 'lg';
}

const heights = {
  sm: '9rem',
  md: '14rem',
  lg: '20rem',
};

export function JsonEditor({ label, value, onApply, placeholder = '{}', height = 'md' }: JsonEditorProps) {
  const [text, setText] = useState(prettyJson(value));
  const [error, setError] = useState('');

  useEffect(() => {
    setText(prettyJson(value));
  }, [value]);

  const apply = () => {
    try {
      const parsed = JSON.parse(text || '{}');
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        throw new Error('Expected a JSON object');
      }
      setError('');
      onApply(parsed as Record<string, unknown>);
      setText(prettyJson(parsed));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Invalid JSON');
    }
  };

  return (
    <div className="field-stack">
      <div className="field-header-row">
        <label className="field-label">{label}</label>
        <div className="button-row compact">
          <button type="button" className="button subtle" onClick={() => setText(prettyJson(value))}>
            Reset
          </button>
          <button
            type="button"
            className="button subtle"
            onClick={() => {
              try {
                setText(prettyJson(JSON.parse(text || '{}')));
                setError('');
              } catch (err) {
                setError(err instanceof Error ? err.message : 'Invalid JSON');
              }
            }}
          >
            Format
          </button>
          <button type="button" className="button" onClick={apply}>
            Apply
          </button>
        </div>
      </div>
      <textarea
        aria-label={label}
        className="json-editor"
        style={{ minHeight: heights[height] }}
        value={text}
        placeholder={placeholder}
        onChange={(event) => setText(event.target.value)}
      />
      {error ? <p className="field-error">{error}</p> : null}
    </div>
  );
}
