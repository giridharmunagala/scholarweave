import { useEffect, useState, type ReactNode } from 'react';
import { Link, NavLink, useLocation } from './router';
import { CommandPalette, useCommandPalette } from '../shared/components/CommandPalette';
import { Icon, type IconName } from '../shared/components/Icons';
import { ThemeSwitcher } from '../shared/components/ThemeSwitcher';

interface NavEntry {
  to: string;
  label: string;
  icon: IconName;
}

const NAV_GROUPS: Array<{ title: string; items: NavEntry[] }> = [
  {
    title: 'Workspace',
    items: [
      { to: '/', label: 'Overview', icon: 'overview' },
      { to: '/chat', label: 'Builder', icon: 'builder' },
      { to: '/research-chat', label: 'Research chat', icon: 'agents' },
      { to: '/papers', label: 'Papers', icon: 'papers' },
    ],
  },
  {
    title: 'Compose',
    items: [
      { to: '/agents', label: 'Agents', icon: 'agents' },
      { to: '/tools', label: 'Tools', icon: 'tools' },
    ],
  },
  {
    title: 'Operate',
    items: [
      { to: '/runs', label: 'Runs', icon: 'runs' },
      { to: '/workspace', label: 'Files', icon: 'workspace' },
      { to: '/settings', label: 'Settings', icon: 'settings' },
    ],
  },
];

const COMPACT_KEY = 'scholarweave-sidebar-compact';

const TITLES: Array<[string, string]> = [
  ['/chat', 'Builder'],
  ['/research-chat', 'Research chat'],
  ['/agents', 'Agents'],
  ['/tools', 'Tools'],
  ['/runs', 'Runs'],
  ['/papers', 'Papers'],
  ['/workspace', 'Files'],
  ['/settings', 'Settings'],
];

function currentTitle(pathname: string): string {
  const match = TITLES.find(([prefix]) => pathname === prefix || pathname.startsWith(`${prefix}/`));
  return match ? match[1] : 'Overview';
}

export function AppShell({ children }: { children: ReactNode }) {
  const { pathname } = useLocation();
  const palette = useCommandPalette();
  const [compact, setCompact] = useState(() => {
    try {
      return localStorage.getItem(COMPACT_KEY) === 'true';
    } catch {
      return false;
    }
  });
  const [drawer, setDrawer] = useState(false);

  useEffect(() => {
    try {
      localStorage.setItem(COMPACT_KEY, String(compact));
    } catch {
      /* Persisting the layout preference is best effort. */
    }
  }, [compact]);

  // Route changes should never leave the mobile drawer covering the page.
  useEffect(() => setDrawer(false), [pathname]);

  const title = currentTitle(pathname);
  const detail = pathname.split('/').filter(Boolean)[1];

  return (
    <div className="app-shell" data-compact={compact} data-drawer={drawer}>
      <aside className="sidebar">
        <Link className="brand" to="/" aria-label="ScholarWeave home">
          <span className="brand-mark">SW</span>
          <span className="brand-text">
            <strong>ScholarWeave</strong>
            <small>Agents SDK workspace</small>
          </span>
        </Link>
        <nav aria-label="Primary">
          {NAV_GROUPS.map((group) => (
            <div key={group.title}>
              <div className="nav-group">{group.title}</div>
              {group.items.map((item) => (
                <NavLink key={item.to} to={item.to} title={item.label}>
                  <Icon name={item.icon} />
                  <span className="nav-label">{item.label}</span>
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
        <div className="sidebar-foot">
          <div className="sdk-status" title="OpenAI Agents SDK 0.19.4">
            <span className="sdk-dot" />
            <span>OpenAI Agents SDK 0.19.4</span>
          </div>
        </div>
      </aside>

      <div className="sidebar-scrim" role="presentation" onClick={() => setDrawer(false)} />

      <div className="app-body">
        <header className="topbar">
          <button
            type="button"
            className="button ghost icon drawer-toggle"
            aria-label="Open navigation"
            onClick={() => setDrawer((value) => !value)}
          >
            <Icon name="menu" />
          </button>
          <button
            type="button"
            className="button ghost icon sidebar-toggle"
            aria-label={compact ? 'Expand sidebar' : 'Collapse sidebar'}
            title={compact ? 'Expand sidebar' : 'Collapse sidebar'}
            onClick={() => setCompact((value) => !value)}
          >
            <Icon name="sidebar" />
          </button>
          <nav className="breadcrumb" aria-label="Breadcrumb">
            <strong>{title}</strong>
            {detail ? (
              <>
                <span className="sep">/</span>
                <span className="truncate">{detail === 'new' ? 'New' : detail}</span>
              </>
            ) : null}
          </nav>
          <span className="spacer" />
          <button type="button" className="search-trigger" onClick={() => palette.setOpen(true)}>
            <Icon name="search" size={16} />
            <span className="search-label">Search…</span>
            <kbd>⌘K</kbd>
          </button>
          <ThemeSwitcher />
        </header>
        <main className="app-main">{children}</main>
      </div>

      <CommandPalette open={palette.open} onClose={palette.close} />
    </div>
  );
}
