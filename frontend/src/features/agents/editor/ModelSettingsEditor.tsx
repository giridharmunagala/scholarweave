import type { AgentSpec } from '../types';
import { OptionalNumber } from './InspectorFields';

type ModelSettings = NonNullable<AgentSpec['model_settings']>;

export function ModelSettingsEditor({
  settings,
  onChange,
}: {
  settings: ModelSettings;
  onChange: (patch: Partial<ModelSettings>) => void;
}) {
  return (
    <details className="inspector-section">
      <summary>Model settings</summary>
      <div className="stack">
        <div className="field-row">
          <OptionalNumber label="Temperature" value={settings.temperature} min={0} max={2} step={0.1} onChange={(value) => onChange({ temperature: value })} />
          <OptionalNumber label="Top P" value={settings.top_p} min={0} max={1} step={0.05} onChange={(value) => onChange({ top_p: value })} />
        </div>
        <div className="field-row">
          <OptionalNumber label="Maximum tokens" value={settings.max_tokens} min={1} onChange={(value) => onChange({ max_tokens: value })} />
          <label className="field">
            Tool choice
            <input value={settings.tool_choice ?? ''} placeholder="auto, required, none, or tool name" onChange={(event) => onChange({ tool_choice: event.target.value || null })} />
          </label>
        </div>
        <div className="field-row">
          <label className="field">
            Parallel tool calls
            <select value={settings.parallel_tool_calls == null ? '' : String(settings.parallel_tool_calls)} onChange={(event) => onChange({ parallel_tool_calls: event.target.value === '' ? null : event.target.value === 'true' })}>
              <option value="">Provider default</option>
              <option value="true">Enabled</option>
              <option value="false">Disabled</option>
            </select>
          </label>
          <label className="field">
            Truncation
            <select value={settings.truncation ?? ''} onChange={(event) => onChange({ truncation: event.target.value === '' ? null : event.target.value as ModelSettings['truncation'] })}>
              <option value="">Provider default</option>
              <option value="auto">Auto</option>
              <option value="disabled">Disabled</option>
            </select>
          </label>
        </div>
        <label className="field">
          Verbosity
          <select value={settings.verbosity ?? ''} onChange={(event) => onChange({ verbosity: event.target.value === '' ? null : event.target.value as ModelSettings['verbosity'] })}>
            <option value="">Provider default</option>
            <option value="low">Low</option>
            <option value="medium">Medium</option>
            <option value="high">High</option>
          </select>
        </label>
      </div>
    </details>
  );
}
