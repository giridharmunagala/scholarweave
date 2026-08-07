import { useEffect, useMemo, useRef, useState } from 'react';
import { Icon } from '../../../shared/components/Icons';
import type { SdkCatalog } from '../types';

type GuardrailKind = 'input' | 'output' | 'tool_input' | 'tool_output';

export function AddNodeMenu({
  catalog,
  onAddAgent,
  onAddFunctionTool,
  onAddGuardrail,
}: {
  catalog: SdkCatalog | null;
  onAddAgent: () => void;
  onAddFunctionTool: (catalogId: string) => void;
  onAddGuardrail: (kind: GuardrailKind, catalogId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const containerRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const normalizedQuery = query.trim().toLowerCase();
  const agentMatches = 'new agent delegation handoff'.includes(normalizedQuery);
  const tools = useMemo(
    () =>
      (catalog?.function_tools ?? []).filter((tool) =>
        `${tool.label} ${tool.description} ${tool.catalog_id}`.toLowerCase().includes(normalizedQuery),
      ),
    [catalog, normalizedQuery],
  );
  const guardrails = useMemo(
    () =>
      (catalog?.guardrails ?? []).filter((guardrail) =>
        `${guardrail.label} ${guardrail.description} ${guardrail.kind}`.toLowerCase().includes(normalizedQuery),
      ),
    [catalog, normalizedQuery],
  );

  useEffect(() => {
    if (!open) return;
    searchRef.current?.focus();
    const onPointerDown = (event: MouseEvent) => {
      if (!containerRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  const choose = (action: () => void) => {
    action();
    setOpen(false);
    setQuery('');
  };

  return (
    <div className="add-node-menu" ref={containerRef}>
      <button
        className="button"
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <Icon name="plus" size={15} />
        Add node
      </button>
      {open ? (
        <div className="add-node-popover" role="menu" aria-label="Add a node">
          <div className="add-node-search">
            <Icon name="search" size={16} />
            <input
              ref={searchRef}
              value={query}
              aria-label="Search node types"
              placeholder="Search agents, tools, guardrails…"
              onChange={(event) => setQuery(event.target.value)}
            />
          </div>
          {agentMatches ? (
            <NodeOption
              kind="Agent"
              title="New agent"
              description="Add another agent for delegation or handoffs."
              onClick={() => choose(onAddAgent)}
            />
          ) : null}
          {tools.length ? <span className="add-node-group">Function tools</span> : null}
          {tools.map((tool) => (
            <NodeOption
              key={tool.catalog_id}
              kind="Tool"
              title={tool.label}
              description={tool.description}
              onClick={() => choose(() => onAddFunctionTool(tool.catalog_id))}
            />
          ))}
          {guardrails.length ? <span className="add-node-group">Guardrails</span> : null}
          {guardrails.map((guardrail) => (
            <NodeOption
              key={`${guardrail.kind}:${guardrail.catalog_id}`}
              kind={guardrail.kind.split('_').join(' ')}
              title={guardrail.label}
              description={guardrail.description}
              onClick={() =>
                choose(() => onAddGuardrail(guardrail.kind as GuardrailKind, guardrail.catalog_id))
              }
            />
          ))}
          {normalizedQuery && !agentMatches && !tools.length && !guardrails.length ? (
            <p className="add-node-empty">No matching nodes.</p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function NodeOption({
  kind,
  title,
  description,
  onClick,
}: {
  kind: string;
  title: string;
  description: string;
  onClick: () => void;
}) {
  return (
    <button className="add-node-option" type="button" role="menuitem" onClick={onClick}>
      <span className="add-node-option-copy">
        <strong>{title}</strong>
        <small>{description}</small>
      </span>
      <span className="add-node-kind">{kind}</span>
    </button>
  );
}
