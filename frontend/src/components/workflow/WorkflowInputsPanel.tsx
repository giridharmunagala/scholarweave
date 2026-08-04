import { useState } from 'react';

import { Icon } from '../common/Icon';
import {
  WORKFLOW_INPUT_KINDS,
  type DeclaredWorkflowInput,
  type WorkflowInputKind,
} from '../../lib/workflowInputs';

export interface WorkflowInputPatch {
  key?: string;
  kind?: WorkflowInputKind;
  label?: string;
  description?: string;
  required?: boolean;
  default?: unknown;
}

export interface NewWorkflowInput {
  key: string;
  kind: WorkflowInputKind;
  label: string;
}

interface WorkflowInputsPanelProps {
  inputs: DeclaredWorkflowInput[];
  /** Applies to every node declaring the key, so one edit keeps the graph consistent. */
  onPatch: (key: string, patch: WorkflowInputPatch) => void;
  onRemove: (key: string) => void;
  onAdd: (input: NewWorkflowInput) => void;
  onFocusNode?: (nodeId: string) => void;
}

function slugifyKey(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '');
}

function defaultText(value: unknown): string {
  if (value === undefined || value === null) return '';
  return typeof value === 'string' ? value : JSON.stringify(value);
}

function parseDefault(text: string, kind: WorkflowInputKind): unknown {
  const trimmed = text.trim();
  if (!trimmed) return undefined;
  if (kind === 'text') return text;
  if (kind === 'number') {
    const parsed = Number(trimmed);
    return Number.isFinite(parsed) ? parsed : text;
  }
  try {
    return JSON.parse(trimmed);
  } catch {
    return text;
  }
}

export function WorkflowInputsPanel({ inputs, onPatch, onRemove, onAdd, onFocusNode }: WorkflowInputsPanelProps) {
  const [expanded, setExpanded] = useState<string>('');
  const [adding, setAdding] = useState(false);
  const [draftKey, setDraftKey] = useState('');
  const [draftKind, setDraftKind] = useState<WorkflowInputKind>('text');

  const existingKeys = new Set(inputs.map((input) => input.key));
  const draftSlug = slugifyKey(draftKey);
  const draftInvalid = !draftSlug || existingKeys.has(draftSlug);

  const submitDraft = () => {
    if (draftInvalid) return;
    onAdd({ key: draftSlug, kind: draftKind, label: draftKey.trim() || draftSlug });
    setDraftKey('');
    setDraftKind('text');
    setAdding(false);
  };

  return (
    <div className="stack gap-sm workflow-inputs-panel">
      <div className="panel-subheader">
        <h3>Customer inputs</h3>
        {inputs.length > 0 ? <span className="tiny-tag">{inputs.length}</span> : null}
      </div>
      <p className="muted-text small">
        Define everything the agent should ask for before it starts. These fields live together on the
        Agent inputs node and appear as a simple form at run time.
      </p>

      {inputs.length === 0 ? (
        <p className="empty-state">No customer inputs yet. Add one if the agent needs a question, document, or other value to begin.</p>
      ) : (
        <div className="stack gap-xs">
          {inputs.map((input) => {
            const isOpen = expanded === input.key;
            return (
              <div key={input.key} className={`workflow-input-row ${input.conflicting ? 'danger' : ''}`}>
                <button
                  type="button"
                  className="workflow-input-summary"
                  onClick={() => setExpanded(isOpen ? '' : input.key)}
                  aria-expanded={isOpen}
                >
                  <Icon name={isOpen ? 'chevronDown' : 'chevronRight'} size={13} />
                  <code className="workflow-input-key">{input.key}</code>
                  <span className="tiny-tag">{input.kind}</span>
                  {input.required ? <span className="tiny-tag accent">required</span> : null}
                  {input.nodeIds.length > 1 ? (
                    <span className="tiny-tag" title={input.nodeIds.join(', ')}>
                      {input.nodeIds.length} connections
                    </span>
                  ) : null}
                  {input.conflicting ? <span className="tiny-tag danger">type conflict</span> : null}
                </button>

                {isOpen ? (
                  <div className="stack gap-sm workflow-input-editor">
                    <div className="field-grid">
                      <div className="field-stack">
                        <label className="field-label">Key</label>
                        <input
                          className="input"
                          value={input.key}
                          onChange={(event) => onPatch(input.key, { key: slugifyKey(event.target.value) })}
                        />
                      </div>
                      <div className="field-stack">
                        <label className="field-label">Type</label>
                        <select
                          className="input"
                          value={input.kind}
                          onChange={(event) => onPatch(input.key, { kind: event.target.value as WorkflowInputKind })}
                        >
                          {WORKFLOW_INPUT_KINDS.map((kind) => (
                            <option key={kind} value={kind}>
                              {kind}
                            </option>
                          ))}
                        </select>
                      </div>
                    </div>
                    <div className="field-stack">
                      <label className="field-label">Label</label>
                      <input
                        className="input"
                        value={input.label}
                        onChange={(event) => onPatch(input.key, { label: event.target.value })}
                      />
                    </div>
                    <div className="field-stack">
                      <label className="field-label">Description</label>
                      <textarea
                        className="input"
                        rows={2}
                        value={input.description}
                        onChange={(event) => onPatch(input.key, { description: event.target.value })}
                      />
                    </div>
                    <div className="field-stack">
                      <label className="field-label">Default</label>
                      <input
                        className="input"
                        placeholder="Leave blank for no default"
                        value={defaultText(input.default)}
                        onChange={(event) =>
                          onPatch(input.key, { default: parseDefault(event.target.value, input.kind) })
                        }
                      />
                    </div>
                    <label className="checkbox-row">
                      <input
                        type="checkbox"
                        checked={input.required}
                        onChange={(event) => onPatch(input.key, { required: event.target.checked })}
                      />
                      <span>Required</span>
                    </label>
                    <div className="button-row">
                      {onFocusNode ? (
                        <button type="button" className="button subtle sm" onClick={() => onFocusNode(input.nodeIds[0])}>
                          <Icon name="cursor" size={12} />
                          Show Agent inputs
                        </button>
                      ) : null}
                      <button type="button" className="button subtle sm danger" onClick={() => onRemove(input.key)}>
                        <Icon name="trash" size={12} />
                        Remove
                      </button>
                    </div>
                    {input.nodeIds.length > 1 ? (
                      <p className="muted-text small">
                        This value currently feeds {input.nodeIds.length} underlying connections.
                      </p>
                    ) : null}
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      )}

      {adding ? (
        <div className="stack gap-sm workflow-input-editor">
          <div className="field-grid">
            <div className="field-stack">
              <label className="field-label">Key</label>
              <input
                className="input"
                autoFocus
                placeholder="document_id"
                value={draftKey}
                onChange={(event) => setDraftKey(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') submitDraft();
                  if (event.key === 'Escape') setAdding(false);
                }}
              />
            </div>
            <div className="field-stack">
              <label className="field-label">Type</label>
              <select
                className="input"
                value={draftKind}
                onChange={(event) => setDraftKind(event.target.value as WorkflowInputKind)}
              >
                {WORKFLOW_INPUT_KINDS.map((kind) => (
                  <option key={kind} value={kind}>
                    {kind}
                  </option>
                ))}
              </select>
            </div>
          </div>
          {draftKey && draftInvalid ? (
            <p className="muted-text small danger-text">
              {existingKeys.has(draftSlug) ? `“${draftSlug}” is already declared.` : 'Enter a key.'}
            </p>
          ) : null}
          <div className="button-row">
            <button type="button" className="button primary sm" disabled={draftInvalid} onClick={submitDraft}>
              Add input
            </button>
            <button type="button" className="button subtle sm" onClick={() => setAdding(false)}>
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <button type="button" className="button subtle block" onClick={() => setAdding(true)}>
          <Icon name="plus" size={13} />
          Add customer input
        </button>
      )}
    </div>
  );
}
