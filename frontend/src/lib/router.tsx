import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type AnchorHTMLAttributes,
  type ReactNode,
} from 'react';

type RouterContextValue = {
  pathname: string;
  search: string;
  navigate: (to: string) => void;
};

const RouterContext = createContext<RouterContextValue | null>(null);

export function RouterProvider({ children }: { children: ReactNode }) {
  const [location, setLocation] = useState(() => ({
    pathname: window.location.pathname,
    search: window.location.search,
  }));

  useEffect(() => {
    const update = () => setLocation({ pathname: window.location.pathname, search: window.location.search });
    window.addEventListener('popstate', update);
    return () => window.removeEventListener('popstate', update);
  }, []);

  const navigate = useCallback((to: string) => {
    window.history.pushState({}, '', to);
    setLocation({ pathname: window.location.pathname, search: window.location.search });
    window.scrollTo({ top: 0 });
  }, []);

  const value = useMemo(() => ({ ...location, navigate }), [location, navigate]);
  return <RouterContext.Provider value={value}>{children}</RouterContext.Provider>;
}

function useRouter() {
  const router = useContext(RouterContext);
  if (!router) {
    throw new Error('Router hooks must be used inside RouterProvider');
  }
  return router;
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
        if (!event.defaultPrevented && event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) {
          event.preventDefault();
          navigate(to);
        }
      }}
    />
  );
}

type NavLinkProps = Omit<LinkProps, 'className'> & {
  end?: boolean;
  className?: string | ((state: { isActive: boolean }) => string);
};

export function NavLink({ end = false, className, to, ...props }: NavLinkProps) {
  const { pathname } = useRouter();
  const isActive = end ? pathname === to : pathname === to || pathname.startsWith(`${to}/`);
  const resolvedClassName = typeof className === 'function' ? className({ isActive }) : className;
  return <Link to={to} className={resolvedClassName} {...props} />;
}

export function useNavigate() {
  return useRouter().navigate;
}

export function usePathname() {
  return useRouter().pathname;
}

export function useSearchParams(): [URLSearchParams] {
  const { search } = useRouter();
  return [useMemo(() => new URLSearchParams(search), [search])];
}

export function useParams(): Record<string, string | undefined> {
  const { pathname } = useRouter();
  const segments = pathname.split('/').filter(Boolean);
  if (segments[0] === 'papers') {
    return { documentId: segments[1] };
  }
  if (segments[0] === 'agents' || segments[0] === 'workflows') {
    return { workflowId: segments[1] === 'new' ? undefined : segments[1] };
  }
  if (segments[0] === 'runs') {
    return { runId: segments[1] };
  }
  return {};
}
