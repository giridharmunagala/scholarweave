import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { Icon } from './Icons';

export type ModelOption = {
  providerId: string;
  providerName: string;
  providerKind?: string;
  model: string;
  capabilities: string[];
};

export type ModelSelectValue = {
  provider_profile_id?: string | null;
  model?: string | null;
};

const rowKey = (option: ModelOption | null) =>
  option ? JSON.stringify([option.providerId, option.model]) : 'default';

/**
 * Searchable, keyboard-navigable model picker grouped by provider profile.
 * Replaces raw <select> lists which become unusable once a provider exposes
 * dozens of discovered models.
 */
export function ModelSelect({
  options,
  value,
  onChange,
  disabled = false,
  placeholder = 'Select a model',
  emptyOptionLabel,
  emptyOptionHint,
  inline = false,
  id,
  ariaLabel,
}: {
  options: ModelOption[];
  value: ModelSelectValue;
  onChange: (value: ModelSelectValue) => void;
  disabled?: boolean;
  placeholder?: string;
  /** Label for the "no explicit selection" entry. Omit to require a choice. */
  emptyOptionLabel?: string;
  emptyOptionHint?: string;
  inline?: boolean;
  id?: string;
  ariaLabel?: string;
}) {
  const [open, setOpen] = useState(false);
  const [dropUp, setDropUp] = useState(false);
  const [query, setQuery] = useState('');
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const listId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const selected = options.find(
    (option) => option.providerId === value.provider_profile_id && option.model === value.model,
  );
  const hasSelection = Boolean(value.provider_profile_id && value.model);
  const missing = hasSelection && !selected;

  const filtered = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    if (!needle) return options;
    return options.filter((option) =>
      `${option.providerName} ${option.model} ${option.capabilities.join(' ')}`
        .toLocaleLowerCase()
        .includes(needle),
    );
  }, [options, query]);

  const groups = useMemo(() => {
    const byProvider = new Map<string, { name: string; kind?: string; models: ModelOption[] }>();
    for (const option of filtered) {
      const group = byProvider.get(option.providerId);
      if (group) group.models.push(option);
      else
        byProvider.set(option.providerId, {
          name: option.providerName,
          kind: option.providerKind,
          models: [option],
        });
    }
    return [...byProvider.entries()];
  }, [filtered]);

  // Follow the rendered provider groups, even when incoming options are interleaved.
  const rows = useMemo<(ModelOption | null)[]>(() => {
    const models = groups.flatMap(([, group]) => group.models);
    return emptyOptionLabel && !query.trim() ? [null, ...models] : models;
  }, [emptyOptionLabel, groups, query]);
  const activeIndex = rows.findIndex((row) => rowKey(row) === activeKey);
  const activeRowId = activeIndex >= 0 ? `${listId}-option-${activeIndex}` : undefined;

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const bounds = rootRef.current?.getBoundingClientRect();
    // Flip above the trigger when there is not enough room below it.
    if (bounds) setDropUp(window.innerHeight - bounds.bottom < 340 && bounds.top > 340);
    inputRef.current?.focus();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    listRef.current
      ?.querySelector<HTMLElement>('[data-active="true"]')
      ?.scrollIntoView?.({ block: 'nearest' });
  }, [activeIndex, open]);

  const commit = (option: ModelOption | null) => {
    onChange(
      option ? { provider_profile_id: option.providerId, model: option.model } : {},
    );
    setOpen(false);
    triggerRef.current?.focus();
  };

  const showOptions = () => {
    setQuery('');
    setActiveKey(selected ? rowKey(selected)
      : hasSelection ? null
        : emptyOptionLabel ? rowKey(null) : options[0] ? rowKey(options[0]) : null);
    setOpen(true);
  };

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (!open) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      setOpen(false);
      triggerRef.current?.focus();
      return;
    }
    if (event.key === 'Tab') {
      setOpen(false);
      // Continue normal tab navigation from the trigger, not the removed search field.
      triggerRef.current?.focus();
      return;
    }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (!rows.length) return;
      const step = event.key === 'ArrowDown' ? 1 : -1;
      const nextIndex = activeIndex < 0
        ? step > 0 ? 0 : rows.length - 1
        : (activeIndex + step + rows.length) % rows.length;
      setActiveKey(rowKey(rows[nextIndex]));
      return;
    }
    if (event.key === 'Enter') {
      event.preventDefault();
      if (activeIndex >= 0) commit(rows[activeIndex]);
    }
  };

  const label = selected
    ? `${selected.providerName} / ${selected.model}`
    : missing
      ? `Unavailable: ${value.model}`
      : emptyOptionLabel ?? placeholder;

  return (
    <div
      className={`model-select${open ? ' open' : ''}${inline ? ' inline' : ''}`}
      ref={rootRef}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpen(false);
      }}
      onKeyDown={onKeyDown}
    >
      <button
        type="button"
        ref={triggerRef}
        id={id}
        aria-label={ariaLabel}
        className={`model-select-trigger${missing ? ' missing' : ''}`}
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => open ? setOpen(false) : showOptions()}
        onKeyDown={(event) => {
          if (!open && ['ArrowDown', 'ArrowUp'].includes(event.key)) {
            event.preventDefault();
            event.stopPropagation();
            showOptions();
          }
        }}
      >
        <span className="model-select-value">
          {selected ? <small>{selected.providerName}</small> : null}
          <strong>{selected ? selected.model : label}</strong>
        </span>
        <Icon name="arrowRight" size={14} />
      </button>
      {open ? (
        <div
          className={`model-select-popover${dropUp && !inline ? ' up' : ''}`}
          role="dialog"
        >
          <div className="model-select-search">
            <Icon name="search" size={14} />
            <input
              ref={inputRef}
              type="search"
              value={query}
              placeholder="Search models…"
              aria-label="Search models"
              role="combobox"
              aria-autocomplete="list"
              aria-expanded={open}
              aria-controls={listId}
              aria-activedescendant={activeRowId}
              onChange={(event) => {
                const nextQuery = event.target.value;
                setQuery(nextQuery);
                const needle = nextQuery.trim().toLocaleLowerCase();
                const first = options.find((option) =>
                  `${option.providerName} ${option.model} ${option.capabilities.join(' ')}`
                    .toLocaleLowerCase().includes(needle));
                setActiveKey(!needle && emptyOptionLabel ? rowKey(null) : first ? rowKey(first) : null);
              }}
            />
          </div>
          <div className="model-select-list" id={listId} role="listbox" ref={listRef}>
            {emptyOptionLabel && !query.trim() ? (
              <button
                type="button"
                role="option"
                id={`${listId}-option-0`}
                aria-selected={!hasSelection}
                data-active={activeIndex === 0}
                className="model-select-option"
                onMouseEnter={() => setActiveKey(rowKey(null))}
                onClick={() => commit(null)}
              >
                <span className="model-select-option-body">
                  <strong>{emptyOptionLabel}</strong>
                  {emptyOptionHint ? <small>{emptyOptionHint}</small> : null}
                </span>
                {!hasSelection ? <Icon name="check" size={14} /> : null}
              </button>
            ) : null}
            {groups.map(([providerId, group]) => (
              <div className="model-select-group" key={providerId}>
                <p className="model-select-group-label">
                  {group.name}
                  {group.kind ? <span>{group.kind}</span> : null}
                </p>
                {group.models.map((option) => {
                  const index = rows.indexOf(option);
                  const isSelected = option === selected;
                  return (
                    <button
                      type="button"
                      role="option"
                      id={`${listId}-option-${index}`}
                      aria-selected={isSelected}
                      data-active={index === activeIndex}
                      className="model-select-option"
                      key={rowKey(option)}
                      onMouseEnter={() => setActiveKey(rowKey(option))}
                      onClick={() => commit(option)}
                    >
                      <span className="model-select-option-body">
                        <strong>{option.model}</strong>
                        {option.capabilities.length ? (
                          <span className="model-select-caps">
                            {option.capabilities.slice(0, 4).map((capability) => (
                              <em key={capability}>{capability}</em>
                            ))}
                          </span>
                        ) : (
                          <small>capabilities not declared</small>
                        )}
                      </span>
                      {isSelected ? <Icon name="check" size={14} /> : null}
                    </button>
                  );
                })}
              </div>
            ))}
            {!filtered.length ? (
              <p className="model-select-empty">
                {options.length ? `No models match “${query.trim()}”.` : 'No models available yet.'}
              </p>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}
