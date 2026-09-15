import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type AnchorHTMLAttributes,
  type ReactNode,
} from 'react';

interface RouterState {
  pathname: string;
  search: string;
  navigate: (to: string, options?: { replace?: boolean }) => void;
  block: (guard: () => boolean) => () => void;
}

const RouterContext = createContext<RouterState | null>(null);

export function RouterProvider({ children }: { children: ReactNode }) {
  const guards = useRef(new Set<() => boolean>());
  const currentUrl = useRef(window.location.pathname + window.location.search);
  const historyIndex = useRef<number>(window.history.state?.scholarweaveIndex ?? 0);
  const restoringHistory = useRef(false);
  const canLeave = useCallback(() => [...guards.current].every((guard) => guard()), []);
  const block = useCallback((guard: () => boolean) => {
    guards.current.add(guard);
    return () => { guards.current.delete(guard); };
  }, []);
  const [location, setLocation] = useState(() => ({
    pathname: window.location.pathname,
    search: window.location.search,
  }));

  useEffect(() => {
    window.history.replaceState({ ...window.history.state, scholarweaveIndex: historyIndex.current }, '', window.location.href);
    const update = (event: PopStateEvent) => {
      if (restoringHistory.current) {
        restoringHistory.current = false;
        return;
      }
      const targetIndex: unknown = event.state?.scholarweaveIndex;
      if (!canLeave()) {
        if (typeof targetIndex === 'number' && targetIndex !== historyIndex.current) {
          restoringHistory.current = true;
          window.history.go(historyIndex.current - targetIndex);
        } else {
          window.history.replaceState(
            { ...window.history.state, scholarweaveIndex: historyIndex.current }, '', currentUrl.current,
          );
        }
        return;
      }
      if (typeof targetIndex === 'number') historyIndex.current = targetIndex;
      currentUrl.current = window.location.pathname + window.location.search;
      setLocation({ pathname: window.location.pathname, search: window.location.search });
    };
    window.addEventListener('popstate', update);
    return () => window.removeEventListener('popstate', update);
  }, [canLeave]);

  const navigate = useCallback((to: string, options?: { replace?: boolean }) => {
    if (restoringHistory.current || !canLeave()) return;
    if (!options?.replace) historyIndex.current += 1;
    window.history[options?.replace ? 'replaceState' : 'pushState'](
      { scholarweaveIndex: historyIndex.current }, '', to,
    );
    currentUrl.current = window.location.pathname + window.location.search;
    setLocation({ pathname: window.location.pathname, search: window.location.search });
    window.scrollTo({ top: 0 });
  }, [canLeave]);

  const value = useMemo(() => ({ ...location, navigate, block }), [location, navigate, block]);
  return <RouterContext.Provider value={value}>{children}</RouterContext.Provider>;
}

export function useNavigationGuard(active: boolean, message: string, allowDiscard = true) {
  const { block } = useRouter();
  useEffect(() => {
    if (!active) return;
    const unblock = block(() => allowDiscard && window.confirm(message));
    const beforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', beforeUnload);
    return () => {
      unblock();
      window.removeEventListener('beforeunload', beforeUnload);
    };
  }, [active, message, allowDiscard, block]);
}

function useRouter(): RouterState {
  const value = useContext(RouterContext);
  if (!value) throw new Error('Router hooks require RouterProvider.');
  return value;
}

type LinkProps = Omit<AnchorHTMLAttributes<HTMLAnchorElement>, 'href'> & { to: string };

export function Link({ to, onClick, ...props }: LinkProps) {
  const { navigate } = useRouter();
  return (
    <a
      href={to}
      {...props}
      onClick={(event) => {
        onClick?.(event);
        if (
          !event.defaultPrevented &&
          event.button === 0 &&
          !event.metaKey &&
          !event.ctrlKey &&
          !event.shiftKey &&
          !event.altKey
        ) {
          event.preventDefault();
          navigate(to);
        }
      }}
    />
  );
}

export function NavLink({
  to,
  children,
  title,
}: {
  to: string;
  children: ReactNode;
  title?: string;
}) {
  const { pathname } = useRouter();
  const active = pathname === to || (to !== '/' && pathname.startsWith(`${to}/`));
  return (
    <Link to={to} title={title} className={active ? 'nav-link active' : 'nav-link'} aria-current={active ? 'page' : undefined}>
      {children}
    </Link>
  );
}

export function useNavigate() {
  return useRouter().navigate;
}

export function useLocation() {
  const { pathname, search } = useRouter();
  return { pathname, search, searchParams: new URLSearchParams(search) };
}

export function pathSegment(index: number): string | undefined {
  return window.location.pathname.split('/').filter(Boolean)[index];
}
