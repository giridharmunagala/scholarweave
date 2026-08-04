import { Icon } from '../common/Icon';
import { parseImportList, pythonCodeError } from '../../lib/agentConfig';

type ConfigValue = Record<string, unknown>;

interface Props {
  config: ConfigValue;
  onChange: (next: ConfigValue) => void;
  /** Output ports feeding this node, so the code can be written against real names. */
  incomingNames: string[];
  allowedImports: string[];
  enabled: boolean;
}

export function PythonNodeInspector({ config, onChange, incomingNames, allowedImports, enabled }: Props) {
  const set = (key: string, value: unknown) => onChange({ ...config, [key]: value });
  const code = String(config.code ?? '');
  const entrypoint = String(config.entrypoint ?? 'transform');
  const codeError = pythonCodeError(code, entrypoint);

  return (
    <div className="python-inspector">
      {!enabled && (
        <p className="field-error small">
          Python nodes are switched off in Settings, so this node will fail when the agent runs.
        </p>
      )}

      <div className="field-stack">
        <label className="field-label">Code</label>
        <textarea
          className="input mono code-editor"
          rows={16}
          spellCheck={false}
          value={code}
          onChange={(event) => set('code', event.target.value)}
        />
        {codeError ? <p className="field-error small">{codeError}</p> : null}
      </div>

      <div className="python-inputs">
        <span className="field-label">Available in inputs</span>
        <div className="agent-placeholders">
          {incomingNames.map((name) => (
            <span key={name} className="chip mono">
              inputs[&quot;{name}&quot;]
            </span>
          ))}
          <span className="chip mono">inputs[&quot;workflow&quot;]</span>
        </div>
        <p className="muted-text small">
          Each wired value arrives under the name of the port it came from. Whatever you return becomes this node&apos;s{' '}
          <strong>value</strong> output — return a list if the next step maps over it.
        </p>
      </div>

      <div className="field-row">
        <div className="field-stack">
          <label className="field-label">Function name</label>
          <input className="input mono" value={entrypoint} onChange={(event) => set('entrypoint', event.target.value)} />
        </div>
        <div className="field-stack">
          <label className="field-label">Timeout (seconds)</label>
          <input
            className="input"
            type="number"
            min={1}
            max={300}
            placeholder="Default"
            value={config.timeout_seconds === null || config.timeout_seconds === undefined ? '' : Number(config.timeout_seconds)}
            onChange={(event) => set('timeout_seconds', event.target.value ? Number(event.target.value) : null)}
          />
        </div>
      </div>

      <details className="agent-advanced">
        <summary>
          <Icon name="settings" /> Use as an agent tool
        </summary>
        <div className="field-stack">
          <label className="field-label">Tool name</label>
          <input
            className="input mono"
            value={String(config.tool_name ?? 'run_python_step')}
            onChange={(event) => set('tool_name', event.target.value)}
          />
        </div>
        <div className="field-stack">
          <label className="field-label">Tool description</label>
          <textarea
            className="input"
            rows={3}
            value={String(config.tool_description ?? '')}
            placeholder="Counts how often each term appears. Pass {&quot;text&quot;: ..., &quot;terms&quot;: [...]}."
            onChange={(event) => set('tool_description', event.target.value)}
          />
          <p className="muted-text small">
            Wire the <strong>tool</strong> output into an agent to let it call this code. The description is how the
            model decides when to use it, so say what it does and what arguments it wants.
          </p>
        </div>
        <div className="field-stack">
          <label className="field-label">Memory limit (MB)</label>
          <input
            className="input"
            type="number"
            min={32}
            max={8192}
            placeholder="Default"
            value={config.memory_mb === null || config.memory_mb === undefined ? '' : Number(config.memory_mb)}
            onChange={(event) => set('memory_mb', event.target.value ? Number(event.target.value) : null)}
          />
        </div>
      </details>

      <div className="python-limits">
        <span className="field-label">Imports allowed</span>
        <p className="muted-text small mono">{allowedImports.join(', ') || 'None'}</p>
        <p className="muted-text small">
          The code runs in a separate process with no network access and no reach outside a scratch folder. That stops
          mistakes, not a determined attacker — only run code you trust.
        </p>
      </div>
    </div>
  );
}
