// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { RouterProvider } from '../../app/router';
import { LiteratureReviews } from './LiteratureReviews';
import type { Candidate, Review } from './api';

const main = { provider_profile_id: 'cloud', model: 'main' };
const original: Review = {
  id: 'review-1', revision: 7, title: 'Adaptive review', objective: 'Compare adaptive methods.',
  main_model: main, main_parallel_requests: 1, decision_model: null, conversation_id: null, track_id: null,
  status: 'awaiting_approval', stage: 'reading_plan', approval: 'papers', scope: null,
  allowance_tokens: 100000, used_tokens: 1000, usage_estimated: false, suggested_allowance_tokens: null,
  work_budget: { screened_papers: 100, read_papers: 20, comparison_passes: 1 },
  work_used: { screened_papers: 50, read_papers: 1, comparison_passes: 0 },
  candidate_count: 50, selected_count: 1, evidence_count: 0, active_run_id: null,
  screened_count: 50, excluded_count: 0, references_skipped: false,
  stage_history: { scope: 'completed', discovery: 'completed', screening: 'completed' },
  notices: [], error: null, output_path: null, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
};
let container: HTMLDivElement;
let root: Root;
let rows: Review[];
let rejectControl: boolean;
let candidateFields: Partial<Candidate>;
let defaultDecision: { provider_profile_id: string; model: string } | null;
let writes: { path: string; body: Record<string, unknown> }[];
let capturedPages: boolean;
let pendingQuotes: boolean;
let proseOnlySummary: boolean;
let paperCount: number;
let detailReads: string[];
let rejectPaperDetails: boolean;
let synthesisDetails: boolean;

const button = (text: string) => {
  const found = [...container.querySelectorAll('button')].find((item) => item.textContent?.includes(text));
  if (!found) throw new Error(`Missing button: ${text}`);
  return found;
};

const openDetails = async (text: string) => {
  await act(async () => {
    const details = [...container.querySelectorAll('details')].find((item) => item.firstElementChild?.textContent === text)!;
    details.open = true;
    details.dispatchEvent(new Event('toggle'));
  });
};

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  rows = [{ ...original }];
  rejectControl = false;
  candidateFields = {};
  defaultDecision = null;
  writes = [];
  capturedPages = false;
  pendingQuotes = false;
  proseOnlySummary = false;
  paperCount = 1;
  detailReads = [];
  rejectPaperDetails = false;
  synthesisDetails = false;
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost');
    const respond = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
    if (url.pathname === '/api/providers') return respond([
      { id: 'cloud', name: 'Cloud main', kind: 'azure_openai', models: [
        { name: 'main', capabilities: ['chat'], enabled: true, reasoning_efforts: ['none', 'low'] },
        { name: 'replacement-main', capabilities: ['chat'], enabled: true },
      ] },
      { id: 'local', name: 'Local decision', kind: 'openai_compatible', models: [{ name: 'tiny-decision', capabilities: ['decision'], enabled: true }] },
    ]);
    if (url.pathname === '/api/settings') return respond({ decision_parallel_requests: 8, default_model_references: { chat: main, ...(defaultDecision ? { decision: defaultDecision } : {}) } });
    if (init?.method === 'POST') {
      writes.push({ path: url.pathname, body: JSON.parse(String(init.body)) });
      if (rejectControl) return respond({ message: 'The review changed. Reload before applying this decision.' }, 409);
      if (url.pathname.endsWith('/clear')) {
        rows = [];
        return respond({ deleted_document_ids: ['owned'], preserved_document_ids: ['shared'], preserved_paths: [] });
      }
      return respond({ ...original, revision: 8, status: 'running', approval: null });
    }
    if (url.pathname === '/api/literature-reviews') return respond(rows);
    if (url.pathname === '/api/literature-reviews/review-1') return respond(rows[0]);
    if (url.pathname === '/api/literature-reviews/review-1/analysis') return respond(synthesisDetails ? {
      phase: 'awaiting_extension', page_limit: 20, admitted_pages: 20, completed_pages: 20, pending_pages: 1,
      selection_completed: 2, selection_total: 2, report_requested: false,
      items: [{ candidate_id: 'paper-1', title: 'Adaptive source', source_version: 'v1', page: 21,
        reason: 'Verify the contradictory evaluation result.', status: 'pending', admitted: false }],
      total: 21, next_offset: null,
    } : null);
    if (url.pathname === '/api/literature-reviews/review-1/activity') return respond({
      items: synthesisDetails ? [{ key: 'synthesize:receipt', stage: 'synthesis', status: 'completed',
        model: 'main', tokens: 250, error: null }] : [],
      total: synthesisDetails ? 1 : 0, next_offset: null,
    });
    if (url.pathname === '/api/literature-reviews/review-1/activity/detail') return respond({
      key: 'synthesize:receipt', status: 'completed',
      result: { sections: [{ title: 'Comparison draft', content: 'The methods disagree on noisy evaluations.' }] },
      error: null,
    });
    if (url.pathname === '/api/literature-reviews/review-1/paper-progress') {
      const items = capturedPages ? Array.from({ length: paperCount }, (_, index) => ({
        candidate_id: index ? `summary-${index}` : 'already-approved',
        title: index ? `Summary ${index}` : 'Approved original',
        collected: true, prepared: true, plan_complete: true, summary_complete: true, operation: null, error: null,
      })) : [];
      const offset = Number(url.searchParams.get('offset'));
      return respond({
        items: items.slice(offset, offset + 25), total: items.length,
        next_offset: offset + 25 < items.length ? offset + 25 : null,
        counts: { total: items.length, collected: items.length, prepared: items.length,
          plan_complete: items.length, summarized: items.length, failed: 0 }, operations: [],
      });
    }
    if (url.pathname.startsWith('/api/literature-reviews/review-1/papers/')) {
      detailReads.push(url.pathname);
      if (rejectPaperDetails) return respond({ message: 'Paper details unavailable.' }, 500);
      return respond({
      candidate_id: 'already-approved', title: 'Approved original', source_version: 'version-1',
      collected: true, prepared: true, plan_complete: true, error: null,
      pages: [{ page: 9, action: 'defer', reference_page: true, reason: 'Bibliography page; extract citations separately.' }],
      read_pages: [1], partial_pages: [], evidence_count: pendingQuotes || proseOnlySummary ? 0 : 3,
      operation: 'Reading and extracting evidence',
      findings: proseOnlySummary ? [] : [{ id: 'evidence-1', finding: 'Measured adaptive improvement.',
        quote: pendingQuotes ? '' : 'Verbatim measured result.', page: 1 }],
      partial_summaries: [{ sections: [{ title: 'Results', content: 'The source packet measured adaptive improvement.',
        evidence_ids: proseOnlySummary ? [] : ['evidence-1'], ...(proseOnlySummary ? { source_pages: [1] } : {}) }],
        limitations: ['Only this packet was read.'] }],
      selective_summary: { sections: [{ title: 'Results', content: 'Combined selective results retain evaluation conditions.',
        evidence_ids: proseOnlySummary ? [] : ['evidence-1'], ...(proseOnlySummary ? { source_pages: [1] } : {}) }],
        limitations: ['Not full-paper coverage.'] },
      });
    }
    if (url.pathname === '/api/documents/search') return respond([{ id: 'uploaded-paper', title: 'Recovered paper', source_filename: 'paper.pdf' }]);
    if (url.pathname.endsWith('/candidates')) {
      const offset = Number(url.searchParams.get('offset'));
      const limit = Number(url.searchParams.get('limit'));
      const candidates = Array.from({ length: 50 }, (_, index) => ({
        id: index === 49 ? 'already-approved' : `paper-${index}`, title: `Paper ${index}`, selected: index === 49,
        screening: { kind: 'research', relevance: 'HIGHLY_RELEVANT', reason: 'Relevant method.', matched_questions: [] },
        ...candidateFields,
      }));
      return respond({
        items: candidates.slice(offset, offset + limit),
        total: 50, next_offset: offset + limit < 50 ? offset + limit : null, selected_ids: ['already-approved'],
      });
    }
    if (url.pathname.endsWith('/references')) return respond([{
      id: 'shared-ref', title: 'Shared adaptive reference', citing_count: 2, citing_paper_ids: ['paper-1', 'paper-2'],
      citations: [{ candidate_id: 'paper-1', source_version: 'source-v1', page: 9, quote: 'Shared citation verbatim.' }],
      selected: true, matches: [], metadata: null, screening: null, error: 'Unresolved arXiv match.',
    }]);
    throw new Error(`Unexpected request: ${url.pathname}`);
  }));
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() });
  container = document.createElement('div');
  document.body.append(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => { root.unmount(); });
  container.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('literature review controls', () => {
  it('shows a bounded synthesis plan and loads readable saved results on demand', async () => {
    synthesisDetails = true;
    rows = [{ ...original, status: 'awaiting_approval', stage: 'synthesis', approval: 'analysis', evidence_count: 3 }];
    await act(async () => root.render(<RouterProvider><LiteratureReviews /></RouterProvider>));
    expect(container.textContent).toContain('limited to 20 distinct pages');
    expect(container.textContent).toContain('Verify the contradictory evaluation result.');
    expect(container.querySelector<HTMLProgressElement>('progress[aria-label="Summary batches inspected"]')?.value).toBe(2);
    const progress = container.querySelector<HTMLProgressElement>('progress[aria-label="Synthesis source reading"]');
    expect(progress?.value).toBe(20);
    expect(progress?.max).toBe(20);
    expect(container.querySelector('section[aria-label="Saved job output"]')).toBeNull();
    await act(async () => button('synthesize').click());
    expect(container.textContent).toContain('The methods disagree on noisy evaluations.');
    expect(container.textContent).toContain('Comparison draft');
    expect(writes).toEqual([]);
  });

  it.each([
    ['Approve up to 20 more source pages', 'extend_analysis'],
    ['Write report with current evidence', 'write_report'],
  ])('sends the explicit synthesis decision: %s', async (title, action) => {
    rows = [{ ...original, status: 'awaiting_approval', stage: 'synthesis', approval: 'analysis', evidence_count: 3 }];
    await act(async () => root.render(<RouterProvider><LiteratureReviews /></RouterProvider>));
    await act(async () => button(title).click());
    expect(writes[0].body).toEqual({ action, expected_revision: 7 });
  });

  it.each(['reading', 'references', 'reference_screening', 'reference_collection'] as const)(
    'can skip reference expansion from %s', async (stage) => {
      rows = [{ ...original, status: 'running', stage, approval: null }];
      await act(async () => root.render(<RouterProvider><LiteratureReviews /></RouterProvider>));
      await act(async () => button('Skip references').click());
      expect(writes[0].body).toEqual({ action: 'skip_references', expected_revision: 7 });
    },
  );

  it('shows the durable skipped choice instead of another skip button', async () => {
    rows = [{ ...original, references_skipped: true }];
    await act(async () => root.render(<RouterProvider><LiteratureReviews /></RouterProvider>));
    expect(container.textContent).toContain('Reference expansion skipped.');
    expect([...container.querySelectorAll('button')].some((item) => item.textContent === 'Skip references')).toBe(false);
  });

  it('shows summary settings during running work with an explicit safe pause action', async () => {
    rows = [{ ...original, status: 'running', stage: 'reading', approval: null }];
    await act(async () => root.render(<RouterProvider><LiteratureReviews /></RouterProvider>));
    const section = container.querySelector('section[aria-label="Summary model settings"]');
    expect(section).not.toBeNull();
    expect(section?.closest('details')).toBeNull();
    expect(section?.querySelector('fieldset')?.disabled).toBe(true);
    expect(section?.textContent).toContain('Use a separate compact summary model');
    expect(writes).toEqual([]);
    await act(async () => button('Pause to choose summary model').click());
    expect(writes[0].body).toEqual({ action: 'pause', expected_revision: 7 });
  });

  it('requires a safe pause before switching the main research model', async () => {
    rows = [{ ...original, status: 'running', stage: 'synthesis', approval: null }];
    await act(async () => root.render(<RouterProvider><LiteratureReviews /></RouterProvider>));
    expect(container.querySelector<HTMLButtonElement>('button[aria-label="Resume main research model"]')?.disabled).toBe(true);
    await act(async () => button('Pause to switch main model').click());
    expect(writes[0].body).toEqual({ action: 'pause', expected_revision: 7 });
  });

  it.each(['paused', 'blocked'] as const)('switches the main model of a %s review without restarting its stage', async (status) => {
    vi.useFakeTimers();
    try {
      rows = [{ ...original, status, stage: 'synthesis', approval: null, evidence_count: 12 }];
      await act(async () => root.render(<RouterProvider><LiteratureReviews /></RouterProvider>));
      const picker = container.querySelector<HTMLButtonElement>('button[aria-label="Resume main research model"]')!;
      expect(picker.disabled).toBe(false);
      await act(async () => picker.click());
      const option = [...container.querySelectorAll<HTMLElement>('[role="option"]')]
        .find((item) => item.textContent?.includes('replacement-main'))!;
      await act(async () => option.click());
      const parallel = [...container.querySelectorAll<HTMLInputElement>('input[type="number"]')].find((input) =>
        input.closest('label')?.textContent?.includes('Main model parallel requests'))!;
      await act(async () => {
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(parallel, '3');
        parallel.dispatchEvent(new Event('input', { bubbles: true }));
      });
      await act(async () => { vi.advanceTimersByTime(3000); });
      expect(picker.textContent).toContain('replacement-main');
      expect(container.textContent).toContain('12 verified source excerpts');
      expect(writes).toEqual([]);
      await act(async () => button('Resume review').click());
      expect(writes[0].body).toMatchObject({
        action: 'resume', stage: 'synthesis', expected_revision: 7,
        main_model: { provider_profile_id: 'cloud', model: 'replacement-main' },
        main_parallel_requests: 3,
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it.each(['new', 'paused'] as const)('configures compact summaries for a %s review without changing exploration models', async (mode) => {
    rows = mode === 'new' ? [] : [{ ...original, status: 'paused', stage: 'reading', approval: null }];
    await act(async () => root.render(<RouterProvider><LiteratureReviews initialObjective="Compare adaptive methods." /></RouterProvider>));
    const checkbox = [...container.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')].find((input) =>
      input.closest('label')?.textContent?.includes('Use a separate compact summary model'));
    if (!checkbox) throw new Error('Missing compact summary checkbox.');
    expect(checkbox.closest('fieldset')?.disabled).toBe(false);
    await act(async () => checkbox.click());
    expect(button(mode === 'new' ? 'Propose review plan' : 'Resume review').disabled).toBe(true);
    const picker = container.querySelector<HTMLButtonElement>('button[aria-label="Summary model"]');
    if (!picker) throw new Error('Missing summary model picker.');
    await act(async () => picker.click());
    const option = [...container.querySelectorAll<HTMLElement>('[role="option"]')].find((item) => item.textContent?.includes('main'));
    if (!option) throw new Error('Missing chat-only summary model option.');
    await act(async () => option.click());
    const reasoning = [...container.querySelectorAll('select')].find((select) =>
      select.closest('label')?.textContent?.includes('Summary reasoning'));
    if (!reasoning) throw new Error('Missing summary reasoning selector.');
    await act(async () => { reasoning.value = 'none'; reasoning.dispatchEvent(new Event('change', { bubbles: true })); });
    const parallel = [...container.querySelectorAll<HTMLInputElement>('input[type="number"]')].find((input) =>
      input.closest('label')?.textContent?.includes('Parallel summaries'))!;
    for (const value of ['0', '33', '1.5']) {
      await act(async () => {
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(parallel, value);
        parallel.dispatchEvent(new Event('input', { bubbles: true }));
      });
      expect(button(mode === 'new' ? 'Propose review plan' : 'Resume review').disabled).toBe(true);
    }
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(parallel, '3');
      parallel.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await act(async () => button(mode === 'new' ? 'Propose review plan' : 'Resume review').click());
    expect(writes[0].body.summary_model).toEqual({
      model_reference: main, max_output_tokens: 26214, parallel_requests: 3, reasoning_effort: 'none',
    });
    if (mode === 'new') expect(writes[0].body).toMatchObject({ main_model: main, main_parallel_requests: 1 });
    else expect(writes[0].body).toMatchObject({ action: 'resume', stage: 'reading', expected_revision: 7 });
  });

  it('keeps the summary configuration draft across unchanged review polling', async () => {
    vi.useFakeTimers();
    try {
      rows = [{ ...original, status: 'paused', stage: 'reading', approval: null }];
      await act(async () => root.render(<RouterProvider><LiteratureReviews /></RouterProvider>));
      const checkbox = [...container.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')].find((input) =>
        input.closest('label')?.textContent?.includes('Use a separate compact summary model'))!;
      await act(async () => checkbox.click());
      rows = [{ ...rows[0], revision: 8 }];
      await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
      expect(checkbox.checked).toBe(true);
      expect(container.querySelector('button[aria-label="Summary model"]')).not.toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it.each(['plan', 'budget'] as const)('preserves edited %s work limits across polling and submits them', async (approval) => {
    vi.useFakeTimers();
    try {
      rows = [{ ...original, approval, stage: 'discovery',
        suggested_work_budget: { screened_papers: 100, read_papers: 30, comparison_passes: 1 } }];
      await act(async () => root.render(<RouterProvider><LiteratureReviews /></RouterProvider>));
      const inputs = [...container.querySelectorAll<HTMLInputElement>('.review-approval input[type="number"]')];
      expect(inputs.map((input) => input.value)).toEqual(['100', '30', '1']);
      for (const [index, value] of ['200', '60', '3'].entries()) {
        await act(async () => {
          Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(inputs[index], value);
          inputs[index].dispatchEvent(new Event('input', { bubbles: true }));
        });
      }
      expect(inputs.map((input) => input.value)).toEqual(['200', '60', '3']);
      for (let poll = 0; poll < 2; poll += 1) {
        rows = [{ ...rows[0], revision: rows[0].revision + 1,
          suggested_work_budget: { ...rows[0].suggested_work_budget! },
          work_budget: { ...original.work_budget! } }];
        await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
        expect(inputs.map((input) => input.value)).toEqual(['200', '60', '3']);
      }
      await act(async () => button(approval === 'plan' ? 'Approve plan and work limits' : 'Extend work limits').click());
      expect(writes[0].body).toMatchObject({
        action: approval === 'plan' ? 'approve_plan' : 'extend_budget', expected_revision: 9,
        work_budget: { screened_papers: 200, read_papers: 60, comparison_passes: 3 },
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it('refreshes the work-limit draft when the server proposes different limits', async () => {
    vi.useFakeTimers();
    try {
      rows = [{ ...original, approval: 'budget',
        suggested_work_budget: { screened_papers: 100, read_papers: 30, comparison_passes: 1 } }];
      await act(async () => root.render(<RouterProvider><LiteratureReviews /></RouterProvider>));
      rows = [{ ...rows[0], revision: 8,
        suggested_work_budget: { screened_papers: 200, read_papers: 60, comparison_passes: 2 } }];
      await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
      const inputs = [...container.querySelectorAll<HTMLInputElement>('.review-approval input[type="number"]')];
      expect(inputs.map((input) => input.value)).toEqual(['200', '60', '2']);
    } finally {
      vi.useRealTimers();
    }
  });

  it('approves three paper/comparison limits without a token allowance', async () => {
    rows = [{ ...original, approval: 'plan', stage: 'discovery', work_budget: null,
      scope: { questions: ['Compare methods'], inclusion_criteria: [], exclusion_criteria: [],
        queries: [{ provider: 'library', query: 'adaptive', pages: 1 }],
        suggested_work_budget: { screened_papers: 80, read_papers: 12, comparison_passes: 2 },
        suggested_allowance_tokens: 1_000_000,
        rationale: 'A bounded review.' } }];
    await act(async () => root.render(<RouterProvider><LiteratureReviews /></RouterProvider>));
    expect(container.textContent).toContain('Papers screened limit');
    expect(container.textContent).toContain('Papers collected/read limit (including references)');
    expect(container.textContent).toContain('Cross-paper comparison passes limit');
    expect(container.textContent).not.toContain('Overall token allowance');
    await act(async () => button('Approve plan and work limits').click());
    expect(writes[0].body).toEqual({
      action: 'approve_plan', expected_revision: 7,
      work_budget: { screened_papers: 80, read_papers: 12, comparison_passes: 2 },
    });
  });

  it('fetches the clicked summary only and keeps source diagnostics behind a disclosure', async () => {
    capturedPages = true;
    rows = [{ ...original, status: 'running', approval: null }];
    await act(async () => root.render(<RouterProvider><LiteratureReviews /></RouterProvider>));
    expect(container.textContent).toContain('Approved original');
    expect(container.textContent).toContain('Summary ready');
    expect(container.querySelector('progress[aria-label="PDFs collected or reused"]')?.getAttribute('value')).toBe('1');
    expect(container.querySelector('progress[aria-label="Papers read and summarized"]')?.getAttribute('value')).toBe('1');
    expect(container.textContent).not.toContain('Combined selective results retain evaluation conditions.');
    expect(detailReads).toHaveLength(0);
    await act(async () => button('View summary').click());
    expect(detailReads).toEqual(['/api/literature-reviews/review-1/papers/already-approved']);
    expect(container.textContent).not.toContain('Bibliography page; extract citations separately.');
    expect(container.textContent).not.toContain('The source packet measured adaptive improvement.');
    await openDetails('Source details and page decisions');
    expect(container.textContent).toContain('Bibliography page; extract citations separately.');
    expect(container.textContent).toContain('reference page');
    expect(container.textContent).toContain('version-1');
    expect(container.textContent).toContain('Reading and extracting evidence');
    expect(container.textContent).toContain('Measured adaptive improvement.');
    expect(container.textContent).toContain('Verbatim measured result.');
    expect(container.textContent).toContain('Selective paper summary');
    expect(container.textContent).toContain('Combined selective results retain evaluation conditions.');
    await openDetails('Paper summary packets (1)');
    expect(container.textContent).toContain('The source packet measured adaptive improvement.');
    expect(container.textContent).toContain('Not full-paper coverage.');
    expect(container.textContent).not.toContain('Creating summary');
  });

  it('counts a saved summary as complete while quotations await later analysis', async () => {
    capturedPages = true;
    pendingQuotes = true;
    rows = [{ ...original, status: 'running', approval: null }];
    await act(async () => root.render(<RouterProvider><LiteratureReviews /></RouterProvider>));
    expect(container.querySelector('progress[aria-label="Papers read and summarized"]')?.getAttribute('value')).toBe('1');
    expect(container.textContent).not.toContain('verified excerpts');
    await act(async () => button('View summary').click());
    await openDetails('Source details and page decisions');
    expect(container.textContent).toContain('0 verified excerpts');
    expect(container.textContent).toContain('Combined selective results retain evaluation conditions.');
    expect(container.textContent).toContain('Source quotation pending; verify against the original page during analysis.');
    expect(container.querySelector('blockquote')).toBeNull();
  });

  it('shows prose-only summaries as complete with application-recorded source pages', async () => {
    capturedPages = true;
    proseOnlySummary = true;
    rows = [{ ...original, status: 'running', approval: null }];
    await act(async () => root.render(<RouterProvider><LiteratureReviews /></RouterProvider>));
    expect(container.querySelector('progress[aria-label="Papers read and summarized"]')?.getAttribute('value')).toBe('1');
    expect(detailReads).toHaveLength(0);
    await act(async () => button('View summary').click());
    expect(container.textContent).toContain('Combined selective results retain evaluation conditions.');
    await openDetails('Source details and page decisions');
    expect(container.textContent).toContain('Read pages: 1');
    expect(container.textContent).toContain('0 verified excerpts');
    expect(container.textContent).not.toContain('see verified excerpts');
    expect(container.querySelector('blockquote')).toBeNull();
  });

  it('paginates the summary browser without fetching hidden paper content', async () => {
    capturedPages = true;
    paperCount = 51;
    rows = [{ ...original, status: 'running', approval: null }];
    await act(async () => root.render(<RouterProvider><LiteratureReviews /></RouterProvider>));
    expect(container.querySelectorAll('.review-paper-list > li')).toHaveLength(25);
    expect(detailReads).toHaveLength(0);
    expect(container.textContent).toContain('1-25 of 51');
    await act(async () => button('Next summaries').click());
    expect(container.querySelectorAll('.review-paper-list > li')).toHaveLength(25);
    expect(container.textContent).toContain('26-50 of 51');
    expect(detailReads).toHaveLength(0);
    await act(async () => button('View summary').click());
    expect(detailReads).toEqual(['/api/literature-reviews/review-1/papers/summary-25']);
    expect(container.textContent).toContain('Combined selective results retain evaluation conditions.');
    await act(async () => button('Close paper').click());
    expect(container.textContent).not.toContain('Combined selective results retain evaluation conditions.');
    await act(async () => button('Next summaries').click());
    expect(container.querySelectorAll('.review-paper-list > li')).toHaveLength(1);
    expect(container.textContent).toContain('51-51 of 51');
    expect(button('Next summaries').disabled).toBe(true);
    expect(vi.mocked(globalThis.fetch).mock.calls.some(([input]) => String(input).endsWith('/review-1/papers'))).toBe(false);
  });

  it('reports detail retrieval errors and retries only when the reader is refreshed', async () => {
    capturedPages = true;
    rejectPaperDetails = true;
    rows = [{ ...original, status: 'running', approval: null }];
    await act(async () => root.render(<RouterProvider><LiteratureReviews /></RouterProvider>));
    await act(async () => button('View summary').click());
    expect(container.textContent).toContain('Paper details unavailable.');
    expect(container.textContent).not.toContain('Combined selective results retain evaluation conditions.');
    expect(detailReads).toHaveLength(1);
    rejectPaperDetails = false;
    await act(async () => button('Refresh paper').click());
    expect(detailReads).toHaveLength(2);
    expect(container.textContent).toContain('Combined selective results retain evaluation conditions.');
    expect(container.textContent).not.toContain('Paper details unavailable.');
  });

  it('shows captured shared-reference provenance and resumes legacy gates automatically', async () => {
    rows = [{ ...original, stage: 'reference_screening', approval: 'references' }];
    await act(async () => root.render(<RouterProvider><LiteratureReviews /></RouterProvider>));
    expect(container.textContent).toContain('Shared adaptive reference');
    expect(container.textContent).toContain('Shared citation verbatim.');
    expect(container.textContent).toContain('source-v1');
    expect(container.textContent).toContain('Unresolved arXiv match');
    expect(container.querySelector('a[href$="references.csv"]')).not.toBeNull();
    expect(container.querySelector('input[aria-label="Select reference Shared adaptive reference"]')).toBeNull();
    await act(async () => button('Continue automatically').click());
    expect(writes[writes.length - 1]?.body).toMatchObject({ action: 'resume' });
    expect(writes.some((write) => write.body.action === 'approve_reference_pdfs')).toBe(false);
  });

  it('keeps one slow paper-progress request in flight and distinguishes collection from reading', async () => {
    vi.useFakeTimers();
    const fetch = globalThis.fetch;
    let complete!: (response: Response) => void;
    const pending = new Promise<Response>((resolve) => { complete = resolve; });
    let requests = 0;
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes('/review-1/paper-progress?')) { requests += 1; return pending; }
      return fetch(input, init);
    }));
    try {
      rows = [{ ...original, stage: 'reading_plan', status: 'running', approval: null }];
      await act(async () => root.render(<RouterProvider><LiteratureReviews /></RouterProvider>));
      for (let poll = 0; poll < 3; poll += 1) {
        rows = [{ ...rows[0], revision: rows[0].revision + 1 }];
        await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
      }
      expect(requests).toBe(1);
      await act(async () => complete(new Response(JSON.stringify({
        items: [], total: 0, next_offset: null,
        counts: { total: 2, collected: 2, prepared: 1, plan_complete: 0, summarized: 0, failed: 1 },
        operations: [{ candidate_id: 'one', title: 'Collected source', operation: 'Deciding pages' }],
      }))));
      expect(container.textContent).toContain('PDFs collected or reused: 2 / 2');
      expect(container.textContent).toContain('Source text prepared: 1 / 2');
      expect(container.textContent).toContain('Papers read and summarized: 0 / 2');
      expect(container.textContent).toContain('1 paper needs attention');
      expect(container.textContent).toContain('choose All papers to inspect errors');
      expect(container.textContent).toContain('Deciding pages: Collected source');
      await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
      expect(requests).toBe(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it('updates screening progress for each streamed completion without waiting for a poll', async () => {
    const sources: FakeEventSource[] = [];
    class FakeEventSource extends EventTarget {
      onerror: (() => void) | null = null;
      closed = false;
      constructor(readonly url: string) { super(); sources.push(this); }
      close() { this.closed = true; }
    }
    vi.stubGlobal('EventSource', FakeEventSource);
    rows = [{ ...original, status: 'running', stage: 'screening', approval: null, screened_count: 0 }];
    await act(async () => root.render(<RouterProvider><LiteratureReviews /></RouterProvider>));
    expect(sources).toHaveLength(1);
    expect(sources[0].url).toContain('/literature-reviews/review-1/events');
    for (const screened of [1, 2, 3]) {
      await act(async () => sources[0].dispatchEvent(new MessageEvent('review.progress', {
        data: JSON.stringify({ ...rows[0], revision: original.revision + screened, screened_count: screened }),
      })));
      expect(container.querySelector<HTMLProgressElement>('progress')?.value).toBe(screened);
    }
    await act(async () => sources[0].onerror?.());
    expect(container.textContent).toContain('Live updates interrupted');
    await act(async () => sources[0].dispatchEvent(new MessageEvent('review.progress', {
      data: JSON.stringify({ ...rows[0], revision: 11, status: 'awaiting_approval', approval: 'papers', screened_count: 50 }),
    })));
    expect(sources[0].closed).toBe(true);
  });

  it('inherits the default native decision model in review setup', async () => {
    rows = [];
    defaultDecision = { provider_profile_id: 'local', model: 'tiny-decision' };
    await act(async () => root.render(<RouterProvider><LiteratureReviews initialObjective="Adaptive research" /></RouterProvider>));
    expect(container.querySelector('button[aria-label="Decision model"]')?.textContent).toContain('tiny-decision');
    await act(async () => button('Propose review plan').click());
    expect(writes[0].body.decision_model).toEqual({
      model_reference: defaultDecision, protocol: 'systemone', input_tokens: 8192,
    });
  });

  it('requires confirmation to clear and keeps the objective ready for a fresh review', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    await act(async () => root.render(<RouterProvider><LiteratureReviews /></RouterProvider>));
    await act(async () => button('Clear review').click());
    expect(writes).toHaveLength(0);
    confirm.mockReturnValue(true);
    await act(async () => button('Clear review').click());
    expect(writes[0]).toEqual({ path: '/api/literature-reviews/review-1/clear', body: { expected_revision: 7 } });
    expect(container.textContent).toContain('Review cleared. Removed 1 task-owned papers');
    expect(container.querySelector<HTMLTextAreaElement>('textarea')?.value).toBe(original.objective);
    expect(container.querySelector('.review-detail')).toBeNull();
  });

  it('preserves the review when clearing fails', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    rejectControl = true;
    await act(async () => root.render(<RouterProvider><LiteratureReviews /></RouterProvider>));
    await act(async () => button('Clear review').click());
    expect(container.textContent).toContain('The review changed');
    expect(container.querySelector('.review-detail')).not.toBeNull();
    expect(container.textContent).not.toContain('Review cleared.');
  });

  it('shows measurable screening progress and automatic exclusions', async () => {
    rows = [{ ...original, status: 'running', stage: 'screening', approval: null,
      candidate_count: 550, screened_count: 300, excluded_count: 220 }];
    await act(async () => root.render(<RouterProvider><LiteratureReviews /></RouterProvider>));
    const progress = container.querySelector<HTMLProgressElement>('progress[aria-label="Paper screening progress"]');
    expect(progress?.value).toBe(300);
    expect(progress?.max).toBe(550);
    expect(container.textContent).toContain('300 of 550 papers rated');
    expect(container.textContent).toContain('220 irrelevant papers automatically excluded');
    expect(container.textContent).toContain('highly relevant');
  });

  it('shows indeterminate discovery activity and no running indicator while paused', async () => {
    rows = [{ ...original, status: 'running', stage: 'discovery', approval: null }];
    await act(async () => root.render(<RouterProvider><LiteratureReviews /></RouterProvider>));
    expect(container.querySelector('progress')?.hasAttribute('value')).toBe(false);
    expect(container.textContent).toContain('discovery in progress');
    await act(async () => root.unmount());
    root = createRoot(container);
    rows = [{ ...original, status: 'paused' }];
    await act(async () => root.render(<RouterProvider><LiteratureReviews /></RouterProvider>));
    expect(container.querySelector('progress')).toBeNull();
  });

  it('opens the saved literature section and links directly to the local paper', async () => {
    rows = [{ ...original, status: 'completed', approval: null, stage: 'publication', track_id: 'track-1',
      output_path: 'projects/tracks/adaptive--track-1/literature.md' }];
    candidateFields = { document_id: 'local-paper-1' };
    await act(async () => root.render(<RouterProvider><LiteratureReviews trackId="track-1" /></RouterProvider>));
    const saved = [...container.querySelectorAll('a')].find((item) => item.textContent === 'Open saved review');
    expect(saved?.getAttribute('href')).toBe('/tracks?id=track-1&section=literature&review=review-1&revision=7');
    expect(container.querySelector('a[href="/library?paper=local-paper-1"]')?.textContent).toBe('Open local paper');
  });

  it('keeps shortlist pagination read-only with automatic selection', async () => {
    await act(async () => root.render(<RouterProvider><LiteratureReviews initialModel={main} /></RouterProvider>));
    const first = container.querySelector<HTMLButtonElement>('button[aria-label="Explore Paper 0"]');
    if (!first) throw new Error('First candidate was not shown.');
    await act(async () => first.click());
    await act(async () => button('Next papers').click());
    const second = container.querySelector<HTMLButtonElement>('button[aria-label="Explore Paper 26"]');
    if (!second) throw new Error('Second page was not shown.');
    await act(async () => second.click());
    expect(container.textContent).not.toContain('Approve 3 papers');
    expect(writes).toHaveLength(0);
  });

  it('surfaces stale automatic continuation without pretending work started', async () => {
    rejectControl = true;
    await act(async () => root.render(<RouterProvider><LiteratureReviews /></RouterProvider>));
    await act(async () => button('Resume review').click());
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('The review changed');
    expect(button('Resume review').disabled).toBe(false);
  });

  it('selects the main API model and local native decision model independently', async () => {
    rows = [];
    await act(async () => root.render(<RouterProvider><LiteratureReviews initialModel={main} initialObjective="Compare adaptive evidence." /></RouterProvider>));
    const protocol = [...container.querySelectorAll('select')].find((select) => [...select.options].some((option) => option.value === 'systemone'));
    if (!protocol) throw new Error('Missing decision protocol selector.');
    await act(async () => { protocol.value = 'systemone'; protocol.dispatchEvent(new Event('change', { bubbles: true })); });
    const picker = container.querySelector<HTMLButtonElement>('button[aria-label="Decision model"]');
    if (!picker) throw new Error('Missing decision model picker.');
    await act(async () => picker.click());
    const option = [...container.querySelectorAll<HTMLElement>('[role="option"]')].find((item) => item.textContent?.includes('tiny-decision'));
    if (!option) throw new Error('Native model is not selectable.');
    await act(async () => option.click());
    expect(container.textContent).toContain('probabilities are not calibrated');
    await act(async () => button('Propose review plan').click());
    expect(writes[0].body.main_model).toEqual(main);
    expect(writes[0].body.openalex_enabled).toBe(false);
    expect(container.textContent).not.toContain('Include OpenAlex references');
    expect(writes[0].body.decision_model).toEqual({
      model_reference: { provider_profile_id: 'local', model: 'tiny-decision' },
      protocol: 'systemone', input_tokens: 8192,
    });
  });

  it('links an explicit library result without approving it or following unsafe source links', async () => {
    rows = [{ ...original, status: 'paused', approval: null }];
    candidateFields = { url: 'javascript:alert(1)', error: 'No accessible PDF is known.' };
    await act(async () => root.render(<RouterProvider><LiteratureReviews /></RouterProvider>));
    await act(async () => container.querySelector<HTMLButtonElement>('button[aria-label="Details for Paper 0"]')?.click());
    expect(container.querySelector('a[href^="javascript:"]')).toBeNull();
    await act(async () => button('Find local copy').click());
    const select = [...container.querySelectorAll('select')].find((item) => [...item.options].some((option) => option.value === 'uploaded-paper'));
    if (!select) throw new Error('Missing local paper results.');
    await act(async () => { select.value = 'uploaded-paper'; select.dispatchEvent(new Event('change', { bubbles: true })); });
    await act(async () => button('Link local paper').click());
    expect(writes[0].body).toEqual({
      action: 'link_paper', candidate_ids: ['paper-0'], document_id: 'uploaded-paper', expected_revision: 7,
    });
  });

    it('returns to automatic screening from a pending paper approval', async () => {
      rows = [{ ...original, screened_count: 0, stage_history: { screening: 'stopped' } }];
      await act(async () => root.render(<RouterProvider><LiteratureReviews /></RouterProvider>));
      const select = container.querySelector<HTMLSelectElement>('.review-controls select')!;
      await act(async () => { select.value = 'screening'; select.dispatchEvent(new Event('change', { bubbles: true })); });
      await act(async () => button('Resume review').click());
      expect(writes[0].body).toEqual({ action: 'resume', stage: 'screening', expected_revision: 7 });
    });

    it('opens details without changing automatic paper selection', async () => {
      await act(async () => root.render(<RouterProvider><LiteratureReviews /></RouterProvider>));
      const pending = () => [...container.querySelectorAll<HTMLButtonElement>('.review-candidates .review-candidate-select')];
      await act(async () => container.querySelector<HTMLButtonElement>('button[aria-label="Details for Paper 0"]')?.click());
      expect(pending()[0].getAttribute('aria-pressed')).toBe('false');
      expect(container.querySelector('button[aria-label="Details for Paper 0"]')?.getAttribute('aria-expanded')).toBe('true');
      await act(async () => pending()[0].click());
      expect(pending()[0].getAttribute('aria-label')).toBe('Explore Paper 0');
      expect(pending()).toHaveLength(25);
      expect(container.querySelector('.review-selected')).toBeNull();
      expect(container.textContent).not.toContain('Approve 1 papers');
    });
});
