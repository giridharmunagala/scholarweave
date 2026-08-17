// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ThemeProvider } from '../shared/theme/ThemeProvider';
import { AppShell } from './AppShell';
import { RouterProvider } from './router';

const CONVERSATIONS = [
  {
    id: 'conversation-1',
    title: 'A long research session title that should wrap inside the navigation',
    kind: 'autonomous',
    agent_revision_id: null,
    model_reference: {},
    session_policy: {},
    status: 'idle',
    last_message_preview: 'Compare the methods and results across the selected papers.',
    created_at: '2026-08-17T10:00:00Z',
    updated_at: '2026-08-17T10:00:00Z',
  },
];

describe('application chat navigation', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
    window.history.replaceState({}, '', '/');
    vi.stubGlobal('scrollTo', vi.fn());
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve(CONVERSATIONS),
        } as Response),
      ),
    );
    container = document.createElement('div');
    document.body.appendChild(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  it('nests previous sessions beneath the Agent navigation item', async () => {
    root = createRoot(container);
    await act(async () => {
      root.render(
        <ThemeProvider>
          <RouterProvider>
            <AppShell>
              <main>Chat</main>
            </AppShell>
          </RouterProvider>
        </ThemeProvider>,
      );
      await Promise.resolve();
    });

    expect(container.querySelector('.nav-agent-branch .nav-chat-tree')).not.toBeNull();
    expect(container.querySelector('.nav-chat-session.active')?.textContent).toContain(
      CONVERSATIONS[0].title,
    );
    expect(container.querySelector('.nav-chat-new')?.textContent).toContain('New chat');
    expect(container.querySelector('.topbar-new-chat')).toBeNull();
    expect(container.querySelector('.app-main.chat-main')).not.toBeNull();
    expect(container.querySelector('.app-body')?.getAttribute('data-chat')).toBe('true');
    expect(container.querySelector('.search-trigger')?.getAttribute('aria-label')).toBe('Search');
  });
});
