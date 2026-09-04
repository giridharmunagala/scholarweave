import { useEffect, useMemo, useRef, useState } from 'react';
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
  const [activeIndex, setActiveIndex] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
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

  // Flat list of selectable rows so arrow keys can cross provider groups.
  const rows = useMemo<(ModelOption | null)[]>(
    () => (emptyOptionLabel && !query.trim() ? [null, ...filtered] : filtered),
    [emptyOptionLabel, filtered, query],
  );

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
    setQuery('');
    setActiveIndex(Math.max(0, rows.findIndex((row) => row === selected)));
    const bounds = rootRef.current?.getBoundingClientRect();
    // Flip above the trigger when there is not enough room below it.
    if (bounds) setDropUp(window.innerHeight - bounds.bottom < 340 && bounds.top > 340);
    inputRef.current?.focus();
    // Only re-run when the popover toggles.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useEffect(() => {
    if (!open) return;
    listRef.current
      ?.querySelector<HTMLElement>('[data-active="true"]')
      ?.scrollIntoView({ block: 'nearest' });
  }, [activeIndex, open]);

  const commit = (option: ModelOption | null) => {
    onChange(
      option ? { provider_profile_id: option.providerId, model: option.model } : {},
    );
    setOpen(false);
  };

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === 'Escape') {
      setOpen(false);
      return;
    }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (!rows.length) return;
      const step = event.key === 'ArrowDown' ? 1 : -1;
      setActiveIndex((index) => (index + step + rows.length) % rows.length);
      return;
    }
    if (event.key === 'Enter') {
      event.preventDefault();
      if (rows.length) commit(rows[activeIndex] ?? null);
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
    >
      <button
        type="button"
        id={id}
        aria-label={ariaLabel}
        className={`model-select-trigger${missing ? ' missing' : ''}`}
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
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
              onKeyDown={onKeyDown}
              onChange={(event) => {
                setQuery(event.target.value);
                setActiveIndex(0);
              }}
            />
          </div>
          <div className="model-select-list" role="listbox" ref={listRef}>
            {emptyOptionLabel && !query.trim() ? (
              <button
                type="button"
                role="option"
                aria-selected={!hasSelection}
                data-active={activeIndex === 0}
                className="model-select-option"
                onMouseEnter={() => setActiveIndex(0)}
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
                      aria-selected={isSelected}
                      data-active={index === activeIndex}
                      className="model-select-option"
                      key={`${option.providerId}:${option.model}`}
                      onMouseEnter={() => setActiveIndex(index)}
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
