import { useEffect, useMemo, useState } from 'react';
import { Icon } from '../common/Icon';
import { categoryVars, groupByCategory, highlightParts, searchNodes } from '../../lib/nodeCatalog';
import type { NodeDefinitionResponse } from '../../types/api';

interface NodePaletteProps {
  nodes: NodeDefinitionResponse[];
  search: string;
  onAdd: (type: string) => void;
  onCreateCustom?: () => void;
  onManageCustom?: () => void;
}

const COLLAPSED_KEY = 'scholarweave-palette-collapsed';

function readCollapsed(): Set<string> {
  try {
    const raw = localStorage.getItem(COLLAPSED_KEY);
    return new Set<string>(raw ? (JSON.parse(raw) as string[]) : []);
  } catch {
    return new Set<string>();
  }
}

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

export function NodePalette({ nodes, search, onAdd, onCreateCustom, onManageCustom }: NodePaletteProps) {
  const [collapsed, setCollapsed] = useState<Set<string>>(readCollapsed);
  const term = search.trim();

  useEffect(() => {
    localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...collapsed]));
  }, [collapsed]);

  const groups = useMemo(() => groupByCategory(searchNodes(nodes, term)), [nodes, term]);
  const matchCount = useMemo(() => groups.reduce((total, group) => total + group.nodes.length, 0), [groups]);

  const toggle = (id: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const allCollapsed = groups.length > 0 && groups.every((group) => collapsed.has(group.meta.id));

  if (groups.length === 0) {
    return (
      <div className="palette">
        {onCreateCustom ? (
          <div className="palette-custom-actions">
            <button type="button" className="button subtle sm block" onClick={onCreateCustom}>
              <Icon name="braces" size={12} />
              Create custom node
            </button>
            {onManageCustom ? <button type="button" className="link-button" onClick={onManageCustom}>Manage custom nodes</button> : null}
          </div>
        ) : null}
        <p className="empty-state">No nodes matched “{term}”.</p>
      </div>
    );
  }

  return (
    <div className="palette">
      {onCreateCustom ? (
        <div className="palette-custom-actions">
          <button type="button" className="button subtle sm block" onClick={onCreateCustom}>
            <Icon name="braces" size={12} />
            Create custom node
          </button>
          {onManageCustom ? (
            <button type="button" className="link-button" onClick={onManageCustom}>
              Manage custom nodes
            </button>
          ) : null}
        </div>
      ) : null}
      <div className="palette-toolbar">
        <span>
          {term ? `${matchCount} ${matchCount === 1 ? 'match' : 'matches'}` : `${matchCount} nodes · ${groups.length} groups`}
        </span>
        <button
          type="button"
          className="link-button"
          onClick={() => setCollapsed(allCollapsed ? new Set() : new Set(groups.map((group) => group.meta.id)))}
        >
          <Icon name={allCollapsed ? 'expand' : 'collapse'} size={11} />
          {allCollapsed ? 'Expand all' : 'Collapse all'}
        </button>
      </div>

      <div className="palette-groups">
        {groups.map(({ meta, nodes: categoryNodes }) => {
          // A live search always reveals its matches, regardless of saved collapse state.
          const isOpen = Boolean(term) || !collapsed.has(meta.id);
          return (
            <section key={meta.id} className={`palette-group${isOpen ? ' open' : ''}`} style={categoryVars(meta.id)}>
              <button
                type="button"
                className="palette-group-header"
                aria-expanded={isOpen}
                title={meta.blurb}
                onClick={() => toggle(meta.id)}
              >
                <Icon name="chevronRight" size={11} className="chevron" />
                <span className="palette-group-icon">
                  <Icon name={meta.icon} size={12} />
                </span>
                <h3>{meta.label}</h3>
                <span className="palette-group-count">{categoryNodes.length}</span>
              </button>

              {isOpen ? (
                <div className="palette-items">
                  {categoryNodes.map((node) => (
                    <button
                      key={node.type}
                      type="button"
                      className="palette-item"
                      draggable
                      title={`${node.label} — ${node.description}\n\nDrag onto the canvas, or click to add.`}
                      onClick={() => onAdd(node.type)}
                      onDragStart={(event) => {
                        event.dataTransfer.setData('application/x-scholarweave-node', node.type);
                        event.dataTransfer.effectAllowed = 'copy';
                      }}
                    >
                      <span className="palette-item-dot" aria-hidden="true" />
                      <span className="palette-item-copy">
                        <strong>
                          <Highlight text={node.label} term={term} />
                        </strong>
                        {term ? <span className="palette-item-type">{node.type}</span> : null}
                      </span>
                      <span className="palette-add" aria-hidden="true">
                        <Icon name="plus" size={12} />
                      </span>
                    </button>
                  ))}
                </div>
              ) : null}
            </section>
          );
        })}
      </div>
    </div>
  );
}
