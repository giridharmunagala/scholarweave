// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { RouterProvider } from '../../app/router';
import { ExtendedWorkPanel } from './ExtendedWorkPanel';

describe('ExtendedWorkPanel', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.restoreAllMocks();
  });

  it('shows saved work-note content and copies it', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });
    await act(async () => {
      root.render(
        <RouterProvider>
          <ExtendedWorkPanel
            events={[
              {
                sequence: 1,
                event_type: 'extended.plan.updated',
                payload: {
                  tasks: [
                    { id: 'evidence', title: 'Collect evidence', status: 'completed' },
                    { id: 'compare', title: 'Compare results', status: 'in_progress' },
                  ],
                },
              },
              {
                sequence: 2,
                event_type: 'extended.note.saved',
                payload: {
                  note_id: 'evidence-1',
                  task_id: 'evidence',
                  title: 'Evidence notes',
                  summary: 'Two sources agree.',
                  content: 'Detailed **findings** to reuse.',
                  workspace_path: 'extended-work-notes/run-1/evidence-1.md',
                },
              },
            ]}
          />
        </RouterProvider>,
      );
    });

    const note = container.querySelector<HTMLDetailsElement>('.extended-work-note')!;
    expect(note.textContent).toContain('Evidence notes');
    await act(async () => {
      note.open = true;
      note.querySelector<HTMLButtonElement>('.extended-work-note-copy')!.click();
    });

    expect(writeText).toHaveBeenCalledWith('Detailed **findings** to reuse.');
    expect(note.textContent).toContain('Copied');
    expect(note.querySelector<HTMLAnchorElement>('a')?.getAttribute('href')).toBe(
      '/workspace?path=extended-work-notes%2Frun-1%2Fevidence-1.md',
    );
  });
});
