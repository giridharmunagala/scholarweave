import { useEffect, useMemo, useRef, useState } from 'react';
import { Icon } from '../common/Icon';
import { api } from '../../lib/api';
import { toMessage } from '../common/Toast';
import { acceptsInput } from '../../lib/workflowInputs';
import type { WorkflowDefinition, WorkflowResponse } from '../../types/api';

export type WorkflowChoice =
  | { kind: 'saved'; workflow: WorkflowResponse; definition: WorkflowDefinition | null }
  | { kind: 'template'; definition: WorkflowDefinition }
  | { kind: 'blank' };

interface WorkflowPickerProps {
  open: boolean;
  onClose: () => void;
  onSelect: (choice: WorkflowChoice) => void;
  title?: string;
  description?: string;
  /** Highlights workflows that declare this input, e.g. `document_id` when coming from a paper. */
  highlightInputKey?: string;
  /** Offers "Start from an empty canvas" as a final option. */
  allowBlank?: boolean;
}

interface PickerEntry {
  id: string;
  name: string;
  description: string;
  badge: string;
  nodeCount: number;
  accepts: boolean;
  choice: WorkflowChoice;
}

function matches(entry: PickerEntry, term: string): boolean {
  if (!term) return true;
  const needle = term.toLowerCase();
  return entry.name.toLowerCase().includes(needle) || entry.description.toLowerCase().includes(needle);
}

export function WorkflowPicker({
  open,
  onClose,
  onSelect,
  title = 'Choose an agent',
  description,
  highlightInputKey,
  allowBlank = false,
}: WorkflowPickerProps) {
  const [saved, setSaved] = useState<WorkflowResponse[]>([]);
  const [templates, setTemplates] = useState<WorkflowDefinition[]>([]);
  const [term, setTerm] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (!open) return;
    setTerm('');
    setError('');
    setLoading(true);
    let active = true;
    Promise.all([api.listWorkflows(), api.listWorkflowTemplates()])
      .then(([workflows, starter]) => {
        if (!active) return;
        setSaved(workflows);
        setTemplates(starter);
      })
      .catch((err) => active && setError(toMessage(err, 'Failed to load agents')))
      .finally(() => active && setLoading(false));
    const frame = requestAnimationFrame(() => inputRef.current?.focus());
    return () => {
      active = false;
      cancelAnimationFrame(frame);
    };
  }, [open]);

  const savedEntries = useMemo<PickerEntry[]>(
    () =>
      saved.map((workflow) => {
        const definition = workflow.latest_version?.definition ?? null;
        return {
          id: `saved:${workflow.id}`,
          name: workflow.name,
          description: workflow.description || definition?.description || 'No description',
          badge: workflow.is_template ? 'Saved template' : 'Saved',
          nodeCount: definition?.nodes.length ?? 0,
          accepts: Boolean(highlightInputKey && acceptsInput(definition, highlightInputKey)),
          choice: { kind: 'saved', workflow, definition },
        };
      }),
    [highlightInputKey, saved],
  );

  const templateEntries = useMemo<PickerEntry[]>(
    () =>
      templates
        // A starter template that has already been saved shows up in the saved list instead.
        .filter((template) => !saved.some((workflow) => workflow.name === template.name))
        .map((template) => ({
          id: `template:${template.name}`,
          name: template.name,
          description: template.description || 'No description',
          badge: 'Starter',
          nodeCount: template.nodes.length,
          accepts: Boolean(highlightInputKey && acceptsInput(template, highlightInputKey)),
          choice: { kind: 'template', definition: template },
        })),
    [highlightInputKey, saved, templates],
  );

  const groups = useMemo(
    () =>
      [
        { label: 'Your agents', entries: savedEntries.filter((entry) => matches(entry, term)) },
        { label: 'Starter templates', entries: templateEntries.filter((entry) => matches(entry, term)) },
      ].filter((group) => group.entries.length > 0),
    [savedEntries, templateEntries, term],
  );

  if (!open) return null;

  const total = groups.reduce((sum, group) => sum + group.entries.length, 0);

  const commit = (choice: WorkflowChoice) => {
    onSelect(choice);
    onClose();
  };

  return (
    <div className="quick-add-scrim" role="presentation" onMouseDown={onClose}>
      <div
        className="quick-add workflow-picker"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="quick-add-input">
          <Icon name="search" size={15} />
          <input
            ref={inputRef}
            value={term}
            placeholder={title}
            aria-label="Search agents"
            onChange={(event) => setTerm(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Escape') {
                event.preventDefault();
                onClose();
              }
            }}
          />
          <kbd>Esc</kbd>
        </div>

        {description ? <p className="workflow-picker-hint">{description}</p> : null}
        {error ? <p className="workflow-picker-hint danger-text">{error}</p> : null}

        <div className="quick-add-results" role="listbox" aria-label="Agent results">
          {loading ? <p className="empty-state">Loading agents…</p> : null}
          {!loading && total === 0 ? (
            <p className="empty-state">{term ? `No agents matched “${term}”.` : 'No agents yet.'}</p>
          ) : null}
          {groups.map((group) => (
            <div className="workflow-picker-group" key={group.label}>
              <span className="workflow-picker-group-label">{group.label}</span>
              {group.entries.map((entry) => (
                <button
                  key={entry.id}
                  type="button"
                  role="option"
                  aria-selected={false}
                  className="quick-add-result"
                  onClick={() => commit(entry.choice)}
                >
                  <span className="quick-add-icon">
                    <Icon name="workflow" size={14} />
                  </span>
                  <span className="quick-add-copy">
                    <strong>{entry.name}</strong>
                    <span>{entry.description}</span>
                  </span>
                  {entry.accepts ? <span className="tiny-tag accent">Takes {highlightInputKey}</span> : null}
                  <span className="quick-add-category">
                    {entry.badge} · {entry.nodeCount} nodes
                  </span>
                </button>
              ))}
            </div>
          ))}
          {allowBlank ? (
            <div className="workflow-picker-group">
              <span className="workflow-picker-group-label">Start fresh</span>
              <button type="button" className="quick-add-result" onClick={() => commit({ kind: 'blank' })}>
                <span className="quick-add-icon">
                  <Icon name="plus" size={14} />
                </span>
                <span className="quick-add-copy">
                  <strong>Empty canvas</strong>
                  <span>Build an agent from scratch.</span>
                </span>
              </button>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
