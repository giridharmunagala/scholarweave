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
let run: Run | null;
let rejectSend: boolean;
let rejectReview: boolean;
let rejectPause: boolean;
let rejectCancel: boolean;
let hasCandidate: boolean;
let olderRuns: Run[];
let writes: { path: string; body: Record<string, unknown> }[];
let reads: string[];
let review: Record<string, unknown> | null;
let secondReview: Record<string, unknown> | null;

function button(text: string) {
  const found = [...container.querySelectorAll('button')].find((item) => item.textContent === text);
  if (!found) throw new Error(`Missing button ${text}`);
  return found;
}
async function input(text: string, selector = 'textarea') {
  await act(async () => {
    const element = container.querySelector<HTMLInputElement | HTMLTextAreaElement>(selector)!;
    const prototype = element.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, 'value')!.set!.call(element, text);
    element.dispatchEvent(new Event('input', { bubbles: true }));
  });
}
async function selectHistory(value: string) {
  await act(async () => {
    container.querySelector<HTMLAnchorElement>(`[data-research-key="${value}"]`)!.click();
  });
}
async function mount(url: string) {
  window.history.replaceState({}, '', url);
  await act(async () => root.render(<RouterProvider><DeepWorkPage /></RouterProvider>));
}
async function reopen(url = '/deep-work') {
  await act(async () => root.unmount());
  root = createRoot(container);
  await mount(url);
}
function savedReview(values: Record<string, unknown> = {}) {
  review = {
    id: 'review-1', revision: 1, title: 'Saved paper research', objective: 'Saved comparison question',
    conversation_id: 'task-1', stage: 'scope', status: 'awaiting_approval', approval: 'plan',
    main_model: model, reading_mode: 'targeted', selected_count: 0, topics: [], notices: [], error: null,
    track_id: 'track-1', created_at: conversation.created_at, updated_at: conversation.updated_at,
    summary_model: { model_reference: model, max_output_tokens: 2048, parallel_requests: 2, reasoning_effort: null },
    scope: { rationale: 'Confirm this direction.', questions: [], suggested_work_budget: {} },
    ...values,
  };
}
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  window.sessionStorage.clear();
  window.localStorage.clear();
  run = { ...initialRun };
  rejectSend = false; rejectReview = false; rejectPause = false; rejectCancel = false; olderRuns = [];
  hasCandidate = false;
  writes = []; reads = []; review = null; secondReview = null;
  vi.stubGlobal('scrollTo', vi.fn());
  vi.stubGlobal('confirm', vi.fn(() => true));
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost');
    const respond = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
    if (init?.method === 'POST') {
      const body = JSON.parse(String(init.body));
      writes.push({ path: url.pathname, body });
      if (url.pathname.endsWith('/messages')) {
        if (rejectSend) return respond({ message: 'Provider unavailable' }, 503);
        run = { ...initialRun };
        return respond({ conversation, run });
      }
      if (url.pathname.endsWith('/steering')) return respond({ id: 'instruction-1', content: body.content, status: 'queued' });
      if (url.pathname.endsWith('/cancel')) {
        if (rejectCancel) return respond({ message: 'Agent cancellation failed' }, 503);
        run = { ...initialRun, status: 'cancelled' };
        return respond(run);
      }
      if (url.pathname.endsWith('/stop-and-answer')) {
        const stopped = { ...initialRun, status: 'cancelled' };
        run = { ...initialRun, id: 'answer-1' };
        return respond({ stopped_run: stopped, answer_run: run });
      }
      if (url.pathname === '/api/deep-work/conversations') return respond(conversation);
      if (url.pathname === '/api/literature-reviews') {
        if (rejectReview) return respond({ message: 'Scope provider unavailable' }, 503);
        savedReview({
          title: body.title, objective: body.objective, conversation_id: body.conversation_id, track_id: body.track_id,
          stage: 'screening', approval: 'topics', question_focus: 'Compare calibration assumptions', selected_count: 1,
          output_path: 'research/calibration/report.md', provisional_path: 'research/calibration/findings.md',
        });
        return respond(review);
      }
      if (url.pathname === '/api/literature-reviews/review-1/control') {
        if (rejectPause && body.action === 'pause') return respond({ message: 'Paper pause failed' }, 503);
        review = {
          ...review, revision: Number(review?.revision) + 1,
          status: body.action === 'pause' ? 'paused' : body.action === 'resume' ? 'running' : review?.status,
          approval: body.action === 'resume' ? null : review?.approval,
          ...(body.main_model ? { main_model: body.main_model } : {}),
          ...(body.summary_model ? { summary_model: body.summary_model } : {}),
        };
        return respond(review);
      }
      throw new Error(`Unexpected write ${url.pathname}`);
    }
    reads.push(url.pathname + url.search);
    if (url.pathname === '/api/tracks') return respond([
      { id: 'track-1', title: 'Adaptive methods' }, { id: 'track-2', title: 'Other methods' },
    ]);
    if (url.pathname === '/api/providers') return respond([{ id: 'local', name: 'Local', kind: 'openai_compatible',
      models: [
        { name: 'research', enabled: true, capabilities: ['chat', 'embedding'] },
        { name: 'replacement', enabled: true, capabilities: ['chat'] },
        { name: 'summary-v2', enabled: true, capabilities: ['chat'] },
      ] }]);
    if (url.pathname === '/api/settings') return respond({ default_model_references: { chat: model, embedding: model } });
    if (url.pathname === '/api/deep-work/conversations') return respond([conversation]);
    if (url.pathname === '/api/agent/conversations/task-1') return respond(conversation);
    if (url.pathname === '/api/runs') return respond([...olderRuns, ...(run ? [run] : [])]);
    if (url.pathname === '/api/literature-reviews') return respond([review, secondReview].filter(Boolean));
    if (url.pathname === '/api/literature-reviews/review-1') return respond(review);
    if (url.pathname.endsWith('/candidates')) return respond({ items: hasCandidate ? [{
      id: 'candidate-1', title: 'Calibration source', abstract: 'Calibration assumptions.', document_id: 'paper-1',
    }] : [], next_offset: null });
    if (url.pathname.endsWith('/papers')) return respond([]);
    if (url.pathname.endsWith('/understanding')) return respond(null);
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
  vi.restoreAllMocks();
});

describe('one research workspace', () => {
  it('reopens the last review and its unfinished paper choices from the Reviews landing page', async () => {
    run = null; hasCandidate = true;
    savedReview({ stage: 'screening', approval: 'topics' });
    await mount('/deep-work?review=review-1');
    await act(async () => container.querySelector<HTMLInputElement>('[aria-label="Read Calibration source"]')!.click());
    await input('Compare assumptions', '[aria-label="Reading question / follow-up focus"]');

    await reopen();

    expect(new URLSearchParams(window.location.search).get('review')).toBe('review-1');
    expect(container.querySelector('[aria-current="step"]')?.textContent).toContain('Choose papers');
    expect(container.querySelector<HTMLInputElement>('[aria-label="Read Calibration source"]')?.checked).toBe(true);
    expect(container.querySelector<HTMLInputElement>('[aria-label="Reading question / follow-up focus"]')?.value)
      .toBe('Compare assumptions');
    expect(container.textContent).not.toContain('What do you want to understand?');
    expect(writes).toEqual([]);
  });

  it('reloads current server progress and the web policy after reopening the browser', async () => {
    run = null; hasCandidate = true;
    savedReview({ stage: 'screening', approval: 'topics' });
    await mount('/deep-work?review=review-1&web=0');
    await act(async () => root.unmount());
    window.sessionStorage.clear();
    savedReview({ revision: 2, stage: 'reading', status: 'running', approval: null, selected_count: 1 });
    root = createRoot(container);
    reads = [];

    await mount('/deep-work');

    expect(new URLSearchParams(window.location.search).get('review')).toBe('review-1');
    expect(container.querySelector('[aria-current="step"]')?.textContent).toContain('Read & compare');
    expect(container.querySelector('[aria-label="Paper shortlist"]')?.textContent).toContain('Calibration source');
    expect(container.querySelector<HTMLInputElement>('.research-composer input[type="checkbox"]')?.checked).toBe(false);
    expect(reads).toContain('/api/literature-reviews');
    expect(reads).toContain('/api/agent/conversations/task-1');
    expect(button('Pause research')).toBeDefined();
    expect(writes).toEqual([]);
  });

  it.each([
    ['/deep-work?new=1', ''],
    ['/deep-work?track=track-2', ''],
    ['/deep-work?objective=Another%20question', 'Another question'],
  ])('keeps the explicit draft at %s separate from remembered research', async (url, objective) => {
    run = null;
    savedReview();
    await mount('/deep-work?review=review-1');

    await reopen(url);

    expect(container.textContent).toContain('What do you want to understand?');
    expect(container.querySelector<HTMLTextAreaElement>('.research-setup textarea')?.value).toBe(objective);
    expect(container.querySelector('[aria-current="step"]')).toBeNull();
    expect(window.location.search).not.toContain('review=');
    expect(writes).toEqual([]);
  });

  it('remembers an explicitly opened different review rather than returning to the previous one', async () => {
    run = null;
    savedReview();
    secondReview = { ...review, id: 'review-2', title: 'Other research', objective: 'A different question', conversation_id: null };
    await mount('/deep-work?review=review-1');

    await reopen('/deep-work?review=review-2');
    expect(container.textContent).toContain('A different question');
    await reopen();

    expect(new URLSearchParams(window.location.search).get('review')).toBe('review-2');
    expect(container.textContent).toContain('A different question');
    expect(container.querySelector('[aria-current="step"]')?.textContent).toContain('Confirm scope');
    expect(writes).toEqual([]);
  });

  it('restores saved conversation-only research and its findings without creating a paper review', async () => {
    run = { ...initialRun, status: 'completed', final_output: 'Findings saved before leaving.' };
    await mount('/deep-work?conversation=task-1');

    await reopen();

    expect(new URLSearchParams(window.location.search).get('conversation')).toBe('task-1');
    expect(container.textContent).toContain('Findings saved before leaving.');
    expect(container.textContent).not.toContain('What do you want to understand?');
    expect(writes).toEqual([]);
  });

  it('fetches remembered research directly when it is absent from the recent list', async () => {
    run = null;
    savedReview({ conversation_id: null, stage: 'reading', status: 'paused', approval: null });
    await mount('/deep-work?review=review-1');
    const fetch = window.fetch;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) =>
      new URL(String(input), 'http://localhost').pathname === '/api/literature-reviews'
        ? new Response('[]') : fetch(input, init)));
    reads = [];

    await reopen();

    expect(reads).toContain('/api/literature-reviews/review-1');
    expect(container.querySelector('[aria-current="step"]')?.textContent).toContain('Read & compare');
    expect(button('Resume research')).toBeDefined();
    expect(writes).toEqual([]);
  });

  it('shows restoration failures without replacing the remembered review or starting new work', async () => {
    run = null;
    savedReview();
    await mount('/deep-work?review=review-1');
    const remembered = window.localStorage.getItem('scholarweave:last-research:v1');
    const fetch = window.fetch;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) =>
      new URL(String(input), 'http://localhost').pathname === '/api/literature-reviews'
        ? new Response(JSON.stringify({ message: 'Saved research temporarily unavailable' }), { status: 503 })
        : fetch(input, init)));

    await reopen();

    expect(container.querySelector('[role="alert"]')?.textContent).toContain('Saved research temporarily unavailable');
    expect(window.localStorage.getItem('scholarweave:last-research:v1')).toBe(remembered);
    expect(container.textContent).not.toContain('What do you want to understand?');
    expect(writes).toEqual([]);
  });

  it('keeps saved research accessible when the browser cannot remember the selection', async () => {
    run = null;
    savedReview();
    vi.spyOn(window.localStorage, 'setItem').mockImplementation(() => { throw new Error('Storage unavailable'); });

    await mount('/deep-work?review=review-1');

    expect(container.querySelector('[role="alert"]')?.textContent).toContain('Could not remember this research');
    expect(button('Approve scope and find papers')).toBeDefined();
    expect(container.querySelector('a.research-permalink')?.getAttribute('href')).toContain('review=review-1');
    expect(writes).toEqual([]);
  });

  it('surfaces an invalid saved bookmark without preventing history from opening', async () => {
    run = null;
    savedReview();
    window.localStorage.setItem('scholarweave:last-research:v1', 'invalid-bookmark');

    await mount('/deep-work');

    expect(container.querySelector('[role="alert"]')?.textContent).toContain('Could not restore the last opened research');
    await selectHistory('conversation:task-1');
    expect(container.querySelector('[aria-current="step"]')?.textContent).toContain('Confirm scope');
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(writes).toEqual([]);
  });

  it('uses the saved track association when an existing review opens from an older track link', async () => {
    run = null;
    savedReview({ track_id: 'track-2' });
    await mount('/deep-work?review=review-1&track=track-1');
    const params = new URLSearchParams(window.location.search);
    expect(params.get('review')).toBe('review-1');
    expect(params.get('track')).toBe('track-2');
    expect(writes).toHaveLength(0);
  });

  it('has one starting question and one history, without a second assistant', async () => {
    await mount('/deep-work?track=track-1');
    expect(container.querySelector('h1')?.textContent).toBe('Research workspace');
    expect(reads).toContain('/api/literature-reviews');
    expect(container.querySelectorAll('textarea')).toHaveLength(1);
    expect(container.querySelectorAll('form')).toHaveLength(1);
    expect(container.querySelectorAll('[aria-label="Saved research"]')).toHaveLength(1);
    expect(container.querySelector('[aria-label="Research history"] select')).toBeNull();
    expect(container.querySelector('[aria-label="Saved research"] a')?.textContent).toContain('Investigate evidence');
    expect(container.querySelector('#workspace-research-assistant')).toBeNull();
    expect(container.textContent).not.toContain('Research assistant');
    expect(container.textContent).not.toContain('Start investigation');
    expect(container.textContent).not.toContain('Saved investigations');
    expect(container.textContent).not.toContain('Add papers from arXiv');
    expect(container.querySelector('.research-setup .model-select')).toBeNull();
    expect(container.querySelectorAll('button[aria-label="Research settings"]')).toHaveLength(1);
    expect(container.querySelector('button[aria-label="Research settings"]')?.closest('.page-header')).not.toBeNull();
    expect(button('Start research')).toBeDefined();
    expect(writes).toHaveLength(0);
  });

  it('starts one linked research and continues beside its paper decisions', async () => {
    run = null;
    await mount('/deep-work?track=track-1');
    await input('Compare calibration methods');
    await act(async () => button('Start research').click());
    expect(writes[0]).toEqual({ path: '/api/deep-work/conversations', body: { model_reference: model, track_id: 'track-1' } });
    expect(writes[1]).toMatchObject({ path: '/api/literature-reviews', body: {
      conversation_id: 'task-1', track_id: 'track-1', objective: 'Compare calibration methods',
    } });
    expect(writes.some((write) => write.path.endsWith('/messages'))).toBe(false);
    expect(container.querySelector('[aria-current="step"]')?.textContent).toContain('Choose papers');
    expect(container.querySelector('[aria-label="Next step"]')?.textContent).toContain('Choose your reading batch');
    expect(window.location.search).toContain('review=review-1');
    expect(window.location.search).toContain('conversation=task-1');
    expect(container.textContent).not.toContain('What do you want to understand?');
    expect(container.querySelectorAll('textarea')).toHaveLength(1);
    expect(container.querySelectorAll('form')).toHaveLength(1);
    expect(container.querySelectorAll('[aria-label="Saved research"] a')).toHaveLength(1);
    await input('Investigate the remaining assumptions');
    await act(async () => button('Send').click());
    const message = writes.find((write) => write.path.endsWith('/messages'))!;
    expect(message.path).toBe('/api/deep-work/conversations/task-1/messages');
    expect(message.body).toMatchObject({ response_effort: 'thorough', turn_intent: 'chat', web_enabled: true });
    expect(message.body.content).toContain('Investigate the remaining assumptions');
    expect(message.body.content).toContain('Review ID: review-1');
    expect(message.body.content).toContain('research/calibration/findings.md');
    expect(message.body.content).toContain('pending approval: topics');
    expect(writes.filter((write) => write.path === '/api/literature-reviews')).toHaveLength(1);
    expect(container.querySelectorAll('textarea')).toHaveLength(1);
  });

  it('uses the same composer for ideas and evidence comparison', async () => {
    savedReview({ status: 'completed', stage: 'reading', approval: null });
    run = null;
    await mount('/deep-work?conversation=task-1');
    await act(async () => button('Ideas').click());
    expect(container.querySelectorAll('textarea')).toHaveLength(1);
    expect(container.querySelector('.ideas-panel')).toBeNull();
    expect(container.querySelector('[aria-label="Message focus"] [aria-pressed="true"]')?.textContent).toBe('Ideas');
    await input('Suggest a simpler hypothesis');
    await act(async () => button('Send').click());
    expect(writes[0].body.turn_intent).toBe('ideation');
    await act(async () => button('Findings').click());
    await act(async () => button('Compare this evidence').click());
    expect(container.querySelector<HTMLTextAreaElement>('textarea')?.value).toContain('Compare agreements');
    expect(container.querySelectorAll('textarea')).toHaveLength(1);
    expect(writes).toHaveLength(1);
  });

  it('loads linked paper approvals and progress without creating another workspace', async () => {
    savedReview();
    await mount('/deep-work?conversation=task-1');
    expect(container.textContent).toContain('Saved comparison question');
    expect(button('Approve scope and find papers')).toBeDefined();
    expect(button('Approve scope and find papers').closest('[aria-label="Next step"]')).not.toBeNull();
    expect(container.querySelector('[aria-label="Paper reader"]')).toBeNull();
    expect(container.querySelectorAll('.research-head h2')).toHaveLength(1);
    expect(container.querySelector('[aria-label="Turn activity"]')).not.toBeNull();
    expect(container.querySelectorAll('textarea')).toHaveLength(1);
    expect(container.textContent).not.toContain('What do you want to understand?');
    expect(writes).toHaveLength(0);
  });

  it('restores legacy task links using the same composer, without an empty paper setup', async () => {
    await mount('/deep-work?view=tasks&conversation=task-1');
    expect(container.textContent).toContain('Investigate evidence');
    expect(container.querySelector('[aria-label="Turn activity"]')).not.toBeNull();
    expect(container.querySelectorAll('textarea')).toHaveLength(1);
    expect(container.textContent).not.toContain('What do you want to understand?');
    await input('Focus on limitations');
    await act(async () => button('Send direction').click());
    expect(writes).toEqual([{ path: '/api/runs/run-1/steering', body: { content: 'Focus on limitations' } }]);
    expect(container.textContent).toContain('Instruction queued: Focus on limitations');
    expect(container.querySelector<HTMLTextAreaElement>('textarea')?.value).toBe('');
  });

  it('pauses both the agent and paper work, and resumes papers explicitly', async () => {
    savedReview({ status: 'running', stage: 'reading', approval: null });
    await mount('/deep-work?review=review-1');
    expect(button('Pause research').closest('[aria-label="Research controls"]')).not.toBeNull();
    expect(button('Pause research').closest('form')).toBeNull();
    await act(async () => button('Pause research').click());
    expect(writes).toContainEqual({ path: '/api/runs/run-1/cancel', body: {} });
    expect(writes).toContainEqual({ path: '/api/literature-reviews/review-1/control', body: { action: 'pause', expected_revision: 1 } });
    expect(button('Send').disabled).toBe(true);
    await act(async () => button('Resume research').click());
    expect(writes[2].body).toEqual({ action: 'resume', expected_revision: 2 });
    expect(button('Pause research')).toBeDefined();
  });

  it('resumes the same review with explicitly selected replacement models', async () => {
    savedReview({ status: 'paused', stage: 'reading', approval: null });
    run = null;
    await mount('/deep-work?review=review-1');
    expect([...container.querySelectorAll('button')].filter((item) => item.textContent === 'Resume research')).toHaveLength(1);
    expect(container.querySelector('[aria-label="Resume review"]')).not.toBeNull();

    await input('Keep this research direction');
    await act(async () => container.querySelector<HTMLButtonElement>('button[aria-label="Research settings"]')!.click());
    const dialog = container.querySelector<HTMLDialogElement>('dialog[aria-label="Research settings"]')!;
    expect(dialog.open).toBe(true);
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Replacement research model"]')!.click());
    await act(async () => [...container.querySelectorAll<HTMLButtonElement>('[role="option"]')]
      .find((item) => item.textContent?.includes('replacement'))!.click());
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Replacement synthesis model"]')!.click());
    await act(async () => [...container.querySelectorAll<HTMLButtonElement>('[role="option"]')]
      .find((item) => item.textContent?.includes('summary-v2'))!.click());
    await act(async () => button('Done').click());
    expect(dialog.open).toBe(false);
    expect(container.querySelector<HTMLTextAreaElement>('textarea')?.value).toBe('Keep this research direction');
    expect(container.querySelectorAll('textarea')).toHaveLength(1);
    expect(writes).toEqual([]);
    await act(async () => button('Resume research').click());

    expect(writes.find((write) => write.path.endsWith('/control'))?.body).toEqual({
      action: 'resume', expected_revision: 1,
      main_model: { provider_profile_id: 'local', model: 'replacement' },
      summary_model: {
        model_reference: { provider_profile_id: 'local', model: 'summary-v2' },
        max_output_tokens: 2048, parallel_requests: 2, reasoning_effort: null,
      },
    });
    expect(writes.some((write) => write.path === '/api/literature-reviews')).toBe(false);
  });

  it('surfaces a paper pause failure without pretending all work stopped', async () => {
    savedReview({ status: 'running', stage: 'reading', approval: null });
    rejectPause = true;
    await mount('/deep-work?conversation=task-1');
    await act(async () => button('Pause research').click());
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('Paper pause failed');
    expect(button('Pause research')).toBeDefined();
    expect(button('Send').disabled).toBe(true);
  });

  it('still pauses papers when cancelling the agent fails', async () => {
    savedReview({ status: 'running', stage: 'reading', approval: null });
    rejectCancel = true;
    await mount('/deep-work?conversation=task-1');
    await act(async () => button('Pause research').click());
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('Agent cancellation failed');
    expect(writes).toContainEqual({ path: '/api/literature-reviews/review-1/control', body: { action: 'pause', expected_revision: 1 } });
    expect(button('Send direction')).toBeDefined();
    expect(button('Resume research')).toBeDefined();
  });

  it('pauses paper collection before requesting stop-and-answer', async () => {
    savedReview({ status: 'running', stage: 'reading', approval: null });
    await mount('/deep-work?conversation=task-1');
    await act(async () => button('Stop and summarize findings').click());
    expect(writes).toEqual([
      { path: '/api/literature-reviews/review-1/control', body: { action: 'pause', expected_revision: 1 } },
      { path: '/api/runs/run-1/stop-and-answer', body: {} },
    ]);
  });

  it('summarizes paper-only work in the same conversation with web access off', async () => {
    savedReview({ status: 'running', stage: 'reading', approval: null });
    run = null;
    await mount('/deep-work?review=review-1');
    await act(async () => button('Stop and summarize findings').click());
    expect(writes[0].body.action).toBe('pause');
    expect(writes[1]).toMatchObject({ path: '/api/deep-work/conversations/task-1/messages',
      body: { web_enabled: false, response_effort: 'thorough' } });
    expect(writes[1].body.content).toContain('Do not restart acquisition or reading');
    expect(writes[1].body.content).toContain('status: paused');
    expect(writes.filter((write) => write.path === '/api/deep-work/conversations')).toHaveLength(0);
  });

  it('retains failed setup for retry and reuses its already-created conversation', async () => {
    rejectReview = true; run = null;
    await mount('/deep-work?new=1');
    await input('Keep this objective');
    await act(async () => button('Start research').click());
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('Scope provider unavailable');
    expect(container.querySelector<HTMLTextAreaElement>('textarea')?.value).toBe('Keep this objective');
    rejectReview = false;
    await act(async () => button('Start research').click());
    expect(writes.filter((write) => write.path === '/api/deep-work/conversations')).toHaveLength(1);
    expect(container.querySelector('[aria-label="Research message"]')).not.toBeNull();
  });

  it('retains failed follow-ups for retry', async () => {
    run = null; rejectSend = true;
    await mount('/deep-work?conversation=task-1');
    await input('Keep this follow-up');
    await act(async () => button('Send').click());
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('Provider unavailable');
    expect(container.querySelector<HTMLTextAreaElement>('textarea')?.value).toBe('Keep this follow-up');
  });

  it('protects unsent instructions on history changes and navigation', async () => {
    vi.mocked(window.confirm).mockReturnValue(false);
    await mount('/deep-work?conversation=task-1');
    await input('Do not lose this direction');
    await act(async () => button('New research').click());
    expect(container.querySelector<HTMLTextAreaElement>('textarea')?.value).toBe('Do not lose this direction');
    await act(async () => container.querySelector<HTMLAnchorElement>('a[href="/chat"]')!.click());
    expect(window.location.pathname).toBe('/deep-work');
    expect(writes).toHaveLength(0);
  });

  it('starts a genuinely new question without retaining the previous conversation', async () => {
    savedReview(); run = null;
    await mount('/deep-work?conversation=task-1');
    await act(async () => button('New research').click());
    expect(container.querySelectorAll('textarea')).toHaveLength(1);
    expect(container.textContent).toContain('What do you want to understand?');
    await input('A genuinely new question');
    await act(async () => button('Start research').click());
    expect(writes[0].path).toBe('/api/deep-work/conversations');
    expect(writes[1].body.objective).toBe('A genuinely new question');
  });

  it('preserves local-only input when replacing the setup URL with the saved research link', async () => {
    run = null;
    await mount('/deep-work?new=1');
    await input('Compare local evidence');
    await act(async () => container.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click());
    await act(async () => button('Start research').click());
    expect(writes[1].body.web_enabled).toBe(false);
    expect(window.location.search).toContain('review=review-1');
    expect(window.location.search).toContain('web=0');
    expect(container.querySelector<HTMLInputElement>('.research-composer input[type="checkbox"]')?.checked).toBe(false);
  });

  it('restores an explicit web-off policy when switching away from and back to saved research', async () => {
    savedReview();
    secondReview = { ...review, id: 'review-2', title: 'Other saved research', conversation_id: null };
    run = null;
    await mount('/deep-work?review=review-1&conversation=task-1');
    const webToggle = () => container.querySelector<HTMLInputElement>('.research-composer input[type="checkbox"]')!;
    await act(async () => webToggle().click());
    expect(webToggle().checked).toBe(false);

    await selectHistory('review:review-2');
    expect(container.textContent).toContain('Other saved research');
    await selectHistory('conversation:task-1');
    expect(window.location.search).toContain('web=0');
    expect(webToggle().checked).toBe(false);
    await input('Continue using local evidence');
    await act(async () => button('Send').click());
    expect(writes.find((write) => write.path.endsWith('/messages'))?.body.web_enabled).toBe(false);
  });

  it('does not attach an unrelated saved conversation to a standalone review', async () => {
    savedReview({ conversation_id: null });
    run = null;
    await mount('/deep-work?review=review-1');
    expect(reads).not.toContain('/api/agent/conversations/task-1');
    await input('Investigate this review only');
    await act(async () => button('Send').click());
    expect(writes[0].path).toBe('/api/deep-work/conversations');
    expect(writes[1].body.content).toContain('Review ID: review-1');
  });

  it('surfaces a review created by an ongoing investigation without replacing its draft', async () => {
    vi.useFakeTimers();
    await mount('/deep-work?conversation=task-1');
    await input('Keep this direction');
    savedReview();
    await act(async () => vi.advanceTimersByTimeAsync(3000));
    expect(button('Approve scope and find papers')).toBeDefined();
    expect(container.querySelectorAll('textarea')).toHaveLength(1);
    expect(container.querySelector<HTMLTextAreaElement>('textarea')?.value).toBe('Keep this direction');
    expect(container.querySelectorAll('[aria-label="Saved research"] a')).toHaveLength(1);
    expect(writes).toHaveLength(0);
  });

  it('keeps previous questions and findings together rather than showing only the latest answer', async () => {
    olderRuns = [{ ...initialRun, id: 'earlier', status: 'completed', input: 'First research question', final_output: 'Earlier evidence.' }];
    run = { ...initialRun, status: 'completed', input: 'Follow-up question', final_output: 'New evidence.' };
    await mount('/deep-work?conversation=task-1');
    const transcript = container.querySelector('[aria-label="Research conversation"]')!;
    expect(transcript.querySelectorAll(':scope > article')).toHaveLength(2);
    expect(transcript.textContent).toContain('First research question');
    expect(transcript.textContent).toContain('Earlier evidence.');
    expect(transcript.textContent).toContain('Follow-up question');
    expect(transcript.textContent).toContain('New evidence.');
    const answers = transcript.querySelectorAll('details');
    expect(answers[0].open).toBe(false);
    expect(answers[1].open).toBe(true);
    expect(container.querySelectorAll('textarea')).toHaveLength(1);
  });

  it('keeps one message box in the main flow without a separate continuation panel', async () => {
    savedReview();
    run = null;
    await mount('/deep-work?review=review-1');
    const composer = container.querySelector<HTMLTextAreaElement>('textarea')!;
    expect(composer.closest('form')?.parentElement?.classList.contains('research-flow')).toBe(true);
    expect(composer.closest('aside')).toBeNull();
    expect(container.querySelector('.research-companion')).toBeNull();
    expect(container.textContent).not.toContain('Continue this research');
    expect(container.querySelectorAll('form')).toHaveLength(1);
    expect(container.querySelectorAll('textarea')).toHaveLength(1);
    await act(async () => button('Write a message').click());
    expect(document.activeElement).toBe(composer);
    await act(async () => button('Ideas').click());
    await input('Find gaps in the evidence');
    expect(composer.value).toBe('Find gaps in the evidence');
    expect(writes).toHaveLength(0);
  });

  it('filters the history list without navigating away or changing the main draft', async () => {
    savedReview();
    secondReview = { ...review, id: 'review-2', title: 'Quantum evidence', conversation_id: null, track_id: 'track-2' };
    run = null;
    await mount('/deep-work?review=review-1&conversation=task-1');
    const originalUrl = window.location.href;
    await input('Keep this research direction');
    await act(async () => button('Other methods').click());
    const history = container.querySelector('[aria-label="Saved research"]')!;
    expect(history.textContent).toContain('Quantum evidence');
    expect(history.textContent).not.toContain('Saved paper research');
    expect(container.querySelector('.research-head h2')?.textContent).toBe('Saved comparison question');
    expect(window.location.href).toBe(originalUrl);
    expect(container.querySelector<HTMLTextAreaElement>('textarea')?.value).toBe('Keep this research direction');
    expect(window.confirm).not.toHaveBeenCalled();

    await input('No matching question', '[aria-label="Search saved research"]');
    expect(history.querySelectorAll('a')).toHaveLength(0);
    expect(container.textContent).toContain('No research matches these filters.');
    await act(async () => button('Show all research').click());
    expect(history.querySelectorAll('a')).toHaveLength(2);
    expect(container.querySelector<HTMLTextAreaElement>('textarea')?.value).toBe('Keep this research direction');
    expect(writes).toHaveLength(0);
  });

  it('creates new work in the explicitly selected history track without reusing the open research', async () => {
    savedReview(); run = null;
    await mount('/deep-work?review=review-1&conversation=task-1');
    await act(async () => button('Other methods').click());
    await act(async () => button('New research').click());
    expect(window.location.search).toContain('track=track-2');
    expect(window.location.search).toContain('new=1');
    expect(window.location.search).not.toContain('review=');
    expect(container.querySelector('.research-track-context')?.textContent).toBe('New research in Other methods');
    await input('A question for the other track');
    await act(async () => button('Start research').click());
    expect(writes[0]).toEqual({
      path: '/api/deep-work/conversations', body: { model_reference: model, track_id: 'track-2' },
    });
    expect(writes[1].body).toMatchObject({ track_id: 'track-2', objective: 'A question for the other track' });
  });

  it('does not reassign an existing question draft when filtering history', async () => {
    run = null;
    await mount('/deep-work?new=1&track=track-1');
    const originalUrl = window.location.href;
    await input('Keep this question in its original track');
    await act(async () => button('Other methods').click());
    expect(window.location.href).toBe(originalUrl);
    expect(container.querySelector<HTMLTextAreaElement>('textarea')?.value).toBe('Keep this question in its original track');
    expect(container.querySelector('.research-track-context')?.textContent).toBe('New research in Adaptive methods');
    await act(async () => button('Start research').click());
    expect(writes[0].body.track_id).toBe('track-1');
    expect(writes[1].body.track_id).toBe('track-1');
  });

  it('keeps the open history and draft when a saved-work navigation is declined', async () => {
    savedReview();
    secondReview = { ...review, id: 'review-2', title: 'Other saved research', conversation_id: null };
    run = null;
    await mount('/deep-work?review=review-1&conversation=task-1');
    const originalUrl = window.location.href;
    await act(async () => button('Research history').click());
    await input('Do not discard this message');
    vi.mocked(window.confirm).mockReturnValue(false);
    await selectHistory('review:review-2');
    expect(window.location.href).toBe(originalUrl);
    expect(container.querySelector<HTMLTextAreaElement>('textarea')?.value).toBe('Do not discard this message');
    expect(container.querySelector('.research-history.is-open')).not.toBeNull();
    expect(container.querySelector('[aria-label="Saved research"] [aria-current="page"]')?.textContent).toContain('Saved paper research');
    expect(writes).toHaveLength(0);
  });

  it('shows and dismisses narrow-screen history without replacing the composer', async () => {
    savedReview(); run = null;
    await mount('/deep-work?review=review-1');
    const composer = container.querySelector('textarea')!;
    await input('An unsent question');
    const toggle = button('Research history');
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    await act(async () => toggle.click());
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    await input('Saved', '[aria-label="Search saved research"]');
    await act(async () => container.querySelector('.research-history')!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(document.activeElement).toBe(toggle);
    expect(container.querySelector('textarea')).toBe(composer);
    expect(composer.value).toBe('An unsent question');
    expect(container.querySelectorAll('textarea')).toHaveLength(1);
    expect(writes).toHaveLength(0);
  });

  it.each(['Summarize paper', 'Ask a question about this paper'])('routes %s into the same main composer', async (action) => {
    savedReview({ stage: 'reading', status: 'completed', approval: null });
    hasCandidate = true; run = null;
    await mount('/deep-work?review=review-1&conversation=task-1');
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Research views"] button')!.click());
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Paper shortlist"] button')!.click());
    await act(async () => button(action).click());
    const composer = container.querySelector<HTMLTextAreaElement>('textarea')!;
    expect(composer.value).toContain('Local paper ID: paper-1');
    expect(composer.value).toContain('Calibration source');
    expect(composer.value).toContain('Calibration assumptions.');
    expect(document.activeElement).toBe(composer);
    expect(container.querySelectorAll('textarea')).toHaveLength(1);
    expect(container.querySelectorAll('form')).toHaveLength(1);
    expect(writes).toHaveLength(0);
    await act(async () => button('Send').click());
    expect(writes[0].path).toBe('/api/deep-work/conversations/task-1/messages');
    expect(writes[0].body.content).toContain('Review ID: review-1');
    expect(writes).toHaveLength(1);
  });

  it('refreshes findings without cancelling work when leaving the page', async () => {
    vi.useFakeTimers();
    await mount('/deep-work?conversation=task-1');
    run = { ...initialRun, status: 'completed', final_output: 'The evidence supports two findings.' };
    await act(async () => vi.advanceTimersByTimeAsync(3000));
    expect(container.textContent).toContain('The evidence supports two findings.');
    expect(button('Send').disabled).toBe(true);
    await act(async () => root.render(<RouterProvider><div>Another page</div></RouterProvider>));
    expect(writes).toHaveLength(0);
  });
});
