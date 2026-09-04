import type { ReactNode } from 'react';
import { Link, NavLink } from './router';
import { Icon, type IconName } from '../shared/components/Icons';
import { ThemeSwitcher } from '../shared/components/ThemeSwitcher';

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
  { to: '/', label: 'Research', icon: 'search' },
  { to: '/library', label: 'Library', icon: 'papers' },
  { to: '/settings', label: 'Settings', icon: 'settings' },
];

/*
 * The shell is deliberately thin: a rail of destinations and the page. Everything
 * else a page needs to say about itself — its title, its actions, its status — is
 * the page's own job, so there is exactly one place to read each thing.
 */
export function AppShell({ children }: { children: ReactNode }) {
  return (
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
          <ThemeSwitcher />
        </div>
      </nav>

      <main className="app-main">{children}</main>
    </div>
  );
}
