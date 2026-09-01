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
    expect(labels).toEqual(['Chat', 'Papers', 'Files', 'Tools', 'Runs', 'Settings']);
    expect(container.querySelector('.rail .nav-link.active .rail-label')?.textContent).toBe('Chat');

    // The rail is the only navigation chrome: no top bar, no breadcrumb, no drawer.
    expect(container.querySelector('.topbar')).toBeNull();
    expect(container.querySelector('.breadcrumb')).toBeNull();
    expect(container.querySelector('.sidebar')).toBeNull();
    expect(container.querySelector('[aria-label="Resize navigation sidebar"]')).toBeNull();

    // Conversations are listed by the chat page alone, never mirrored in the rail.
    expect(container.querySelector('.nav-chat-tree')).toBeNull();

    expect(container.querySelector('.search-trigger')?.getAttribute('aria-label')).toBe('Search');
    expect(container.querySelector('.theme-trigger')).not.toBeNull();
  });

  it('marks the rail entry for the current section', async () => {
    await render('/papers/123');

    expect(container.querySelector('.rail .nav-link.active .rail-label')?.textContent).toBe('Papers');
  });
});
