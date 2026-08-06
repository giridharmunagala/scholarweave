import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from '../../app/router';
import { Icon, type IconName } from './Icons';
import { useTheme } from '../theme/ThemeProvider';
import { THEMES } from '../theme/themes';

export interface Command {
  id: string;
  label: string;
  group: string;
  icon: IconName;
  hint?: string;
  run: () => void;
}

/** True when a keystroke happens inside a text entry, so shortcuts stay out of the way. */
function isTypingTarget(target: EventTarget | null): boolean {
  const element = target as HTMLElement | null;
  if (!element) return false;
  return (
    element.isContentEditable ||
    ['INPUT', 'TEXTAREA', 'SELECT'].includes(element.tagName)
  );
}

export function useCommandPalette() {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        setOpen((value) => !value);
        return;
      }
      if (event.key === '/' && !isTypingTarget(event.target) && !event.metaKey && !event.ctrlKey) {
        event.preventDefault();
        setOpen(true);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);
  const close = useCallback(() => setOpen(false), []);
  return { open, setOpen, close };
}

const NAVIGATION: Array<{ to: string; label: string; icon: IconName }> = [
  { to: '/', label: 'Overview', icon: 'overview' },
  { to: '/chat', label: 'Builder chat', icon: 'builder' },
  { to: '/agents', label: 'Agents', icon: 'agents' },
  { to: '/tools', label: 'Tools', icon: 'tools' },
  { to: '/runs', label: 'Runs', icon: 'runs' },
  { to: '/papers', label: 'Papers', icon: 'papers' },
  { to: '/workspace', label: 'Workspace', icon: 'workspace' },
  { to: '/settings', label: 'Settings', icon: 'settings' },
];

export function CommandPalette({ open, onClose }: { open: boolean; onClose: () => void }) {
  const navigate = useNavigate();
  const { setPreference } = useTheme();
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);
  const listRef = useRef<HTMLDivElement>(null);

  const commands = useMemo<Command[]>(() => {
    const items: Command[] = NAVIGATION.map((entry) => ({
      id: `go:${entry.to}`,
      label: entry.label,
      group: 'Go to',
      icon: entry.icon,
      run: () => navigate(entry.to),
    }));
    items.push({
      id: 'create:agent',
      label: 'Create a new agent',
      group: 'Actions',
      icon: 'plus',
      run: () => navigate('/agents/new'),
    });
    for (const theme of THEMES) {
      items.push({
        id: `theme:${theme.id}`,
        label: `Theme: ${theme.label}`,
        group: 'Appearance',
        icon: 'palette',
        hint: theme.description,
        run: () => setPreference(theme.id),
      });
    }
    items.push({
      id: 'theme:system',
      label: 'Theme: match system',
      group: 'Appearance',
      icon: 'palette',
      run: () => setPreference('system'),
    });
    return items;
  }, [navigate, setPreference]);

  const results = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return commands;
    return commands.filter((command) =>
      `${command.group} ${command.label}`.toLowerCase().includes(needle),
    );
  }, [commands, query]);

  useEffect(() => {
    if (open) {
      setQuery('');
      setActive(0);
    }
  }, [open]);

  useEffect(() => {
    setActive(0);
  }, [query]);

  useEffect(() => {
    if (!open) return undefined;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  useEffect(() => {
    listRef.current?.querySelector('[data-active="true"]')?.scrollIntoView({ block: 'nearest' });
  }, [active, results]);

  if (!open) return null;

  const select = (command: Command | undefined) => {
    if (!command) return;
    command.run();
    onClose();
  };

  let lastGroup = '';
  return (
    <div
      className="palette-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="palette" role="dialog" aria-modal="true" aria-label="Command palette">
        <div className="palette-input">
          <Icon name="search" size={17} />
          <input
            autoFocus
            value={query}
            placeholder="Search pages, actions and themes…"
            aria-label="Command palette search"
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Escape') onClose();
              else if (event.key === 'ArrowDown') {
                event.preventDefault();
                setActive((value) => (results.length ? (value + 1) % results.length : 0));
              } else if (event.key === 'ArrowUp') {
                event.preventDefault();
                setActive((value) => (results.length ? (value - 1 + results.length) % results.length : 0));
              } else if (event.key === 'Enter') {
                event.preventDefault();
                select(results[active]);
              }
            }}
          />
          <button type="button" className="button ghost icon" aria-label="Close" onClick={onClose}>
            <Icon name="close" size={16} />
          </button>
        </div>
        <div className="palette-list" ref={listRef}>
          {results.map((command, index) => {
            const heading = command.group !== lastGroup ? command.group : null;
            lastGroup = command.group;
            return (
              <div key={command.id}>
                {heading ? <div className="palette-group">{heading}</div> : null}
                <button
                  type="button"
                  className="palette-item"
                  data-active={index === active}
                  onMouseEnter={() => setActive(index)}
                  onClick={() => select(command)}
                >
                  <Icon name={command.icon} size={17} />
                  {command.label}
                  {command.hint ? <small>{command.hint}</small> : null}
                </button>
              </div>
            );
          })}
          {!results.length ? <p className="palette-empty">No matches for “{query}”.</p> : null}
        </div>
        <div className="palette-foot">
          <span>
            <kbd>↑</kbd> <kbd>↓</kbd> navigate
          </span>
          <span>
            <kbd>↵</kbd> open
          </span>
          <span>
            <kbd>esc</kbd> close
          </span>
        </div>
      </div>
    </div>
  );
}
