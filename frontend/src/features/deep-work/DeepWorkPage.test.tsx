// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { RouterProvider } from '../../app/router';
import DeepWorkPage from './DeepWorkPage';
import type { Run } from '../chat/api';

const model = { provider_profile_id: 'local', model: 'research' };
const conversation = {
  id: 'task-1', title: 'Investigate evidence', kind: 'deep_work', model_reference: model,
  session_policy: {}, status: 'running', track_id: 'track-1', last_message_preview: 'Compare methods',
  created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z', items: [],
};
const initialRun: Run = {
  id: 'run-1', conversation_id: 'task-1', agent_name: 'Deep Work Coordinator', status: 'running',
  input: 'Compare methods', final_output: null, last_agent_name: null, usage: {}, error: null,
  cancel_requested: false, created_at: conversation.created_at, started_at: conversation.created_at,
  finished_at: null, items: [], events: [], epochs: [], tool_attempts: [], goal_state: null,
};
let container: HTMLDivElement;
let root: Root;
let run: Run;
let rejectSend: boolean;
let writes: { path: string; body: Record<string, unknown> }[];
let reads: string[];

function button(text: string) {
  const found = [...container.querySelectorAll('button')].find((item) => item.textContent === text);
  if (!found) throw new Error(`Missing button ${text}`);
  return found;
}
async function input(text: string) {
  await act(async () => {
    const textarea = container.querySelector('textarea')!;
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(textarea, text);
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
  });
}
async function mount(url: string) {
  window.history.replaceState({}, '', url);
  await act(async () => root.render(<RouterProvider><DeepWorkPage /></RouterProvider>));
}
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  run = { ...initialRun };
  rejectSend = false;
  writes = []; reads = [];
  vi.stubGlobal('scrollTo', vi.fn());
  vi.stubGlobal('confirm', vi.fn(() => true));
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost');
    const respond = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
    if (init?.method === 'POST') {
      writes.push({ path: url.pathname, body: JSON.parse(String(init.body)) });
      if (url.pathname.endsWith('/messages')) {
        if (rejectSend) return respond({ message: 'Provider unavailable' }, 503);
        return respond({ conversation, run });
      }
      if (url.pathname.endsWith('/steering')) return respond({ id: 'instruction-1', content: 'Focus on limitations', status: 'queued' });
      if (url.pathname.endsWith('/cancel')) {
        run = { ...run, status: 'cancelled' };
        return respond(run);
      }
      if (url.pathname.endsWith('/stop-and-answer')) {
        const stopped = { ...run, status: 'cancelled' };
        run = { ...run, id: 'answer-1' };
        return respond({ stopped_run: stopped, answer_run: run });
      }
      if (url.pathname === '/api/deep-work/conversations') return respond(conversation);
      throw new Error(`Unexpected write ${url.pathname}`);
    }
    reads.push(url.pathname + url.search);
    if (url.pathname === '/api/tracks') return respond([{ id: 'track-1', title: 'Adaptive methods' }]);
    if (url.pathname === '/api/providers') return respond([{ id: 'local', name: 'Local', kind: 'openai_compatible',
      models: [{ name: 'research', enabled: true, capabilities: ['chat'] }] }]);
    if (url.pathname === '/api/settings') return respond({ default_model_references: { chat: model } });
    if (url.pathname === '/api/deep-work/conversations') return respond([conversation]);
    if (url.pathname === '/api/agent/conversations/task-1') return respond(conversation);
    if (url.pathname === '/api/runs') return respond([run]);
    if (url.pathname === '/api/literature-reviews') return respond([]);
    throw new Error(`Unexpected read ${url.pathname}`);
  }));
  container = document.createElement('div');
  document.body.append(container);
  root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('dedicated Deep Work', () => {
  it('opens a track-scoped review workspace without a chat composer', async () => {
    await mount('/deep-work?track=track-1');
    expect(container.querySelector('h1')?.textContent).toBe('Deep Work');
    expect(container.querySelector('[aria-label="Literature review workspace"]')).not.toBeNull();
    expect(reads).toContain('/api/literature-reviews?track_id=track-1');
    expect(container.querySelector('.chat-composer')).toBeNull();
    expect(container.querySelector('a[href="/tracks?id=track-1"]')).not.toBeNull();
    expect(container.textContent).toContain("First, approve the model's plan");
  });

  it('starts a track-linked task with explicit Thorough effort on the dedicated endpoint', async () => {
    await mount('/deep-work?view=tasks&track=track-1');
    await input('Compare the evidence and identify gaps');
    await act(async () => button('Start task').click());
    expect(writes[0]).toEqual({ path: '/api/deep-work/conversations', body: { model_reference: model, track_id: 'track-1' } });
    expect(writes[1]).toEqual({ path: '/api/deep-work/conversations/task-1/messages',
      body: { content: 'Compare the evidence and identify gaps', response_effort: 'thorough', web_enabled: true } });
    expect(container.querySelector('[aria-label="Session overview"]')).not.toBeNull();
    expect(button('Stop task').disabled).toBe(false);
  });

  it('restores progress and queues steering without starting another message', async () => {
    await mount('/deep-work?view=tasks&conversation=task-1');
    expect(container.querySelector('h2')?.textContent).toBe('Research tasks');
    expect(container.textContent).toContain('Investigate evidence');
    expect(container.querySelector('[aria-label="Turn activity"]')).not.toBeNull();
    await input('Focus on limitations');
    await act(async () => button('Send instruction').click());
    expect(writes).toEqual([{ path: '/api/runs/run-1/steering', body: { content: 'Focus on limitations' } }]);
    expect(container.textContent).toContain('Instruction queued: Focus on limitations');
    expect(container.querySelector('textarea')?.value).toBe('');
  });

  it('stops work and permits an explicit continuation', async () => {
    await mount('/deep-work?view=tasks&conversation=task-1');
    await act(async () => button('Stop task').click());
    expect(writes[0].path).toBe('/api/runs/run-1/cancel');
    expect(button('Continue task').disabled).toBe(true);
    await input('Continue with local evidence only');
    await act(async () => button('Continue task').click());
    expect(writes[1].path).toBe('/api/deep-work/conversations/task-1/messages');
  });

  it('uses the existing stop-and-answer operation', async () => {
    await mount('/deep-work?view=tasks&conversation=task-1');
    await act(async () => button('Stop and summarize findings').click());
    expect(writes).toEqual([{ path: '/api/runs/run-1/stop-and-answer', body: {} }]);
    expect(button('Stop task').disabled).toBe(false);
  });

  it('keeps a failed initial objective available for retry', async () => {
    rejectSend = true;
    await mount('/deep-work?view=tasks');
    await input('Keep this objective');
    await act(async () => button('Start task').click());
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('Provider unavailable');
    expect(container.querySelector('textarea')?.value).toBe('Keep this objective');
    expect(writes.filter((write) => write.path === '/api/deep-work/conversations')).toHaveLength(1);
  });

  it('protects unsent steering when switching tasks or leaving the page', async () => {
    vi.mocked(window.confirm).mockReturnValue(false);
    await mount('/deep-work?view=tasks&conversation=task-1');
    await input('Do not lose this direction');
    await act(async () => button('New task').click());
    expect(container.querySelector('textarea')?.value).toBe('Do not lose this direction');
    await act(async () => container.querySelector<HTMLAnchorElement>('a[href="/"]')!.click());
    expect(window.location.pathname).toBe('/deep-work');
    expect(writes).toHaveLength(0);
  });

  it('refreshes completed findings and does not cancel on unmount', async () => {
    vi.useFakeTimers();
    await mount('/deep-work?view=tasks&conversation=task-1');
    run = { ...run, status: 'completed', final_output: 'The evidence supports two findings.' };
    await act(async () => vi.advanceTimersByTimeAsync(3000));
    expect(container.textContent).toContain('The evidence supports two findings.');
    expect(button('Continue task').disabled).toBe(true);
    await act(async () => root.render(<RouterProvider><div>Another page</div></RouterProvider>));
    expect(writes).toHaveLength(0);
  });
});
