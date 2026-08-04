import { useEffect, useMemo, useState } from 'react';
import { Icon } from '../common/Icon';
import { api } from '../../lib/api';
import { instructionPlaceholders, isChatModel, parseOutputSchema } from '../../lib/agentConfig';
import type { ModelInfo } from '../../types/api';

type ConfigValue = Record<string, unknown>;

interface Props {
  config: ConfigValue;
  onChange: (next: ConfigValue) => void;
  /** Names of the tools currently wired into this agent, for an at-a-glance check. */
  wiredTools: string[];
  wiredHandoffs: string[];
  /** The workflow modal owns the provider-aware Model tab. */
  hideModel?: boolean;
}

export function AgentNodeInspector({ config, onChange, wiredTools, wiredHandoffs, hideModel = false }: Props) {
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [schemaText, setSchemaText] = useState(() =>
    config.output_schema ? JSON.stringify(config.output_schema, null, 2) : '',
  );
  const [schemaError, setSchemaError] = useState('');

  useEffect(() => {
    if (hideModel) return undefined;
    let cancelled = false;
    api
      .listModels()
      .then((list) => {
        if (!cancelled) setModels(list.filter((model) => isChatModel(model.name)));
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [hideModel]);

  const set = (key: string, value: unknown) => onChange({ ...config, [key]: value });

  const placeholders = useMemo(() => instructionPlaceholders(String(config.instructions ?? '')), [config.instructions]);

  const applySchema = (text: string) => {
    setSchemaText(text);
    const { value, error } = parseOutputSchema(text);
    setSchemaError(error);
    if (!error) set('output_schema', value);
  };

  return (
    <div className="agent-inspector">
      <div className="field-stack">
        <label className="field-label">Agent name</label>
        <input
          className="input"
          value={String(config.name ?? '')}
          placeholder="Paper analyst"
          onChange={(event) => set('name', event.target.value)}
        />
      </div>

      <div className="field-stack">
        <label className="field-label">Instructions</label>
        <textarea
          className="input agent-instructions"
          rows={10}
          value={String(config.instructions ?? '')}
          placeholder="You answer questions about one research paper…"
          onChange={(event) => set('instructions', event.target.value)}
        />
        <p className="muted-text small">
          Write <code>{'{{input}}'}</code> or <code>{'{{context}}'}</code> to drop a wired value into the prompt.
          Any customer input works too, such as <code>{'{{document_id}}'}</code>.
        </p>
        {placeholders.length > 0 && (
          <div className="agent-placeholders">
            {placeholders.map((name) => (
              <span key={name} className="chip mono">
                {name}
              </span>
            ))}
          </div>
        )}
      </div>

      <div className="field-row">
        {!hideModel ? (
          <div className="field-stack">
            <label className="field-label">Model</label>
            <select className="input" value={String(config.model ?? '')} onChange={(event) => set('model', event.target.value || null)}>
              <option value="">Use the default from Settings</option>
              {models.map((model) => (
                <option key={model.name} value={model.name}>
                  {model.name}
                </option>
              ))}
            </select>
          </div>
        ) : null}
        <div className="field-stack">
          <label className="field-label">Max turns</label>
          <input
            className="input"
            type="number"
            min={1}
            max={50}
            value={config.max_turns === null || config.max_turns === undefined ? '' : Number(config.max_turns)}
            placeholder="Default"
            onChange={(event) => set('max_turns', event.target.value ? Number(event.target.value) : null)}
          />
        </div>
      </div>

      <div className="field-stack">
        <label className="field-label">Temperature</label>
        <input
          className="input"
          type="number"
          step="0.1"
          min={0}
          max={2}
          value={config.temperature === null || config.temperature === undefined ? '' : Number(config.temperature)}
          placeholder="Model default"
          onChange={(event) => set('temperature', event.target.value ? Number(event.target.value) : null)}
        />
      </div>

      <div className="agent-wiring">
        <div>
          <span className="field-label">Tools</span>
          {wiredTools.length ? (
            <div className="agent-placeholders">
              {wiredTools.map((name) => (
                <span key={name} className="chip mono">
                  {name}
                </span>
              ))}
            </div>
          ) : (
            <p className="muted-text small">
              Nothing wired. Connect a node's <strong>tool</strong> output to this agent's <strong>tools</strong> port
              to let it decide when to call that step.
            </p>
          )}
        </div>
        <div>
          <span className="field-label">Handoffs</span>
          {wiredHandoffs.length ? (
            <div className="agent-placeholders">
              {wiredHandoffs.map((name) => (
                <span key={name} className="chip mono">
                  {name}
                </span>
              ))}
            </div>
          ) : (
            <p className="muted-text small">
              Connect another agent's <strong>agent</strong> output here to let this one hand the work over.
            </p>
          )}
        </div>
      </div>

      <div className="field-stack">
        <label className="field-label">Handoff description</label>
        <input
          className="input"
          value={String(config.handoff_description ?? '')}
          placeholder="Hand over once the evidence is gathered."
          onChange={(event) => set('handoff_description', event.target.value)}
        />
        <p className="muted-text small">Shown to other agents so they know when to hand off to this one.</p>
      </div>

      <details className="agent-advanced">
        <summary>
          <Icon name="settings" /> Structured output
        </summary>
        <div className="field-stack">
          <label className="field-label">JSON schema</label>
          <textarea
            className="input mono"
            rows={6}
            value={schemaText}
            placeholder='{"type": "object", "properties": {"answer": {"type": "string"}}}'
            onChange={(event) => applySchema(event.target.value)}
          />
          {schemaError ? (
            <p className="field-error small">{schemaError}</p>
          ) : (
            <p className="muted-text small">
              Leave blank for plain text. With a schema set, the parsed value appears on the{' '}
              <strong>structured output</strong> port. Models are asked and re-asked rather than forced, so keep
              the schema small.
            </p>
          )}
        </div>
      </details>
    </div>
  );
}
