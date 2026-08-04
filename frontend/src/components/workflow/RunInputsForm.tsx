import { useEffect, useState } from 'react';
import type { WorkflowPort } from '../../types/api';

interface RunInputsFormProps {
  ports: WorkflowPort[];
  value: Record<string, unknown>;
  onChange: (next: Record<string, unknown>) => void;
}

function isStructured(kind: string): boolean {
  return kind === 'json' || kind === 'list';
}

function placeholderFor(port: WorkflowPort): string {
  if (port.default !== null && port.default !== undefined) return `Default: ${JSON.stringify(port.default)}`;
  if (port.kind === 'list') return '[]';
  if (port.kind === 'json') return '{}';
  return '';
}

/** Structured values keep their own draft text so a half-typed object is not discarded. */
function StructuredField({ port, value, onCommit }: { port: WorkflowPort; value: unknown; onCommit: (next: unknown) => void }) {
  const [text, setText] = useState(() => (value === undefined ? '' : JSON.stringify(value, null, 2)));
  const [error, setError] = useState('');

  useEffect(() => {
    setText(value === undefined ? '' : JSON.stringify(value, null, 2));
  }, [value]);

  return (
    <>
      <textarea
        className="input json-inline"
        aria-label={port.label || port.key}
        placeholder={placeholderFor(port)}
        rows={4}
        value={text}
        onChange={(event) => setText(event.target.value)}
        onBlur={() => {
          if (!text.trim()) {
            setError('');
            onCommit(undefined);
            return;
          }
          try {
            onCommit(JSON.parse(text));
            setError('');
          } catch (err) {
            setError(err instanceof Error ? err.message : 'Invalid JSON');
          }
        }}
      />
      {error ? <span className="field-error">{error}</span> : null}
    </>
  );
}

export function RunInputsForm({ ports, value, onChange }: RunInputsFormProps) {
  const update = (key: string, next: unknown) => {
    const updated = { ...value };
    if (next === undefined || next === '') {
      delete updated[key];
    } else {
      updated[key] = next;
    }
    onChange(updated);
  };

  return (
    <div className="field-stack">
      {ports.map((port) => {
        const current = value[port.key];
        return (
          <div className="field-stack" key={port.key}>
            <label className="field-label">
              {port.label || port.key}
              {port.required ? <span className="required-dot">*</span> : null}
              <span className="tiny-tag">{port.kind}</span>
            </label>
            {port.description ? <p className="field-hint">{port.description}</p> : null}
            {isStructured(port.kind) ? (
              <StructuredField port={port} value={current} onCommit={(next) => update(port.key, next)} />
            ) : port.kind === 'number' ? (
              <input
                className="input"
                type="number"
                aria-label={port.label || port.key}
                placeholder={placeholderFor(port)}
                value={typeof current === 'number' ? current : ''}
                onChange={(event) =>
                  update(port.key, event.target.value === '' ? undefined : Number(event.target.value))
                }
              />
            ) : (
              <textarea
                className="input"
                rows={String(current ?? '').length > 80 ? 4 : 2}
                aria-label={port.label || port.key}
                placeholder={placeholderFor(port)}
                value={typeof current === 'string' ? current : current === undefined ? '' : JSON.stringify(current)}
                onChange={(event) => update(port.key, event.target.value)}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}
