import { useEffect, useState, type ReactNode } from 'react';
import { NavLink, usePathname } from '../../lib/router';
import { Icon, type IconName } from '../common/Icon';
import { api } from '../../lib/api';

type Theme = 'amoled' | 'paper' | 'cloud';

const themes: { id: Theme; label: string }[] = [
  { id: 'paper', label: 'Paper' },
  { id: 'cloud', label: 'Cloud' },
  { id: 'amoled', label: 'Dark' },
];

function getInitialTheme(): Theme {
  const saved = localStorage.getItem('scholarweave-theme');
  const theme = themes.some(({ id }) => id === saved) ? (saved as Theme) : 'paper';
  document.documentElement.dataset.theme = theme;
  return theme;
}

function getInitialSidebarState(): boolean {
  return localStorage.getItem('scholarweave-sidebar-collapsed') === 'true';
}

function useMobileLayout(): boolean {
  const [mobile, setMobile] = useState(() => window.matchMedia('(max-width: 960px)').matches);
  useEffect(() => {
    const query = window.matchMedia('(max-width: 960px)');
    const update = () => setMobile(query.matches);
    query.addEventListener('change', update);
    return () => query.removeEventListener('change', update);
  }, []);
  return mobile;
}

interface NavItem {
  to: string;
  label: string;
  icon: IconName;
  end?: boolean;
  description: string;
}

const navSections: { title: string; items: NavItem[] }[] = [
  {
    title: 'Workspace',
    items: [
      { to: '/', label: 'Dashboard', icon: 'dashboard', end: true, description: 'Everything happening in your local workspace' },
      { to: '/papers', label: 'Papers', icon: 'papers', description: 'Upload PDFs and extract text, tables, and figures' },
      { to: '/notes', label: 'Notes', icon: 'notes', description: 'Markdown files under your workspace folder' },
    ],
  },
  {
    title: 'Build',
    items: [
      { to: '/agents', label: 'Agents', icon: 'workflow', description: 'Build and run OpenAI Agents SDK agents, tools, and handoffs' },
      { to: '/runs', label: 'Runs', icon: 'runs', description: 'Live tokens, node output, and errors for every execution' },
      { to: '/settings', label: 'Settings', icon: 'settings', description: 'Model providers, defaults, and execution limits' },
    ],
  },
];

const allNavItems = navSections.flatMap((section) => section.items);

function activeItem(pathname: string): NavItem {
  return (
    allNavItems.find((item) => (item.end ? pathname === item.to : pathname === item.to || pathname.startsWith(`${item.to}/`))) ??
    allNavItems[0]
  );
}

type BackendState = 'unknown' | 'online' | 'offline';

function useBackendStatus() {
  const [state, setState] = useState<BackendState>('unknown');
  const [detail, setDetail] = useState('Checking backend…');

  useEffect(() => {
    let active = true;
    const check = () =>
      api
        .getHealth()
        .then((health) => {
          if (!active) return;
          setState('online');
          setDetail(health.ocr_available ? 'Backend online · OCR ready' : 'Backend online · OCR unavailable');
        })
        .catch(() => {
          if (!active) return;
          setState('offline');
          setDetail('Backend unreachable');
        });
    void check();
    const timer = window.setInterval(check, 30000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, []);

  return { state, detail };
}

export function AppShell({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<Theme>(getInitialTheme);
  const [navOpen, setNavOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(getInitialSidebarState);
  const mobileLayout = useMobileLayout();
  const pathname = usePathname();
  const current = activeItem(pathname);
  const backend = useBackendStatus();

  useEffect(() => {
    setNavOpen(false);
  }, [pathname]);

  useEffect(() => {
    document.title = `${current.label} · ScholarWeave`;
  }, [current.label]);

  const selectTheme = (nextTheme: Theme) => {
    document.documentElement.dataset.theme = nextTheme;
    localStorage.setItem('scholarweave-theme', nextTheme);
    setTheme(nextTheme);
  };

  const toggleSidebar = () => {
    if (mobileLayout) {
      setNavOpen((open) => !open);
      return;
    }
    setSidebarCollapsed((collapsed) => {
      localStorage.setItem('scholarweave-sidebar-collapsed', String(!collapsed));
      return !collapsed;
    });
  };

  return (
    <div className={`app-shell${sidebarCollapsed ? ' sidebar-collapsed' : ''}`}>
      {mobileLayout && navOpen ? <div className="sidebar-scrim" onClick={() => setNavOpen(false)} /> : null}

      <aside className={`sidebar${navOpen ? ' open' : ''}`}>
        <div className="sidebar-scroll">
          <div className="brand-card">
            <div className="brand-mark" aria-hidden="true">
              SW
            </div>
            <div>
              <h1>ScholarWeave</h1>
              <p>Local research workspace</p>
            </div>
          </div>

          <nav aria-label="Primary navigation">
            {navSections.map((section) => (
              <div className="nav-section" key={section.title}>
                <p className="nav-section-label">{section.title}</p>
                {section.items.map((item) => (
                  <NavLink
                    key={item.to}
                    to={item.to}
                    end={item.end}
                    title={item.description}
                    className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}
                  >
                    <Icon name={item.icon} size={16} />
                    <span>{item.label}</span>
                  </NavLink>
                ))}
              </div>
            ))}
          </nav>
        </div>

        <div className="sidebar-footer">
          <div className="theme-switcher" role="group" aria-label="Colour theme">
            <span className="theme-switcher-label">Theme</span>
            <div className="theme-options">
              {themes.map((option) => (
                <button
                  key={option.id}
                  type="button"
                  className={`theme-option theme-option-${option.id}`}
                  aria-label={`${option.label} theme`}
                  aria-pressed={theme === option.id}
                  title={`${option.label} theme`}
                  onClick={() => selectTheme(option.id)}
                >
                  <span className="theme-swatch" aria-hidden="true" />
                  <span>{option.label}</span>
                </button>
              ))}
            </div>
          </div>
          <p className="sidebar-note" title={backend.detail}>
            <span
              className={`status-dot ${backend.state === 'online' ? '' : backend.state === 'offline' ? 'offline' : 'unknown'}`}
              aria-hidden="true"
            />
            {backend.detail}
          </p>
        </div>
      </aside>

      <div className="app-main">
        <header className="topbar">
          <button
            type="button"
            className="button subtle icon-only sidebar-toggle"
            aria-label={mobileLayout ? 'Toggle navigation' : sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            aria-expanded={mobileLayout ? navOpen : !sidebarCollapsed}
            onClick={toggleSidebar}
          >
            <Icon name="menu" size={17} />
          </button>
          <div className="topbar-heading">
            <h1>{current.label}</h1>
            <p className="muted-text">{current.description}</p>
          </div>
        </header>
        <main className="main-content">{children}</main>
      </div>
    </div>
  );
}
