// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ThemeProvider } from '../shared/theme/ThemeProvider';
import { AppShell } from './AppShell';
import { RouterProvider } from './router';

describe('application shell', () => {
  let container: HTMLDivElement;
  let root: Root;

  const render = async (pathname: string) => {
    window.history.replaceState({}, '', pathname);
    root = createRoot(container);
    await act(async () => {
      root.render(
        <ThemeProvider>
          <RouterProvider>
            <AppShell>
              <main>Page</main>
            </AppShell>
          </RouterProvider>
        </ThemeProvider>,
      );
      await Promise.resolve();
    });
  };

  beforeEach(() => {
    localStorage.clear();
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

  it('offers every destination once, in one labelled rail', async () => {
    await render('/');

    const labels = Array.from(container.querySelectorAll('.rail .nav-link .rail-label')).map(
      (node) => node.textContent,
    );
    expect(labels).toEqual(['Research', 'Library', 'Settings']);
    expect(container.querySelector('.rail .nav-link.active .rail-label')?.textContent).toBe('Research');

    // The rail is the only navigation chrome: no top bar, no breadcrumb, no drawer.
    expect(container.querySelector('.topbar')).toBeNull();
    expect(container.querySelector('.breadcrumb')).toBeNull();
    expect(container.querySelector('.sidebar')).toBeNull();
    expect(container.querySelector('[aria-label="Resize navigation sidebar"]')).toBeNull();

    // Conversations are listed by the chat page alone, never mirrored in the rail.
    expect(container.querySelector('.nav-chat-tree')).toBeNull();

    expect(container.querySelector('.search-trigger')).toBeNull();
    expect(container.querySelector('.theme-trigger')).not.toBeNull();
  });

  it('marks the rail entry for the current section', async () => {
    await render('/library/123');

    expect(container.querySelector('.rail .nav-link.active .rail-label')?.textContent).toBe('Library');
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
    expect(options.map((option) => option.textContent)).toEqual(
      expect.arrayContaining([
        expect.stringContaining('Paper'),
        expect.stringContaining('Slate'),
        expect.stringContaining('Match system'),
      ]),
    );

    await act(async () => {
      options.find((option) => option.textContent?.includes('Match system'))!.click();
    });
    expect(localStorage.getItem('scholarweave-theme')).toBe('system');
    expect(container.querySelector('[role="menu"]')).toBeNull();
  });
});
