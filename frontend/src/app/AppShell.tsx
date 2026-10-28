import { lazy, Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from 'react';
import { Link, useLocation } from './router';
import { AskContextProvider, type AskApi, type AskOptions } from './ask';
import { CommandPalette } from './CommandPalette';
import { Icon } from '../shared/components/Icons';
import { ThemeSwitcher } from '../shared/components/ThemeSwitcher';
import { Loading } from '../shared/components/Ui';
import { PaneResizer, readStoredWidth, storeWidth } from '../shared/components/PaneResizer';

const AskPanel = lazy(() => import('../features/chat/AskPanel'));
const ASK_WIDTH_KEY = 'scholarweave.assistant.width';
const ASK_WIDTH = { value: 560, min: 320, max: 960 };

/*
 * Research material is the face of the app: the bar names what you have
 * collected and what you are working on. The assistant is summoned (Ctrl+J)
 * and takes over part of the screen only while you need it.
 */
const NAV = [
  { to: '/', label: 'Papers', match: ['/', '/library', '/papers'], exclude: ['/library/notes'] },
  { to: '/library/notes', label: 'Notes', match: ['/library/notes', '/workspace'] },
  { to: '/tracks', label: 'Tracks', match: ['/tracks'] },
  { to: '/deep-work', label: 'Reviews', match: ['/deep-work'] },
  { to: '/runs', label: 'Runs', match: ['/runs'] },
  { to: '/brief', label: 'Brief', match: ['/brief'] },
];

function isActive(pathname: string, item: (typeof NAV)[number]) {
  if (item.exclude?.some((prefix) => pathname.startsWith(prefix))) return false;
  return item.match.some((prefix) => prefix === '/' ? pathname === '/' : pathname === prefix || pathname.startsWith(`${prefix}/`));
}

export function AppShell({ children }: { children: ReactNode }) {
  const { pathname } = useLocation();
  const [askDraft, setAskDraft] = useState<(AskOptions & { text?: string; nonce: number }) | null>(null);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [askWidth, setAskWidth] = useState(() => readStoredWidth(ASK_WIDTH_KEY, ASK_WIDTH));
  const [viewportWidth, setViewportWidth] = useState(() => document.documentElement.clientWidth || window.innerWidth);
  const [resizingAsk, setResizingAsk] = useState(false);
  const askRequest = useRef(0);
  const content = useRef<HTMLElement>(null);
  const onChatPage = pathname === '/chat' || pathname.startsWith('/chat/');
  const askBounds = {
    ...ASK_WIDTH,
    min: Math.min(ASK_WIDTH.min, viewportWidth),
    max: Math.min(ASK_WIDTH.max, viewportWidth <= 900 ? viewportWidth : viewportWidth - 480),
  };
  const visibleAskWidth = Math.min(askWidth, askBounds.max);

  useEffect(() => {
    const resize = () => {
      setViewportWidth(document.documentElement.clientWidth || window.innerWidth);
      setResizingAsk(false);
    };
    window.addEventListener('resize', resize);
    return () => window.removeEventListener('resize', resize);
  }, []);

  useLayoutEffect(() => {
    const element = content.current;
    const motion = window.matchMedia?.('(prefers-reduced-motion: reduce)');
    if (!element?.animate || motion?.matches) return;
    const animation = element.animate(
      [{ opacity: 0, transform: 'translateY(6px)' }, { opacity: 1, transform: 'none' }],
      { duration: 220, easing: 'cubic-bezier(.2,.7,.2,1)' },
    );
    const reduceMotion = () => { if (motion?.matches) animation.cancel(); };
    motion?.addEventListener('change', reduceMotion);
    return () => { animation.cancel(); motion?.removeEventListener('change', reduceMotion); };
  }, [pathname]);

  const ask = useCallback((text?: string, options?: AskOptions) => {
    setViewportWidth(document.documentElement.clientWidth || window.innerWidth);
    setAskDraft({ text, ...options, nonce: ++askRequest.current });
  }, []);
  const close = useCallback(() => { setAskDraft(null); setResizingAsk(false); }, []);
  const askApi = useMemo<AskApi>(() => ({ ask, close, open: askDraft !== null }), [ask, close, askDraft]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const mod = event.ctrlKey || event.metaKey;
      if (mod && !event.shiftKey && !event.altKey && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        setPaletteOpen((value) => !value);
      } else if (mod && !event.shiftKey && !event.altKey && event.key.toLowerCase() === 'j' && !onChatPage) {
        event.preventDefault();
        if (askDraft) close();
        else ask();
      } else if (event.key === 'Escape' && !event.defaultPrevented && askDraft && !paletteOpen) {
        const target = event.target instanceof Element ? event.target : null;
        if (target?.closest('.ask-panel details[open], .ask-panel [role="dialog"]')) return;
        close();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [askDraft, paletteOpen, onChatPage, ask, close]);

  useEffect(() => { if (onChatPage) close(); }, [onChatPage, close]);

  return (
    <AskContextProvider value={askApi}>
      <div className={`app-shell${askDraft ? ' ask-open' : ''}${resizingAsk ? ' resizing-assistant' : ''}`}
        style={{ '--assistant-width': `${visibleAskWidth}px` } as CSSProperties}>
        <header className="topbar">
          <Link className="topbar-brand" to="/" aria-label="ScholarWeave home" title="ScholarWeave">
            <span className="rail-mark">SW</span>
            <span className="topbar-wordmark">ScholarWeave</span>
          </Link>
          <nav className="topbar-nav" aria-label="Primary">
            {NAV.map((item) => {
              const active = isActive(pathname, item);
              return (
                <Link key={item.to} to={item.to} className={active ? 'nav-link active' : 'nav-link'} aria-current={active ? 'page' : undefined}>
                  {item.label}
                </Link>
              );
            })}
          </nav>
          <span className="topbar-spacer" />
          {!onChatPage ? (
            <button type="button" className="ask-trigger" aria-label="Explore ideas" title="Explore ideas" onClick={() => ask(undefined, { intent: 'ideation' })}>
              <Icon name="sparkle" size={15} /><span>Explore ideas</span>
            </button>
          ) : null}
          <button type="button" className="search-trigger" onClick={() => setPaletteOpen(true)} aria-label="Search everything (Ctrl+K)">
            <Icon name="search" size={15} />
            <span>Search</span>
            <kbd>Ctrl K</kbd>
          </button>
          {!onChatPage ? (
            <button
              type="button"
              className={`ask-trigger${askDraft ? ' active' : ''}`}
              aria-pressed={askDraft !== null}
              aria-label="Ask"
              title="Ask the assistant (Ctrl+J)"
              onClick={() => (askDraft ? close() : ask())}
            >
              <Icon name="sparkle" size={15} />
              <span>Ask</span>
            </button>
          ) : null}
          <div className="topbar-tools">
            <ThemeSwitcher />
            <Link to="/settings" title="Settings" className={`nav-link settings-trigger${pathname === '/settings' ? ' active' : ''}`} aria-current={pathname === '/settings' ? 'page' : undefined}>
              <Icon name="settings" size={20} />
              <span>Settings</span>
            </Link>
          </div>
        </header>

        <main ref={content} className="app-main">{children}</main>

        {askDraft ? (
          <aside className="ask-panel" aria-label="Assistant">
            <PaneResizer side="left" label="Resize assistant" width={visibleAskWidth} bounds={askBounds}
              onResize={(width) => { setAskWidth(width); storeWidth(ASK_WIDTH_KEY, width); }}
              onResizingChange={setResizingAsk} />
            <Suspense fallback={<Loading label="Opening the assistant…" />}>
              <AskPanel key={askDraft.nonce} draft={askDraft.text} autoSend={askDraft.send} context={askDraft.context} intent={askDraft.intent} onClose={close} />
            </Suspense>
          </aside>
        ) : null}
        {paletteOpen ? <CommandPalette onClose={() => setPaletteOpen(false)} onAsk={ask} /> : null}
      </div>
    </AskContextProvider>
  );
}
