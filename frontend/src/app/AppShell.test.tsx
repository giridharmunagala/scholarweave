// @vitest-environment jsdom
import { act, type ReactNode } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ThemeProvider } from '../shared/theme/ThemeProvider';
import { AppShell } from './AppShell';
import { RouterProvider } from './router';

describe('application shell', () => {
  let container: HTMLDivElement;
  let root: Root;

  const render = async (pathname: string, children: ReactNode = <main>Page</main>) => {
    window.history.replaceState({}, '', pathname);
    root = createRoot(container);
    await act(async () => {
      root.render(
        <ThemeProvider>
          <RouterProvider>
            <AppShell>
              {children}
            </AppShell>
          </RouterProvider>
        </ThemeProvider>,
      );
      await Promise.resolve();
    });
  };

  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
    vi.stubGlobal('scrollTo', vi.fn());
    container = document.createElement('div');
    document.body.appendChild(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  it('summons the assistant with Ctrl+J and opens search with Ctrl+K', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('[]', { status: 200, headers: { 'Content-Type': 'application/json' } })));
    await render('/');

    await act(async () => { window.dispatchEvent(new KeyboardEvent('keydown', { key: 'j', ctrlKey: true })); });
    expect(container.querySelector('.app-shell')?.classList.contains('ask-open')).toBe(true);
    expect(container.querySelector('.ask-panel[aria-label="Assistant"]')).not.toBeNull();

    await act(async () => { window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })); });
    expect(container.querySelector('.ask-panel')).toBeNull();

    await act(async () => { window.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', ctrlKey: true })); });
    expect(container.querySelector('.palette')).not.toBeNull();
  });

  it('opens the compact theme picker and supports matching the system theme', async () => {
    vi.stubGlobal('matchMedia', vi.fn(() => ({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })));
    await render('/');

    await act(async () => {
      container.querySelector<HTMLButtonElement>('.theme-trigger')!.click();
    });
    const options = [...container.querySelectorAll<HTMLElement>('[role="menuitemradio"]')];
    expect(options.map((option) => option.textContent)).toEqual([
      expect.stringContaining('Light'),
      expect.stringContaining('Sepia'),
      expect.stringContaining('Dark'),
      expect.stringContaining('Match system'),
    ]);

    await act(async () => {
      options.find((option) => option.textContent?.includes('Match system'))!.click();
    });
    expect(localStorage.getItem('scholarweave-theme')).toBe('system');
    expect(container.querySelector('[role="menu"]')).toBeNull();
  });

  it('defaults to white even when the operating system prefers dark', async () => {
    vi.stubGlobal('matchMedia', vi.fn(() => ({
      matches: true,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })));
    await render('/');

    expect(document.documentElement.dataset.theme).toBe('light');
    expect(document.documentElement.dataset.themePreference).toBe('light');
    expect(localStorage.getItem('scholarweave-theme')).toBe('light');
  });

  it.each(['sepia', 'dark', 'system'])('retains the explicitly saved %s theme', async (preference) => {
    vi.stubGlobal('matchMedia', vi.fn(() => ({
      matches: true,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })));
    localStorage.setItem('scholarweave-theme', preference);
    await render('/');

    expect(document.documentElement.dataset.theme).toBe(preference === 'system' ? 'dark' : preference);
    expect(localStorage.getItem('scholarweave-theme')).toBe(preference);
  });

  it('keeps the resize edge inside a narrow viewport with a reserved scrollbar gutter', async () => {
    vi.stubGlobal('innerWidth', 390);
    vi.stubGlobal('fetch', vi.fn(async () => new Response('[]', { status: 200 })));
    const clientWidth = vi.spyOn(document.documentElement, 'clientWidth', 'get').mockReturnValue(390);
    try {
      await render('/');
      clientWidth.mockReturnValue(381);
      await act(async () => { window.dispatchEvent(new KeyboardEvent('keydown', { key: 'j', ctrlKey: true })); });
      const handle = container.querySelector('[aria-label="Resize assistant"]')!;
      expect(handle.getAttribute('aria-valuemax')).toBe('381');
      expect(handle.getAttribute('aria-valuenow')).toBe('381');
      expect(container.querySelector<HTMLElement>('.app-shell')!.style.getPropertyValue('--assistant-width')).toBe('381px');
    } finally {
      clientWidth.mockRestore();
    }
  });

  it('navigates to batch Runs and marks its page active', async () => {
    await render('/');
    const runs = container.querySelector<HTMLAnchorElement>('nav[aria-label="Primary"] a[href="/runs"]')!;
    await act(async () => runs.click());
    expect(window.location.pathname).toBe('/runs');
    expect(runs.getAttribute('aria-current')).toBe('page');
  });
});
