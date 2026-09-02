// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SpeechControl } from './SpeechControl';

describe('speech control', () => {
  const containers: HTMLDivElement[] = [];

  afterEach(() => {
    for (const container of containers) container.remove();
    containers.length = 0;
    delete (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT;
  });

  it('offers an on-device model download from the chat composer', async () => {
    const container = document.createElement('div');
    containers.push(container);
    document.body.appendChild(container);
    (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
    const root = createRoot(container);
    const install = vi.fn();

    await act(async () => {
      root.render(
        <SpeechControl
          mode="builtin"
          onModeChange={vi.fn()}
          builtIn={{
            state: 'not_installed',
            available: true,
            installed: false,
            running: false,
            model: 'nvidia/nemotron-speech-streaming-en-0.6b',
            downloaded_bytes: 0,
            total_bytes: 100,
            error: null,
          }}
          options={[]}
          modelReference={{}}
          onModelReferenceChange={vi.fn()}
          recording={false}
          starting={false}
          transcribing={false}
          busy={false}
          onInstallBuiltIn={install}
          onToggle={vi.fn()}
        />,
      );
    });

    await act(async () => {
      container.querySelector<HTMLButtonElement>('[aria-label="Speech source"]')!.click();
    });
    await act(async () => {
      Array.from(container.querySelectorAll('button'))
        .find((button) => button.textContent === 'Download on-device model')!
        .click();
    });

    expect(install).toHaveBeenCalledOnce();
    act(() => root.unmount());
  });
});
