// @vitest-environment jsdom
import { act, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { expect, it, vi } from 'vitest';
import { Link, RouterProvider, useLocation, useNavigationGuard } from './router';

it('guards links, browser navigation and unloading only while a draft is dirty', async () => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  const original = window.location.href;
  window.history.replaceState({}, '', '/library/notes');
  vi.stubGlobal('scrollTo', vi.fn());
  const confirm = vi.fn(() => false);
  vi.stubGlobal('confirm', confirm);
  function Page() {
    const [dirty, setDirty] = useState(true);
    useNavigationGuard(dirty, 'Discard draft?');
    const { pathname } = useLocation();
    return <><p>{pathname}</p><Link to="/papers">Papers</Link><button onClick={() => setDirty(false)}>Clean</button></>;
  }
  const container = document.createElement('div');
  const root = createRoot(container);
  try {
    await act(async () => root.render(<RouterProvider><Page /></RouterProvider>));
    await act(async () => container.querySelector('a')!.click());
    expect(window.location.pathname).toBe('/library/notes');
    const unload = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(unload);
    expect(unload.defaultPrevented).toBe(true);
    await act(async () => {
      window.history.replaceState({}, '', '/papers');
      window.dispatchEvent(new PopStateEvent('popstate'));
    });

    expect(window.location.pathname).toBe('/library/notes');
    expect(container.textContent).toContain('/library/notes');
    confirm.mockReturnValue(true);
    await act(async () => {
      window.history.replaceState({}, '', '/papers');
      window.dispatchEvent(new PopStateEvent('popstate'));
    });
    expect(container.textContent).toContain('/papers');
    await act(async () => container.querySelector('button')!.click());
    confirm.mockClear();
    await act(async () => container.querySelector('a')!.click());
    expect(confirm).not.toHaveBeenCalled();
    const cleanUnload = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(cleanUnload);
    expect(cleanUnload.defaultPrevented).toBe(false);
  } finally {
    await act(async () => root.unmount());
    window.history.replaceState({}, '', original);
    vi.unstubAllGlobals();
  }
});

it('restores canceled Back and Forward without deleting history entries', async () => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  const original = window.location.href;
  window.history.replaceState({}, '', '/first');
  vi.stubGlobal('scrollTo', vi.fn());
  const confirm = vi.fn(() => true);
  vi.stubGlobal('confirm', confirm);
  function Page() {
    useNavigationGuard(true, 'Discard?');
    const { pathname } = useLocation();
    return <><p>{pathname}</p><Link to="/second">Second</Link><Link to="/third">Third</Link></>;
  }
  const container = document.createElement('div');
  const root = createRoot(container);
  const travel = async (delta: number, canceled = false) => {
    await act(async () => {
      await new Promise<void>((resolve) => {
        let remaining = canceled ? 2 : 1;
        const listener = () => {
          remaining -= 1;
          if (!remaining) { window.removeEventListener('popstate', listener); resolve(); }
        };
        window.addEventListener('popstate', listener);
        window.history.go(delta);
      });
    });
  };
  try {
    await act(async () => root.render(<RouterProvider><Page /></RouterProvider>));
    await act(async () => container.querySelector<HTMLAnchorElement>('a[href="/second"]')!.click());
    await act(async () => container.querySelector<HTMLAnchorElement>('a[href="/third"]')!.click());
    const length = window.history.length;
    confirm.mockReturnValue(false);
    await travel(-1, true);
    expect(window.location.pathname).toBe('/third');
    expect(container.querySelector('p')!.textContent).toBe('/third');
    expect(window.history.length).toBe(length);
    confirm.mockReturnValue(true);
    await travel(-1);
    expect(window.location.pathname).toBe('/second');
    confirm.mockReturnValue(false);
    await travel(1, true);
    expect(window.location.pathname).toBe('/second');
    expect(window.history.length).toBe(length);
    confirm.mockReturnValue(true);
    await travel(1);
    expect(window.location.pathname).toBe('/third');
    await travel(-2);
    expect(window.location.pathname).toBe('/first');
  } finally {
    await act(async () => root.unmount());
    window.history.replaceState({}, '', original);
    vi.unstubAllGlobals();
  }
});
