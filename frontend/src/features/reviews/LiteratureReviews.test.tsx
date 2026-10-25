// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { RouterProvider } from '../../app/router';
import { LiteratureReviews } from './LiteratureReviews';

vi.mock('../chat/IdeasPanel', () => ({
  IdeasPanel: ({ objective, context }: { objective: string; context: string }) =>
    <section aria-label="Ideas panel">{objective}<pre>{context}</pre></section>,
}));
const model = { provider_profile_id: 'local', model: 'research' };
let container: HTMLDivElement;
let root: Root;
let review: Record<string, unknown>;
let writes: { path: string; body: Record<string, unknown> }[];
let configured: boolean;
let hasBrief: boolean;
let smallTasks: typeof model | undefined;
const button = (text: string) => {
  const found = [...container.querySelectorAll('button')].find((item) => item.textContent?.includes(text));
  if (!found) throw new Error(`Missing button ${text}`);
  return found;
};
async function input(selector: string, value: string) {
  await act(async () => {
    const element = container.querySelector(selector)!;
    const prototype = element.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, 'value')!.set!.call(element, value);
    element.dispatchEvent(new Event('input', { bubbles: true }));
  });
}
async function mount(selected = false) {
  window.history.replaceState({}, '', selected ? '/deep-work?review=r1' : '/deep-work');
  await act(async () => root.render(<RouterProvider><LiteratureReviews /></RouterProvider>));
}
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  window.sessionStorage.clear();
  configured = true; hasBrief = false; writes = []; smallTasks = undefined;
  review = {
    id: 'r1', revision: 7, title: 'Saved question', objective: 'Compare calibration',
    reading_mode: 'targeted', status: 'awaiting_approval', approval: 'topics', stage: 'screening',
    main_model: model, embedding_model: { model_reference: model, batch_size: 16 },
    summary_model: { model_reference: model, max_output_tokens: 4096, parallel_requests: 1 },
    selected_count: 0, notices: [], error: null,
    topics: [{ id: 't1', title: 'Calibration methods', description: 'Abstract-based grouping', candidate_ids: ['p1', 'p2'] }],
  };
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const path = new URL(String(url), 'http://localhost').pathname;
    const respond = (value: unknown) => new Response(JSON.stringify(value));
    if (init?.method === 'POST') {
      const body = JSON.parse(String(init.body));
      writes.push({ path, body });
      review = { ...review, revision: Number(review.revision) + 1 };
      return respond(review);
    }
    if (path === '/api/providers') return respond([{
      id: 'local', name: 'Local', kind: 'openai_compatible',
      models: [
        { name: 'research', capabilities: ['chat', 'embedding'], enabled: true },
        { name: 'alternate', capabilities: ['chat', 'embedding'], enabled: true },
      ],
    }]);
    if (path === '/api/settings') return respond({ default_model_references: configured
      ? { chat: model, embedding: model, small_tasks: smallTasks } : {} });
    if (path === '/api/research/arxiv') return respond({ query: 'calibration', provider: 'arxiv', results: [{
      arxiv_id: '2610.01234', title: 'Explicit arXiv input', summary: 'Input abstract', authors: ['Researcher'],
      url: 'https://arxiv.org/abs/2610.01234', pdf_url: null, published_at: '2026-10-01', updated_at: '2026-10-01', categories: [],
    }], next_start: null });
    if (path === '/api/literature-reviews') return respond([review]);
    if (path.endsWith('/candidates')) return respond({ items: [
      { id: 'p1', title: 'First paper', abstract: 'Calibration method abstract', document_id: 'd1' },
      { id: 'p2', title: 'Second paper', abstract: 'Other method abstract' },
    ], next_offset: null });
    if (path.endsWith('/papers')) return respond(hasBrief ? [{
      candidate_id: 'p1', brief: { question_focus: 'Compare calibration', sections: [
        { title: 'Pertinent findings', content: 'Calibration finding', evidence_ids: ['e1'] },
      ], findings: [{ id: 'e1', quote: 'Verbatim evidence.', page: 2, finding: 'A bounded result' }], limitations: ['Only selected passages.'] },
    }] : []);
    if (path.endsWith('/understanding')) return respond(hasBrief ? {
      markdown: '## Agreements\nShared calibration assumptions.\n## Open questions\nUnmeasured transfer.',
      candidate_ids: ['p1'], question_focus: 'Compare calibration', fingerprint: 'fingerprint',
    } : null);
    throw new Error(`Unexpected read ${path}`);
  }));
  container = document.createElement('div'); document.body.append(container); root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks();
});

it('lands on a question, resolves defaults and creates only on explicit submission', async () => {
  await mount();
  expect(writes).toEqual([]);
  expect(container.textContent).toContain('What do you want to understand?');
  expect(container.textContent).not.toContain('Resume stage');
  await input('textarea', 'How does calibration transfer?');
  await act(async () => container.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
  expect(writes[0].body).toMatchObject({
    objective: 'How does calibration transfer?', reading_mode: 'targeted',
    main_model: model, embedding_model: { model_reference: model, batch_size: 16 },
  });
});

it('pairs the small task summary model with research and allows an explicit override', async () => {
  smallTasks = { provider_profile_id: 'local', model: 'alternate' };
  await mount();
  await act(async () => container.querySelector<HTMLButtonElement>('button[aria-label="Research settings"]')!.click());
  expect(container.querySelector('[aria-label="Paper summary model"]')?.textContent).toContain('alternate');
  await act(async () => button('Done').click());
  await input('textarea', 'Compare calibration');
  await act(async () => container.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
  expect(writes[0].body).toMatchObject({
    main_model: model, summary_model: { model_reference: smallTasks },
  });
  await act(async () => button('New paper question').click());
  await act(async () => container.querySelector<HTMLButtonElement>('button[aria-label="Research settings"]')!.click());
  await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Paper summary model"]')!.click());
  await act(async () => [...container.querySelectorAll<HTMLButtonElement>('[role="option"]')]
    .find((option) => option.textContent?.includes('Use research model'))!.click());
  await act(async () => button('Done').click());
  await act(async () => container.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
  expect(writes[1].body).toMatchObject({ main_model: model, summary_model: { model_reference: model } });
});

it('opens settings from the missing-model warning and enables starting only after both models are chosen', async () => {
  configured = false;
  await mount();
  await input('textarea', 'Compare calibration');
  expect(container.textContent).toContain('Choose a research model and embedding model before starting');
  expect(button('Explore the landscape').disabled).toBe(true);
  expect(container.querySelector<HTMLDialogElement>('dialog')?.open).toBe(false);
  await act(async () => button('Open research settings').click());
  const dialog = container.querySelector<HTMLDialogElement>('dialog[aria-label="Research settings"]')!;
  expect(dialog.open).toBe(true);
  await act(async () => dialog.querySelector<HTMLButtonElement>('[aria-label="Research model"]')!.click());
  await act(async () => dialog.querySelector<HTMLButtonElement>('[role="option"]')!.click());
  expect(button('Explore the landscape').disabled).toBe(true);
  await act(async () => dialog.querySelector<HTMLButtonElement>('[aria-label="Embedding model"]')!.click());
  await act(async () => dialog.querySelector<HTMLButtonElement>('[role="option"]')!.click());
  await act(async () => button('Done').click());
  expect(button('Explore the landscape').disabled).toBe(false);
  expect(container.querySelector('[role="alert"]')).toBeNull();
  expect(writes).toEqual([]);
});

it('keeps arXiv entry out of the question form and settings', async () => {
  await mount();
  expect(container.querySelector('[aria-label="How research works"]')?.textContent).toContain('You choose which papers to read');
  expect(container.querySelector('.arxiv-search')).toBeNull();
  expect(container.textContent).not.toContain('Add papers from arXiv');
  await act(async () => container.querySelector<HTMLButtonElement>('button[aria-label="Research settings"]')!.click());
  expect(container.querySelector('.arxiv-search')).toBeNull();
  await act(async () => button('Done').click());
  await input('textarea', 'Compare calibration');
  await act(async () => container.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
  expect(writes).toHaveLength(1);
  expect(writes[0].body.input_papers).toEqual([]);
});

it('puts the question and action first and offers editable starters without launching work', async () => {
  await mount();
  expect(container.querySelector('.research-setup .research-options')).toBeNull();
  expect(container.querySelector('.research-setup .model-select')).toBeNull();
  expect(container.querySelector('[aria-label="How research works"]')?.closest('details')).toBeNull();
  await act(async () => button('Find a research gap').click());
  const question = container.querySelector('textarea')!;
  expect(question.value).toContain('[your topic]');
  expect(document.activeElement).toBe(question);
  expect(container.querySelector('[aria-label="Question starters"]')).toBeNull();
  expect(writes).toEqual([]);
});

it.each(['close button', 'Escape', 'backdrop'])('dismisses settings using %s without losing the question and returns focus', async (method) => {
  await mount();
  await input('textarea', 'Keep this question');
  const trigger = container.querySelector<HTMLButtonElement>('button[aria-label="Research settings"]')!;
  trigger.focus();
  await act(async () => trigger.click());
  const dialog = container.querySelector<HTMLDialogElement>('dialog[aria-label="Research settings"]')!;
  expect(dialog.open).toBe(true);
  expect(dialog.closest('form')).toBeNull();
  expect(document.body.style.overflow).toBe('hidden');
  await act(async () => dialog.querySelector('h2')!.dispatchEvent(new MouseEvent('click', { bubbles: true })));
  expect(dialog.open).toBe(true);
  await act(async () => {
    if (method === 'close button') dialog.querySelector<HTMLButtonElement>('[aria-label="Close research settings"]')!.click();
    else if (method === 'Escape') dialog.dispatchEvent(new Event('cancel', { cancelable: true }));
    else dialog.click();
  });
  expect(dialog.open).toBe(false);
  expect(document.activeElement).toBe(trigger);
  expect(document.body.style.overflow).toBe('');
  expect(container.querySelector<HTMLTextAreaElement>('textarea')?.value).toBe('Keep this question');
  expect(writes).toEqual([]);
});

it('keeps selected models across closing settings and applies them only when starting research', async () => {
  await mount();
  const trigger = container.querySelector<HTMLButtonElement>('button[aria-label="Research settings"]')!;
  await act(async () => trigger.click());
  const dialog = container.querySelector<HTMLDialogElement>('dialog[aria-label="Research settings"]')!;
  for (const label of ['Research model', 'Embedding model']) {
    await act(async () => dialog.querySelector<HTMLButtonElement>(`[aria-label="${label}"]`)!.click());
    await act(async () => [...dialog.querySelectorAll<HTMLButtonElement>('[role="option"]')]
      .find((item) => item.textContent?.includes('alternate'))!.click());
  }
  await act(async () => button('Done').click());
  expect(dialog.open).toBe(false);
  await act(async () => trigger.click());
  expect(dialog.querySelector('[aria-label="Research model"]')?.textContent).toContain('alternate');
  expect(dialog.querySelector('[aria-label="Embedding model"]')?.textContent).toContain('alternate');
  expect(dialog.querySelector('a')?.getAttribute('href')).toBe('/settings');
  await act(async () => button('Done').click());
  expect(writes).toEqual([]);
  await input('textarea', 'Use the chosen models');
  await act(async () => button('Explore the landscape').click());
  const selectedModel = { provider_profile_id: 'local', model: 'alternate' };
  expect(writes).toHaveLength(1);
  expect(writes[0].body).toMatchObject({
    main_model: selectedModel, summary_model: { model_reference: selectedModel },
    embedding_model: { model_reference: selectedModel },
  });
});

it('closes a model picker with Escape before dismissing the settings drawer', async () => {
  await mount();
  await act(async () => container.querySelector<HTMLButtonElement>('button[aria-label="Research settings"]')!.click());
  const dialog = container.querySelector<HTMLDialogElement>('dialog[aria-label="Research settings"]')!;
  const modelTrigger = dialog.querySelector<HTMLButtonElement>('[aria-label="Research model"]')!;
  await act(async () => modelTrigger.click());
  expect(dialog.querySelector('[role="listbox"]')).not.toBeNull();
  const escape = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
  await act(async () => dialog.querySelector('input')!.dispatchEvent(escape));
  expect(escape.defaultPrevented).toBe(true);
  expect(dialog.querySelector('[role="listbox"]')).toBeNull();
  expect(dialog.open).toBe(true);
  expect(document.activeElement).toBe(modelTrigger);
  await act(async () => dialog.dispatchEvent(new Event('cancel', { cancelable: true })));
  expect(dialog.open).toBe(false);
  expect(writes).toEqual([]);
});

it.each(['running', 'awaiting_approval', 'completed'])('shows saved model choices without editing %s research', async (status) => {
  review.status = status;
  await mount(true);
  await act(async () => container.querySelector<HTMLButtonElement>('button[aria-label="Research settings"]')!.click());
  const dialog = container.querySelector<HTMLDialogElement>('dialog[aria-label="Research settings"]')!;
  expect(dialog.querySelectorAll('.research-settings-models dd')).toHaveLength(3);
  expect([...dialog.querySelectorAll('dd')].map((item) => item.textContent)).toEqual(['research', 'research', 'research']);
  expect(dialog.querySelector('.model-select')).toBeNull();
  expect(dialog.querySelector('textarea')).toBeNull();
  expect(writes).toEqual([]);
});

it.each([
  ['running', 'scope', null, 'Preparing your search plan'],
  ['running', 'discovery', null, 'Finding relevant papers'],
  ['paused', 'reading', null, 'Research is paused'],
  ['blocked', 'reading', null, 'Research is blocked'],
  ['awaiting_approval', 'reading', 'budget', 'A work limit has been reached'],
])('explains the next step for %s work at %s', async (status, stage, approval, heading) => {
  review = { ...review, status, stage, approval };
  await mount(true);
  expect(container.querySelector('[aria-label="Next step"] h3')?.textContent).toBe(heading);
  expect(writes).toEqual([]);
});

it('guides the next decision and makes paper questions and summaries visible', async () => {
  await mount(true);
  expect(container.querySelector('[aria-label="Next step"]')?.textContent).toContain('select paper checkboxes');
  await act(async () => button('First paper').click());
  expect(button('Summarize paper')).toBeDefined();
  expect(button('Ask a question about this paper')).toBeDefined();
});

it('adds explicitly searched arXiv papers to an existing shortlist without approving reading', async () => {
  await mount(true);
  const addPapers = container.querySelector<HTMLDetailsElement>('.research-add-papers')!;
  expect(addPapers.querySelector('summary')?.textContent).toBe('Add papers from arXiv');
  expect(addPapers.closest('.research-discovery')).not.toBeNull();
  await act(async () => addPapers.querySelector('summary')!.click());
  expect(addPapers.open).toBe(true);
  await input('.arxiv-search input', 'calibration');
  await act(async () => button('Search arXiv').click());
  expect(writes).toEqual([]);
  await act(async () => button('Add to shortlist').click());
  expect(writes).toHaveLength(1);
  expect(writes[0].body).toMatchObject({
    action: 'add_arxiv_papers', expected_revision: 7,
    input_papers: [{ arxiv_id: '2610.01234', title: 'Explicit arXiv input' }],
  });
  expect(container.querySelectorAll('input[type="checkbox"]:checked')).toHaveLength(0);
  await act(async () => button('Findings').click());
  expect(container.querySelector('.arxiv-search')).toBeNull();
  expect(container.querySelector('.research-add-papers')).toBeNull();
});

it('adds a PDF URL to the shortlist without approving reading or requesting a download', async () => {
  await mount(true);
  await act(async () => button('Add PDF link').click());
  await input('[aria-label="PDF URL"]', 'https://example.org/download?id=paper');
  await input('[aria-label="PDF paper title"]', '  Calibration paper  ');
  await act(async () => button('Add PDF to shortlist').click());
  expect(writes).toEqual([{
    path: '/api/literature-reviews/r1/control',
    body: {
      action: 'add_pdf_paper', expected_revision: 7,
      pdf_paper: { url: 'https://example.org/download?id=paper', title: 'Calibration paper' },
    },
  }]);
  expect(container.querySelectorAll('input[type="checkbox"]:checked')).toHaveLength(0);
  expect(container.querySelector('[aria-label="PDF URL"]')).toBeNull();
});

it('preserves a PDF URL draft on server failure and allows retry without a title', async () => {
  await mount(true);
  await act(async () => button('Add PDF link').click());
  await input('[aria-label="PDF URL"]', 'https://example.org/paper.pdf');
  vi.mocked(fetch).mockResolvedValueOnce(new Response('PDF intake unavailable', { status: 502 }));
  await act(async () => button('Add PDF to shortlist').click());
  expect(container.textContent).toContain('PDF intake unavailable');
  expect(container.querySelector<HTMLInputElement>('[aria-label="PDF URL"]')?.value).toBe('https://example.org/paper.pdf');
  await act(async () => button('Add PDF to shortlist').click());
  expect(writes).toHaveLength(1);
  expect(writes[0].body).toMatchObject({
    action: 'add_pdf_paper', pdf_paper: { url: 'https://example.org/paper.pdf', title: null },
  });
  expect(container.querySelector('[aria-label="PDF URL"]')).toBeNull();
});

it('disables PDF intake while its request is pending', async () => {
  await mount(true);
  await act(async () => button('Add PDF link').click());
  await input('[aria-label="PDF URL"]', 'https://example.org/paper.pdf');
  let finish!: (response: Response) => void;
  vi.mocked(fetch).mockReturnValueOnce(new Promise<Response>((resolve) => { finish = resolve; }));
  await act(async () => button('Add PDF to shortlist').click());
  expect(container.querySelector<HTMLInputElement>('[aria-label="PDF URL"]')?.disabled).toBe(true);
  expect(button('Adding paper').disabled).toBe(true);
  const requests = vi.mocked(fetch).mock.calls.length;
  await act(async () => button('Adding paper').click());
  expect(fetch).toHaveBeenCalledTimes(requests);
  await act(async () => finish(new Response(JSON.stringify({ ...review, revision: 8 }))));
  expect(container.querySelector('[aria-label="PDF URL"]')).toBeNull();
});

it('requires explicit paper choices and submits only those choices and focus', async () => {
  await mount(true);
  expect(button('Read selected papers').disabled).toBe(true);
  await act(async () => (container.querySelector('[aria-label="Read First paper"]') as HTMLInputElement).click());
  await input('input[aria-label="Reading question / follow-up focus"]', 'Which assumptions transfer?');
  await act(async () => button('Read selected papers').click());
  expect(writes[0].body).toEqual({
    action: 'approve_topics', expected_revision: 7, topic_ids: ['t1'],
    candidate_ids: ['p1'], feedback: 'Which assumptions transfer?', reading_mode: 'targeted',
  });
});

it('requests reasoned metadata recommendations instead of selecting arbitrary papers', async () => {
  await mount(true);
  await act(async () => button('Explain fit').click());
  expect(writes[0].body).toMatchObject({ action: 'explore_topics', topic_ids: ['t1'] });
  expect(container.querySelectorAll('input[type="checkbox"]:checked')).toHaveLength(0);
});

it('keeps reading approval above the shortlist and secondary discovery collapsed', async () => {
  await mount(true);
  const batch = container.querySelector('[aria-label="Reading batch"]')!;
  const shortlist = container.querySelector('[aria-label="Paper shortlist"]')!;
  expect(batch.closest('[aria-label="Next step"]')).not.toBeNull();
  expect(batch.compareDocumentPosition(shortlist) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(container.querySelector<HTMLDetailsElement>('.research-landscape')?.open).toBe(false);
  expect(container.querySelector<HTMLDetailsElement>('.research-more-papers')?.open).toBe(false);
  expect(batch.querySelector('.arxiv-search')).toBeNull();
});

it('returns from findings to the pending reading decision without discarding choices', async () => {
  await mount(true);
  await act(async () => (container.querySelector('[aria-label="Read First paper"]') as HTMLInputElement).click());
  await input('[aria-label="Reading question / follow-up focus"]', 'Check transfer assumptions');
  await act(async () => button('Findings').click());
  expect(container.querySelector('[aria-label="Reading batch"]')).toBeNull();
  expect(button('Choose papers').closest('[aria-label="Next step"]')).not.toBeNull();
  await act(async () => button('Choose papers').click());
  expect(container.querySelector<HTMLInputElement>('[aria-label="Read First paper"]')?.checked).toBe(true);
  expect(container.querySelector<HTMLInputElement>('[aria-label="Reading question / follow-up focus"]')?.value).toBe('Check transfer assumptions');
  expect(writes).toEqual([]);
});

it('filters selected papers without losing choices and explicitly clears the batch', async () => {
  await mount(true);
  await act(async () => (container.querySelector('[aria-label="Read First paper"]') as HTMLInputElement).click());
  await act(async () => button('Selected only').click());
  expect(container.querySelector('[aria-label="Paper shortlist"]')?.textContent).toContain('First paper');
  expect(container.querySelector('[aria-label="Paper shortlist"]')?.textContent).not.toContain('Second paper');
  await input('[aria-label="Search shortlist"]', 'Second');
  expect(button('Read selected papers').textContent).toContain('(1)');
  await act(async () => button('Show all papers').click());
  expect(container.querySelectorAll('[aria-label="Paper shortlist"] input:checked')).toHaveLength(1);
  await act(async () => button('Clear selection').click());
  expect(button('Read selected papers').disabled).toBe(true);
  expect(writes).toEqual([]);
});

it('filters by topic without losing the reading batch or starting recommendations', async () => {
  review.topics = [
    { id: 't1', title: 'Calibration', description: 'Calibration papers', candidate_ids: ['p1'] },
    { id: 't2', title: 'Other methods', description: 'Other papers', candidate_ids: ['p2'] },
  ];
  await mount(true);
  await act(async () => (container.querySelector('[aria-label="Read First paper"]') as HTMLInputElement).click());
  await act(async () => {
    const filter = container.querySelector<HTMLSelectElement>('[aria-label="Filter shortlist by topic"]')!;
    filter.value = 't2';
    filter.dispatchEvent(new Event('change', { bubbles: true }));
  });
  const shortlist = container.querySelector('[aria-label="Paper shortlist"]')!;
  expect(shortlist.textContent).not.toContain('First paper');
  expect(shortlist.textContent).toContain('Second paper');
  expect(button('Read selected papers').textContent).toContain('(1)');
  await act(async () => button('Selected only').click());
  await act(async () => button('Show all papers').click());
  expect(container.querySelector<HTMLInputElement>('[aria-label="Read First paper"]')?.checked).toBe(true);
  expect(container.querySelector<HTMLSelectElement>('[aria-label="Filter shortlist by topic"]')?.value).toBe('');
  expect(writes).toEqual([]);
});

it('restores an in-progress paper selection and focus after remounting the review', async () => {
  await mount(true);
  await act(async () => (container.querySelector('[aria-label="Read First paper"]') as HTMLInputElement).click());
  await input('input[aria-label="Reading question / follow-up focus"]', 'Assumptions to compare');

  await act(async () => root.unmount());
  root = createRoot(container);
  await mount(true);

  expect((container.querySelector('[aria-label="Read First paper"]') as HTMLInputElement).checked).toBe(true);
  expect((container.querySelector('[aria-label="Reading question / follow-up focus"]') as HTMLInputElement).value)
    .toBe('Assumptions to compare');
  expect(writes).toEqual([]);
});

it('drops stale saved paper choices after loading the current shortlist', async () => {
  window.sessionStorage.setItem('scholarweave:literature-review-drafts:v1', JSON.stringify({
    r1: { chosen: ['p1', 'removed-paper'], focus: 'Keep this focus' },
  }));
  await mount(true);
  expect(container.querySelectorAll('[aria-label="Paper shortlist"] input:checked')).toHaveLength(1);
  expect((container.querySelector('[aria-label="Reading question / follow-up focus"]') as HTMLInputElement).value)
    .toBe('Keep this focus');
});

it('shows used, current, and editable proposed work limits before explicit extension', async () => {
  review = {
    ...review, status: 'awaiting_approval', approval: 'budget',
    work_budget: { screened_papers: 100, read_papers: 20, comparison_passes: 1 },
    work_used: { screened_papers: 96, read_papers: 20, comparison_passes: 1 },
    suggested_work_budget: { screened_papers: 100, read_papers: 21, comparison_passes: 1 },
    scope: { rationale: 'A bounded scope.', questions: [], suggested_work_budget: { screened_papers: 100, read_papers: 20, comparison_passes: 1 } },
  };
  await mount(true);
  expect(container.querySelector('[aria-label="Current work budget"]')?.textContent)
    .toContain('20 used / 20 current / 21 proposed');
  expect(button('Approve additional work limits').disabled).toBe(false);

  await input('[aria-label="Proposed read papers"]', '5001');
  expect(button('Approve additional work limits').disabled).toBe(true);
  await input('[aria-label="Proposed read papers"]', '32');
  expect(button('Approve additional work limits').disabled).toBe(false);
  await act(async () => button('Approve additional work limits').click());
  expect(writes[0].body).toEqual({
    action: 'extend_budget', expected_revision: 7,
    work_budget: { screened_papers: 100, read_papers: 32, comparison_passes: 1 },
  });
});

it('keeps pause outside collapsed diagnostics', async () => {
  review.status = 'running'; review.approval = null;
  await mount(true);
  expect(button('Pause').closest('details')).toBeNull();
  expect(button('Pause').closest('[aria-label="Review controls"]')).not.toBeNull();
  await act(async () => button('Pause').click());
  expect(writes[0].body).toMatchObject({ action: 'pause', expected_revision: 7 });
});

it('offers the same saved-work model recovery in the standalone review surface', async () => {
  review.status = 'paused'; review.approval = null;
  await mount(true);
  expect(button('Resume research')).toBeDefined();
  expect(container.querySelector('[aria-label="Resume review"]')).not.toBeNull();
  expect(container.querySelector('.research-run-controls button')?.textContent).not.toBe('Resume research');
  expect(container.querySelector('[aria-label="Replacement research model"]')?.closest('dialog')?.open).toBe(false);
  await act(async () => container.querySelector<HTMLButtonElement>('button[aria-label="Research settings"]')!.click());
  const dialog = container.querySelector<HTMLDialogElement>('dialog[aria-label="Research settings"]')!;
  expect(dialog.querySelector('[aria-label="Replacement research model"]')).not.toBeNull();
  expect(dialog.querySelector('[aria-label="Replacement synthesis model"]')).not.toBeNull();
  await act(async () => button('Done').click());
  expect(writes).toEqual([]);
  await act(async () => button('Resume research').click());
  expect(writes[0].body).toEqual({ action: 'resume', expected_revision: 7 });
});

it('reads source quotations in place and exposes cross-paper understanding inline', async () => {
  hasBrief = true; review.status = 'completed'; review.approval = null;
  await mount(true);
  expect(container.querySelector('.research-tabs [aria-pressed="true"]')?.textContent).toContain('Findings');
  await act(async () => button('Papers').click());
  await act(async () => button('First paper').click());
  expect(document.activeElement).toBe(container.querySelector('[aria-label="Paper reader"]'));
  expect(container.querySelector('[aria-label="Paper reader"]')?.textContent).toContain('Verbatim evidence.');
  expect(container.textContent).toContain('Page 2');
  await act(async () => button('Findings').click());
  expect(container.textContent).toContain('Shared calibration assumptions');
  await act(async () => button('Refine understanding').click());
  expect(writes[0].body).toMatchObject({ action: 'refine_understanding', expected_revision: 7 });
});

it('does not switch away from papers when polling detects completed research', async () => {
  vi.useFakeTimers();
  review.status = 'running'; review.approval = null; review.stage = 'reading';
  await mount(true);
  expect(container.querySelector('.research-tabs [aria-pressed="true"]')?.textContent).toContain('Papers');
  review = { ...review, revision: 8, status: 'completed' };
  await act(async () => vi.advanceTimersByTimeAsync(3000));
  expect(container.querySelector('[aria-label="Next step"]')?.textContent).toContain('Your findings are ready');
  expect(container.querySelector('.research-tabs [aria-pressed="true"]')?.textContent).toContain('Papers');
  await act(async () => button('View findings').click());
  expect(container.querySelector('.research-tabs [aria-pressed="true"]')?.textContent).toContain('Findings');
  expect(writes).toEqual([]);
});
it('provides resolved paper context to the embedded ideas panel without starting research', async () => {
  await mount(true);
  await act(async () => button('First paper').click());
  await act(async () => button('Ideas').click());
  expect(container.querySelector('[aria-label="Ideas panel"]')?.textContent).toContain('First paper');
  expect(container.querySelector('[aria-label="Ideas panel"]')?.textContent).toContain('Calibration method abstract');
  expect(container.querySelector('[aria-label="Ideas panel"]')?.textContent).not.toContain('candidate_id');
  expect(writes).toEqual([]);
});

it('opens an editable literature-check draft and ignores an old review selection', async () => {
  window.history.replaceState({}, '', '/deep-work?view=reviews&new=1&review=r1&objective=Calibration%20premise');
  await act(async () => root.render(<RouterProvider><LiteratureReviews /></RouterProvider>));
  expect((container.querySelector('textarea') as HTMLTextAreaElement).value).toBe('Calibration premise');
  expect(button('Explore the landscape')).toBeDefined();
  expect(writes).toEqual([]);
});

it('preserves old research as read-only history without destructive controls', async () => {
  review.reading_mode = 'full';
  await mount(true);
  expect(container.textContent).toContain('Archived full-reading workspace');
  expect(container.textContent).not.toContain('Read selected papers');
  expect(container.textContent).not.toContain('Clear review');
  expect(writes).toEqual([]);
});
