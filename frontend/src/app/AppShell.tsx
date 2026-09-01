import type { ReactNode } from 'react';
import { Link, NavLink } from './router';
import { CommandPalette, useCommandPalette } from '../shared/components/CommandPalette';
import { Icon, type IconName } from '../shared/components/Icons';
import { ThemeSwitcher } from '../shared/components/ThemeSwitcher';
import { WallpaperLayer } from '../shared/components/WallpaperLayer';

interface NavEntry {
  to: string;
  label: string;
  icon: IconName;
}

/*
 * One flat list of destinations. Grouping six items under "Research" and "Operate"
 * headings added reading work without adding meaning, so the rail now just shows
 * every place you can go, labelled, in one column.
 */
const NAV_ITEMS: NavEntry[] = [
  { to: '/', label: 'Chat', icon: 'agents' },
  { to: '/papers', label: 'Papers', icon: 'papers' },
  { to: '/workspace', label: 'Files', icon: 'workspace' },
  { to: '/tools', label: 'Tools', icon: 'tools' },
  { to: '/runs', label: 'Runs', icon: 'runs' },
  { to: '/settings', label: 'Settings', icon: 'settings' },
];

/*
 * The shell is deliberately thin: a rail of destinations and the page. Everything
 * else a page needs to say about itself — its title, its actions, its status — is
 * the page's own job, so there is exactly one place to read each thing.
 */
export function AppShell({ children }: { children: ReactNode }) {
  const palette = useCommandPalette();

  return (
    <>
      <WallpaperLayer />
      <div className="app-shell">
        <nav className="rail" aria-label="Primary">
          <Link className="rail-brand" to="/" aria-label="ScholarWeave home" title="ScholarWeave">
            <span className="rail-mark">SW</span>
          </Link>

          <div className="rail-nav">
            {NAV_ITEMS.map((item) => (
              <NavLink key={item.to} to={item.to} title={item.label}>
                <Icon name={item.icon} size={19} />
                <span className="rail-label">{item.label}</span>
              </NavLink>
            ))}
          </div>

          <div className="rail-foot">
            <button
              type="button"
              className="rail-action search-trigger"
              aria-label="Search"
              title="Search (⌘K)"
              onClick={() => palette.setOpen(true)}
            >
              <Icon name="search" size={18} />
              <span className="rail-label">Search</span>
            </button>
            <ThemeSwitcher />
          </div>
        </nav>

        <main className="app-main">{children}</main>

        <CommandPalette open={palette.open} onClose={palette.close} />
      </div>
    </>
  );
}
