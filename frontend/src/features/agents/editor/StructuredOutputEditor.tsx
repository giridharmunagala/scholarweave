import { useEffect, useState } from 'react';
import type { AgentSpec } from '../types';

export function StructuredOutputEditor({
  output,
  onChange,
}: {
  output: AgentSpec['output'];
  onChange: (output: AgentSpec['output']) => void;
}) {
  const [schemaText, setSchemaText] = useState(() =>
    output ? JSON.stringify(output.schema, null, 2) : '',
  );
  const [schemaError, setSchemaError] = useState<string | null>(null);

  useEffect(() => {
    setSchemaText(output ? JSON.stringify(output.schema, null, 2) : '');
    setSchemaError(null);
  }, [output?.name, output?.schema]);

  const enable = () =>
    onChange({
      kind: 'json_schema',
      name: 'structured_output',
      schema: {
        type: 'object',
        properties: {},
        additionalProperties: false,
      },
      strict: true,
    });
  const applySchema = () => {
    if (!output) return;
    try {
      const parsed: unknown = JSON.parse(schemaText);
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        setSchemaError('The output schema must be a JSON object.');
        return;
      }
      setSchemaError(null);
      onChange({ ...output, schema: Object.fromEntries(Object.entries(parsed)) });
    } catch (error) {
      setSchemaError(error instanceof Error ? error.message : 'Invalid JSON schema.');
    }
  };

  return (
    <details className="inspector-section" open={Boolean(output)}>
      <summary>Structured output</summary>
      {output ? (
        <div className="stack">
          <div className="inspector-heading">
            <span className="muted">Agent.output_type</span>
            <button className="button secondary small" type="button" onClick={() => onChange(null)}>Disable</button>
          </div>
          <label className="field">Schema name<input value={output.name} onChange={(event) => onChange({ ...output, name: event.target.value })} /></label>
          <label className="check-row"><input type="checkbox" checked={output.strict} onChange={(event) => onChange({ ...output, strict: event.target.checked })} />Strict JSON Schema</label>
          <label className="field">JSON Schema<textarea className="schema-editor" value={schemaText} onChange={(event) => setSchemaText(event.target.value)} onBlur={applySchema} /></label>
          {schemaError ? <p className="field-error">{schemaError}</p> : null}
        </div>
      ) : (
        <button className="button secondary" type="button" onClick={enable}>Enable JSON Schema output</button>
      )}
    </details>
  );
}
