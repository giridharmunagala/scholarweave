// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { describe, expect, it, vi } from 'vitest';
import { ResidencyPanel } from './ResidencyPanel';

describe('ResidencyPanel', () => {
  it('shows model mismatches and requires external-load attestation before confirmation', async () => {
    (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
    const requests: Array<{ url: string; init?: RequestInit }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ url: String(input), init });
      return new Response(JSON.stringify({
        profile_id: 'local', enabled: true, resident_model: 'main', confirmed: false,
        paused: true, session_mode: 'interactive', active_requests: 0,
        interactive_queued: 0, background_queued: 1,
        queue: [{ profile_id: 'local', model: 'small', priority: 'background', blocked_by_residency: true }],
      }), { status: 200, headers: { 'content-type': 'application/json' } });
    }));
    const container = document.createElement('div');
    const root = createRoot(container);
    try {
      await act(async () => { root.render(<ResidencyPanel providers={[]} />); });
      expect(container.textContent).toContain('small (background) — waiting for residency confirmation');
      expect(container.textContent).toContain('never loads, unloads, or downgrades');
      const confirm = Array.from(container.querySelectorAll('button'))
        .find((button) => button.textContent === 'Confirm external load and resume')!;
      expect(confirm.disabled).toBe(true);
      const input = container.querySelector('input:not([type="checkbox"])') as HTMLInputElement;
      await act(async () => {
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, 'small');
        input.dispatchEvent(new Event('input', { bubbles: true }));
      });
      expect(confirm.disabled).toBe(true);
      await act(async () => { (container.querySelector('input[type="checkbox"]') as HTMLInputElement).click(); });
      expect(confirm.disabled).toBe(false);
      await act(async () => { confirm.click(); });
      const request = requests.find((entry) => entry.url.endsWith('/residency/confirm'));
      expect(JSON.parse(String(request?.init?.body))).toEqual({
        model: 'small', externally_loaded: true, session_mode: 'interactive',
      });
    } finally {
      await act(async () => { root.unmount(); });
      vi.unstubAllGlobals();
    }
  });
});
