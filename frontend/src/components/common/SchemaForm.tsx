import type { ChangeEvent } from 'react';
import { useEffect, useState } from 'react';

interface JsonSchemaProperty {
  type?: string | string[];
  title?: string;
  description?: string;
  enum?: Array<string | number>;
  default?: unknown;
}

interface SchemaFormProps {
  schema: Record<string, unknown>;
  value: Record<string, unknown>;
  onChange: (value: Record<string, unknown>) => void;
}

function normalizeType(property: JsonSchemaProperty): string {
  if (Array.isArray(property.type)) {
    return property.type.find((type) => type !== 'null') || 'string';
  }
  return property.type || (property.enum ? 'string' : 'object');
}

function FieldJsonEditor({ name, value, onCommit }: { name: string; value: unknown; onCommit: (value: unknown) => void }) {
  const [text, setText] = useState(JSON.stringify(value ?? {}, null, 2));
  const [error, setError] = useState('');

  useEffect(() => {
    setText(JSON.stringify(value ?? {}, null, 2));
  }, [value]);

  return (
    <div className="field-stack">
      <textarea
        className="input json-inline"
        aria-label={name}
        value={text}
        onChange={(event) => setText(event.target.value)}
        onBlur={() => {
          try {
            const parsed = JSON.parse(text || 'null');
            setError('');
            onCommit(parsed);
          } catch (err) {
            setError(err instanceof Error ? err.message : 'Invalid JSON');
          }
        }}
      />
      {error ? <span className="field-error">{error}</span> : null}
    </div>
  );
}

export function SchemaForm({ schema, value, onChange }: SchemaFormProps) {
  const properties = (schema.properties as Record<string, JsonSchemaProperty> | undefined) ?? {};
  const required = new Set((schema.required as string[] | undefined) ?? []);

  const update = (name: string, next: unknown) => {
    const updated = { ...value };
    if (next === '' || next == null) {
      delete updated[name];
    } else {
      updated[name] = next;
    }
    onChange(updated);
  };

  return (
    <div className="field-stack">
      {Object.entries(properties).length === 0 ? <p className="empty-state inline">This node has no typed configuration fields.</p> : null}
      {Object.entries(properties).map(([name, property]) => {
        const propertyType = normalizeType(property);
        const current = value[name] ?? property.default ?? (propertyType === 'boolean' ? false : '');
        const label = property.title || name;

        const input = (() => {
          if (property.enum?.length) {
            return (
              <select className="input" value={String(current ?? '')} onChange={(event) => update(name, event.target.value)}>
                <option value="">Select…</option>
                {property.enum.map((option) => (
                  <option key={String(option)} value={String(option)}>
                    {String(option)}
                  </option>
                ))}
              </select>
            );
          }
          if (propertyType === 'boolean') {
            return (
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={Boolean(current)}
                  onChange={(event) => update(name, event.target.checked)}
                />
                <span>Enabled</span>
              </label>
            );
          }
          if (propertyType === 'integer' || propertyType === 'number') {
            return (
              <input
                className="input"
                type="number"
                value={current === '' ? '' : Number(current)}
                onChange={(event: ChangeEvent<HTMLInputElement>) =>
                  update(name, event.target.value === '' ? '' : propertyType === 'integer' ? Number.parseInt(event.target.value, 10) : Number.parseFloat(event.target.value))
                }
              />
            );
          }
          if (propertyType === 'string') {
            return (
              <textarea
                className="input"
                rows={String(current).includes('\n') || String(current).length > 100 ? 5 : 2}
                value={String(current ?? '')}
                onChange={(event) => update(name, event.target.value)}
              />
            );
          }
          return <FieldJsonEditor name={name} value={current} onCommit={(next) => update(name, next)} />;
        })();

        return (
          <div className="field-stack" key={name}>
            <label className="field-label">
              {label}
              {required.has(name) ? <span className="required-dot">*</span> : null}
            </label>
            {property.description ? <p className="field-hint">{property.description}</p> : null}
            {input}
          </div>
        );
      })}
    </div>
  );
}
