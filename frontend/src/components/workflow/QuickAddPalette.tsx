import { useEffect, useMemo, useRef, useState } from 'react';
import { Icon } from '../common/Icon';
import { categoryMeta, categoryVars, highlightParts, searchNodes } from '../../lib/nodeCatalog';
import type { NodeDefinitionResponse } from '../../types/api';

interface QuickAddProps {
  open: boolean;
  nodes: NodeDefinitionResponse[];
  onAdd: (type: string) => void;
  onCreateCustom?: () => void;
  onClose: () => void;
}

const MAX_RESULTS = 40;

function Highlight({ text, term }: { text: string; term: string }) {
  const [before, match, after] = highlightParts(text, term);
  if (!match) return <>{text}</>;
  return (
    <>
      {before}
      <mark>{match}</mark>
      {after}
    </>
  );
}

/** Returns true when the ⌘K / Ctrl+K shortcut should be honoured for this event. */
export function isQuickAddShortcut(event: KeyboardEvent): boolean {
  return event.key.toLowerCase() === 'k' && (event.metaKey || event.ctrlKey) && !event.altKey;
}

export function QuickAddPalette({ open, nodes, onAdd, onCreateCustom, onClose }: QuickAddProps) {
  const [term, setTerm] = useState('');
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);

  const results = useMemo(() => searchNodes(nodes, term).slice(0, MAX_RESULTS), [nodes, term]);

  useEffect(() => {
    if (!open) return;
    setTerm('');
    setActive(0);
    const frame = requestAnimationFrame(() => inputRef.current?.focus());
    return () => cancelAnimationFrame(frame);
  }, [open]);

  useEffect(() => {
    setActive(0);
  }, [term]);

  useEffect(() => {
    listRef.current?.querySelector('[data-active="true"]')?.scrollIntoView({ block: 'nearest' });
  }, [active, results]);

  if (!open) return null;

  const commit = (index: number) => {
    const node = results[index];
    if (!node) return;
    onAdd(node.type);
    onClose();
  };

  return (
    <div className="quick-add-scrim" role="presentation" onMouseDown={onClose}>
      <div
        className="quick-add"
        role="dialog"
        aria-modal="true"
        aria-label="Add a node"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="quick-add-input">
          <Icon name="search" size={15} />
          <input
            ref={inputRef}
            value={term}
            placeholder="Add a node — search by name, type or tag"
            aria-label="Search nodes"
            onChange={(event) => setTerm(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Escape') {
                event.preventDefault();
                onClose();
              } else if (event.key === 'ArrowDown') {
                event.preventDefault();
                setActive((index) => (results.length ? (index + 1) % results.length : 0));
              } else if (event.key === 'ArrowUp') {
                event.preventDefault();
                setActive((index) => (results.length ? (index - 1 + results.length) % results.length : 0));
              } else if (event.key === 'Enter') {
                event.preventDefault();
                commit(active);
              }
            }}
          />
          <kbd>Esc</kbd>
        </div>

        <div className="quick-add-results" ref={listRef} role="listbox" aria-label="Node results">
          {results.map((node, index) => {
            const meta = categoryMeta(node.category);
            return (
              <button
                key={node.type}
                type="button"
                role="option"
                aria-selected={index === active}
                data-active={index === active}
                className="quick-add-result"
                style={categoryVars(node.category)}
                onMouseMove={() => setActive(index)}
                onClick={() => commit(index)}
              >
                <span className="quick-add-icon">
                  <Icon name={meta.icon} size={14} />
                </span>
                <span className="quick-add-copy">
                  <strong>
                    <Highlight text={node.label} term={term} />
                  </strong>
                  <span>{node.description}</span>
                </span>
                <span className="quick-add-category">{meta.label}</span>
              </button>
            );
          })}
          {results.length === 0 ? <p className="empty-state">No nodes matched “{term}”.</p> : null}
        </div>

        <div className="quick-add-footer">
          <span>
            <kbd>↑</kbd>
            <kbd>↓</kbd>
            navigate
          </span>
          <span>
            <kbd>↵</kbd>
            add to canvas
          </span>
          <span>{results.length} shown</span>
          {onCreateCustom ? (
            <button
              type="button"
              className="link-button quick-add-custom-action"
              onClick={() => {
                onClose();
                onCreateCustom();
              }}
            >
              <Icon name="braces" size={11} />
              Create custom node
            </button>
          ) : null}
        </div>
      </div>
    </div>
  );
}
