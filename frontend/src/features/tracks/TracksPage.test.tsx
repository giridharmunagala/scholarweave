// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { Link, RouterProvider } from '../../app/router';
import TracksPage from './TracksPage';
import { SECTIONS, trackAssistUrl, type Track } from './api';
import { TrackChatSelector } from './TrackChatSelector';

const stamp = '2026-10-01T10:00:00Z';
const hash = 'a'.repeat(64);
const track: Track = {
  id: 'track-1', title: 'Evidence project', folder_path: 'research/tracks/evidence--track-1',
  created_at: stamp, updated_at: stamp, paper_ids: [], conversation_ids: [],
  exploration: [],
  sections: SECTIONS.map((section) => ({ section, path: `${section}.md`, sha256: hash, modified_at: stamp })),
  experiments: [{ id: 'EXP-001', experiment: 'Baseline', status: 'Planned', success_criterion: 'Reproduce baseline', evidence: '' }],
  status_counts: { Planned: 1 }, warnings: ['Missing evidence for baseline'],
};
let container: HTMLDivElement;
let root: Root;
let confirm: ReturnType<typeof vi.fn>;
let writes: { url: string; body: Record<string, unknown> }[];
let stale: boolean;
let saved: Record<string, string>;
let oldUrl: string;
let reviews: { id: string; title: string; track_id: string | null; revision: number; status: string; output_path: string | null }[];
let reviewConflict: boolean;

beforeEach(() => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  oldUrl = window.location.href;
  window.history.replaceState({}, '', '/tracks?id=track-1');
  vi.stubGlobal('scrollTo', vi.fn());
  confirm = vi.fn(() => false);
  vi.stubGlobal('confirm', confirm);
  writes = [];
  stale = false;
  reviewConflict = false;
  reviews = [
    { id: 'review-1', title: 'Existing review', track_id: null, revision: 4, status: 'completed', output_path: 'knowledge/findings.md' },
    { id: 'review-2', title: 'Another track review', track_id: 'track-2', revision: 7, status: 'paused', output_path: null },
  ];
  track.exploration = [];
  saved = Object.fromEntries(SECTIONS.map((section) => [section, `Original ${section}`]));
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost');
    const body = init?.body && !(init.body instanceof FormData) ? JSON.parse(String(init.body)) : {};
    if (init?.method && init.method !== 'GET') writes.push({ url: url.pathname, body });
    if (url.pathname === '/api/documents') return Response.json([{ id: 'paper-1', title: 'Existing paper' }]);
    if (url.pathname === '/api/skills') return Response.json([{ name: 'literature-map' }]);
    if (url.pathname === '/api/literature-reviews') return Response.json(reviews);
    if (url.pathname.match(/\/literature-reviews\/[^/]+\/track$/)) {
      if (reviewConflict) return Response.json({ message: 'The review changed. Refresh saved reviews before moving it.' }, { status: 409 });
      const id = url.pathname.split('/')[3];
      reviews = reviews.map((item) => item.id === id ? { ...item, track_id: body.track_id, revision: item.revision + 1 } : item);
      return Response.json(reviews.find((item) => item.id === id));
    }
    if (url.pathname.endsWith('/reader-notes')) return Response.json({ items: [], total: 0, next_offset: null });
    if (url.pathname === '/api/references') return Response.json({ revision: 0, references: [] });
    if (url.pathname.endsWith('/writing/claims')) return Response.json({ sha256: null, claims: [] });
    if (url.pathname.endsWith('/writing/manuscript')) {
      if (init?.method === 'PUT') {
        saved['exploration/manuscript'] = body.content;
        if (!track.exploration?.some((item) => item.section === 'exploration/manuscript')) {
          track.exploration?.push({ section: 'exploration/manuscript', path: 'exploration/manuscript.md', sha256: hash, modified_at: stamp });
        }
      }
      return Response.json({ content: saved['exploration/manuscript'] ?? '# Manuscript', sha256: hash, path: 'exploration/manuscript.md' });
    }
    if (url.pathname.endsWith('/repositories')) return Response.json([]);
    if (url.pathname === '/api/tracks' && init?.method === 'POST') return Response.json({ ...track, id: 'new-track', title: body.title }, { status: 201 });
    if (url.pathname === '/api/tracks') return Response.json([track, { ...track, id: 'track-2', title: 'Another track', exploration: [] }]);
    if (url.pathname.includes('/papers/')) return Response.json({ ...track, paper_ids: init?.method === 'PUT' ? ['paper-1'] : [] });
    if (url.pathname.includes('/conversations/')) return Response.json(track);
    if (url.pathname.includes('/imports/preview')) return Response.json({
      preview_id: 'preview-1', paper_ids: [], warnings: ['Check duplicate heading'],
      sections: [{ section: 'problem', current_content: saved.problem, incoming_content: 'Incoming problem', expected_sha256: hash, diff: '-Original problem\n+Incoming problem' }],
    });
    if (url.pathname.endsWith('/imports')) {
      saved.problem = body.mode === 'merge' ? body.merged_content.problem : 'Incoming problem';
      return Response.json(track);
    }
    const exploration = url.pathname.match(/\/exploration\/([a-z0-9_-]+)(.*)/);
    if (exploration) {
      const section = `exploration/${exploration[1]}`;
      if (exploration[2] === '/history') return Response.json([]);
      if (init?.method === 'PUT') {
        saved[section] = body.content;
        if (!track.exploration?.some((item) => item.section === section)) {
          track.exploration?.push({ section, path: `${section}.md`, sha256: hash, modified_at: stamp });
        }
      }
      return Response.json({ section, content: saved[section], path: `${section}.md`, sha256: hash, modified_at: stamp });
    }
    const match = url.pathname.match(/\/sections\/(problem|literature|experiments|results)(.*)/);
    if (match) {
      const section = match[1];
      if (match[2] === '/history') return Response.json([{ revision: 'old-1', sha256: hash, created_at: stamp }]);
      if (match[2].startsWith('/history/')) return Response.json({ section, content: 'Prior evidence', path: `${section}.md`, sha256: hash, modified_at: stamp });
      if (init?.method === 'PUT') {
        if (stale) return Response.json({ message: 'Section changed on disk' }, { status: 409 });
        saved[section] = body.content;
      }
      return Response.json({ section, content: saved[section], path: `${section}.md`, sha256: hash, modified_at: stamp });
    }
    return Response.json(url.pathname.endsWith('new-track') ? { ...track, id: 'new-track', exploration: [] }
      : url.pathname.endsWith('track-2') ? { ...track, id: 'track-2', title: 'Another track', exploration: [] } : track);
  }));
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  window.history.replaceState({}, '', oldUrl);
  vi.unstubAllGlobals();
});
const button = (text: string) => Array.from(container.querySelectorAll('button')).find((item) => item.textContent === text)!;
const click = async (text: string) => { await act(async () => button(text).click()); };

it('connects writing and repository views without discarding manuscript drafts', async () => {
  await act(async () => root.render(<RouterProvider><TracksPage /></RouterProvider>));
  await click('Claims & writing');
  await change('[aria-label="Manuscript Markdown"]', '# Keep this draft');
  await click('Code & artifacts');
  expect(container.textContent).toContain('Connect an existing repository');
  await click('Notebook');
  await click('Claims & writing');
  expect(container.querySelector<HTMLTextAreaElement>('[aria-label="Manuscript Markdown"]')!.value).toBe('# Keep this draft');
  expect(writes).toHaveLength(0);
});

async function change(selector: string, value: string) {
  const element = container.querySelector<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>(selector)!;
  const prototype = element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
    : element instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
  await act(async () => {
    Object.getOwnPropertyDescriptor(prototype, 'value')!.set!.call(element, value);
    element.dispatchEvent(new Event(element instanceof HTMLSelectElement ? 'change' : 'input', { bubbles: true }));
  });
}
const mount = async () => { await act(async () => root.render(<RouterProvider><Link to="/settings">Leave tracks</Link><TracksPage /></RouterProvider>)); };
async function upload(name: string) {
  await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Track settings"]')!.click());
  const file = new File(['Incoming problem'], name, { type: name.endsWith('.zip') ? 'application/zip' : 'text/markdown' });
  Object.defineProperty(file, 'text', { value: async () => 'Incoming problem' });
  const input = container.querySelector<HTMLInputElement>('[aria-label="Import track file"]')!;
  Object.defineProperty(input, 'files', { configurable: true, value: [file] });
  await act(async () => input.dispatchEvent(new Event('change', { bubbles: true })));
}

it('keeps secondary tools in settings without losing section drafts on close', async () => {
  await mount();
  const dialog = container.querySelector<HTMLDialogElement>('#track-settings')!;
  expect(dialog.open).toBe(false);
  expect(button('Export track ZIP').closest('dialog')).toBe(dialog);
  expect(container.querySelector('.track-context')?.textContent).not.toContain('Recent section updates');
  await click('Edit');
  await change('[aria-label="Problem Markdown"]', 'Keep my section draft');
  const trigger = container.querySelector<HTMLButtonElement>('[aria-label="Track settings"]')!;
  trigger.focus();
  await act(async () => trigger.click());
  expect(dialog.open).toBe(true);
  await change('[aria-label="Track assistance skill"]', 'literature-map');
  await act(async () => dialog.dispatchEvent(new Event('cancel', { cancelable: true })));
  expect(dialog.open).toBe(false);
  expect(document.activeElement).toBe(trigger);
  expect(container.querySelector<HTMLTextAreaElement>('[aria-label="Problem Markdown"]')!.value).toBe('Keep my section draft');
  await act(async () => trigger.click());
  expect(container.querySelector<HTMLSelectElement>('[aria-label="Track assistance skill"]')!.value).toBe('literature-map');
  expect(writes).toHaveLength(0);
});

it('opens the requested saved section rather than defaulting to the problem', async () => {
  window.history.replaceState({}, '', '/tracks?id=track-1&section=literature&review=review-1');
  saved.literature = 'Published findings with canonical source links.';
  await mount();
  expect(container.textContent).toContain(saved.literature);
  expect(button('Literature').getAttribute('aria-selected')).toBe('true');
  expect(container.querySelector('[aria-label="Literature review workspace"]')).toBeNull();
});

it('refreshes a clean section after publication but never overwrites an unsaved draft', async () => {
  await act(async () => root.render(<RouterProvider>
    <Link to="/tracks?id=track-1&section=literature&review=review-1">Open first review</Link>
    <Link to="/tracks?id=track-1&section=literature&review=review-2">Open second review</Link>
    <TracksPage />
  </RouterProvider>));
  await click('Literature');
  saved.literature = 'Published first review';
  await act(async () => container.querySelector<HTMLAnchorElement>('a[href$="review=review-1"]')!.click());
  expect(container.textContent).toContain('Published first review');
  await click('Edit');
  await change('[aria-label="Literature Markdown"]', 'Keep my unsaved review annotation');
  saved.literature = 'Published second review';
  confirm.mockReturnValue(true);
  await act(async () => container.querySelector<HTMLAnchorElement>('a[href$="review=review-2"]')!.click());
  expect(container.querySelector<HTMLTextAreaElement>('[aria-label="Literature Markdown"]')!.value).toBe('Keep my unsaved review annotation');
  expect(writes).toHaveLength(0);
});

it('creates multiple exploration files and preserves their drafts while editing main sections', async () => {
  await mount();
  expect(container.textContent).toContain('Exploration · 0 files');
  await change('[aria-label="New exploration filename"]', 'search-leads.md');
  await act(async () => button('Create exploration file').closest('form')!.dispatchEvent(
    new Event('submit', { bubbles: true, cancelable: true }),
  ));
  expect(writes.find((item) => item.url.endsWith('/exploration/search-leads'))!.body.expected_sha256).toBeNull();
  await change('[aria-label="Exploration/search-leads Markdown"]', 'Tentative lead; not a finding.');
  await click('Problem');
  await click('search-leads.md');
  expect(container.querySelector<HTMLTextAreaElement>('textarea')!.value).toBe('Tentative lead; not a finding.');
  await click('Save section');
  expect(saved['exploration/search-leads']).toBe('Tentative lead; not a finding.');
  const saves = writes.filter((item) => item.url.endsWith('/exploration/search-leads'));
  expect(saves[saves.length - 1].body.expected_sha256).toBe(hash);
  await change('[aria-label="New exploration filename"]', 'open-questions');
  await act(async () => button('Create exploration file').closest('form')!.dispatchEvent(
    new Event('submit', { bubbles: true, cancelable: true }),
  ));
  expect(container.textContent).toContain('Exploration · 2 files');
  expect(container.textContent).toContain('open-questions.md');
});

it('restores only valid per-track sections when switching tracks or creating a new track, retaining drafts', async () => {
  track.exploration = [{ section: 'exploration/leads', path: 'exploration/leads.md', sha256: hash, modified_at: stamp }];
  saved['exploration/leads'] = 'Saved lead';
  await mount();
  await click('leads.md');
  await click('Edit');
  await change('[aria-label="Exploration/leads Markdown"]', 'Unsaved track one lead');
  confirm.mockReturnValue(true);
  await act(async () => container.querySelector<HTMLAnchorElement>('a[href="/tracks?id=track-2"]')!.click());
  expect(button('Problem').getAttribute('aria-selected')).toBe('true');
  expect(vi.mocked(fetch).mock.calls.some(([input]) => String(input).includes('track-2/exploration/leads'))).toBe(false);
  await click('Results');
  await act(async () => container.querySelector<HTMLAnchorElement>('a[href="/tracks?id=track-1"]')!.click());
  expect(container.querySelector<HTMLTextAreaElement>('[aria-label="Exploration/leads Markdown"]')!.value).toBe('Unsaved track one lead');
  await act(async () => container.querySelector<HTMLAnchorElement>('a[href="/tracks?id=track-2"]')!.click());
  expect(button('Results').getAttribute('aria-selected')).toBe('true');
  await act(async () => container.querySelector<HTMLAnchorElement>('a[href="/tracks?id=track-1"]')!.click());
  await change('#track-title', 'New notebook');
  await act(async () => container.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
  expect(window.location.search).toBe('?id=new-track');
  expect(button('Problem').getAttribute('aria-selected')).toBe('true');
  expect(vi.mocked(fetch).mock.calls.some(([input]) => String(input).includes('new-track/exploration/leads'))).toBe(false);
});

it('falls back to a valid section for a nonexistent exploration deep link', async () => {
  window.history.replaceState({}, '', '/tracks?id=track-1&section=exploration/missing');
  await mount();
  expect(button('Problem').getAttribute('aria-selected')).toBe('true');
  expect(vi.mocked(fetch).mock.calls.some(([input]) => String(input).includes('/exploration/missing'))).toBe(false);
});

it('refreshes manuscript discovery and offers history handoff without replacing Notebook or writing drafts', async () => {
  await mount();
  await click('Edit');
  await change('[aria-label="Problem Markdown"]', 'Notebook problem draft');
  await click('Claims & writing');
  await change('[aria-label="Manuscript Markdown"]', '# Saved new manuscript');
  await click('Save manuscript');
  await change('[aria-label="Manuscript Markdown"]', '# Ongoing writing draft');
  await click('Open manuscript and history in Notebook');
  expect(button('manuscript.md')).toBeTruthy();
  expect(container.querySelector<HTMLTextAreaElement>('[aria-label="Exploration/manuscript Markdown"]')!.value).toBe('# Saved new manuscript');
  expect(button('Version history')).toBeTruthy();
  await change('[aria-label="Exploration/manuscript Markdown"]', '# Separate Notebook manuscript draft');
  await click('Problem •');
  expect(container.querySelector<HTMLTextAreaElement>('[aria-label="Problem Markdown"]')!.value).toBe('Notebook problem draft');
  await click('Claims & writing');
  expect(container.querySelector<HTMLTextAreaElement>('[aria-label="Manuscript Markdown"]')!.value).toBe('# Ongoing writing draft');
  await click('Save manuscript');
  await click('Refresh Notebook inventory');
  await change('[aria-label="Manuscript Markdown"]', '# New unsaved writing draft');
  await click('Open manuscript and history in Notebook');
  expect(container.querySelector<HTMLTextAreaElement>('[aria-label="Exploration/manuscript Markdown"]')!.value).toBe('# Separate Notebook manuscript draft');
  await click('Claims & writing');
  expect(container.querySelector<HTMLTextAreaElement>('[aria-label="Manuscript Markdown"]')!.value).toBe('# New unsaved writing draft');
});

it('protects section drafts, guards revision saves, reconciles conflicts and creates tracks', async () => {
  await mount();
  expect(container.textContent).toContain('Original problem');
  expect(container.textContent).toContain('Missing evidence for baseline');
  await click('Edit');
  await change('[aria-label="Problem Markdown"]', 'My protected draft');
  await click('Literature');
  await change('[aria-label="Literature Markdown"]', 'Literature draft');
  await click('Problem •');
  expect(container.querySelector<HTMLTextAreaElement>('textarea')!.value).toBe('My protected draft');
  await act(async () => container.querySelector<HTMLAnchorElement>('a[href="/settings"]')!.click());
  expect(window.location.pathname).toBe('/tracks');
  expect(confirm).toHaveBeenCalled();
  stale = true;
  await click('Save section');
  expect(writes.find((item) => item.url.endsWith('/sections/problem'))!.body.expected_sha256).toBe(hash);
  expect(container.textContent).toContain('Conflict:');
  expect(container.querySelector<HTMLTextAreaElement>('textarea')!.value).toBe('My protected draft');
  saved.problem = 'Externally updated evidence';
  await click('Compare latest saved version');
  expect(container.textContent).toContain('Externally updated evidence');
  confirm.mockReturnValue(true);
  await click('Use latest revision as save base');
  stale = false;
  await click('Save section');
  expect(saved.problem).toBe('My protected draft');
  expect(container.textContent).toContain('Saved locally');
  await change('#track-title', 'A new project');
  await act(async () => container.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
  expect(writes.find((item) => item.url === '/api/tracks')!.body).toEqual({ title: 'A new project' });
  expect(window.location.search).toBe('?id=new-track');
});

it('previews Markdown and ZIP imports, reviews merges, and requires explicit replace confirmation', async () => {
  await mount();
  await upload('problem.md');
  expect(container.textContent).toContain('Import preview — no changes saved yet');
  expect(container.textContent).toContain('Check duplicate heading');
  expect(writes.some((item) => item.url.endsWith('/imports'))).toBe(false);
  await change('[aria-label="Merged Problem Markdown"]', 'Original problem\nMerged evidence');
  await click('Apply import');
  const merge = writes.find((item) => item.url.endsWith('/imports'))!.body;
  expect(merge).toMatchObject({ preview_id: 'preview-1', mode: 'merge', confirmed_replace: false, expected_revisions: { problem: hash }, merged_content: { problem: 'Original problem\nMerged evidence' } });
  expect(saved.problem).toBe('Original problem\nMerged evidence');
  await upload('track.zip');
  const zip = vi.mocked(fetch).mock.calls.find(([input]) => String(input).endsWith('/preview-zip'))!;
  expect(zip[1]!.body).toBeInstanceOf(FormData);
  expect((zip[1]!.body as FormData).get('file')).toBeInstanceOf(File);
  await change('[aria-label="Import mode"]', 'replace');
  expect(button('Apply import').disabled).toBe(true);
  await act(async () => container.querySelector<HTMLInputElement>('.track-import input[type="checkbox"]')!.click());
  await click('Apply import');
  const imports = writes.filter((item) => item.url.endsWith('/imports'));
  expect(imports[imports.length - 1].body).toMatchObject({ mode: 'replace', confirmed_replace: true });
  expect(container.textContent).toContain('Earlier content is retained in version history');
});

it('retains reviewed import content when revision guards reject a stale import', async () => {
  await mount();
  await upload('problem.md');
  await change('[aria-label="Merged Problem Markdown"]', 'Preserved reviewed merge');
  const originalFetch = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation((input, init) => String(input).endsWith('/imports')
    ? Promise.resolve(Response.json({ message: 'Import revisions changed. Preview again.' }, { status: 409 }))
    : originalFetch(input, init));
  await click('Apply import');
  expect(container.textContent).toContain('Import revisions changed');
  expect(container.querySelector<HTMLTextAreaElement>('[aria-label="Merged Problem Markdown"]')!.value).toBe('Preserved reviewed merge');
  expect(saved.problem).toBe('Original problem');
  expect(button('Apply import').disabled).toBe(true);
  expect(button('Refresh import baseline').disabled).toBe(false);
});

it.each(['problem.md', 'track.zip'])('refreshes a stale %s import baseline without losing the reviewed merge and applies explicitly', async (filename) => {
  await mount();
  await upload(filename);
  await change('[aria-label="Merged Problem Markdown"]', 'Preserved reviewed merge');
  const originalFetch = vi.mocked(fetch).getMockImplementation()!;
  let rejectImport = true;
  vi.mocked(fetch).mockImplementation(async (input, init) => {
    if (String(input).endsWith('/imports') && rejectImport) {
      return Response.json({ message: 'Import changed. Preview again.' }, { status: 409 });
    }
    const response = await originalFetch(input, init);
    if (String(input).includes('/imports/preview')) return Response.json({
      preview_id: 'preview-fresh', paper_ids: [], warnings: ['Fresh preview warning'],
      sections: [{ section: 'problem', current_content: 'External evidence added', incoming_content: 'Incoming problem',
        expected_sha256: 'b'.repeat(64), diff: '-External evidence added\n+Incoming problem' }],
    });
    return response;
  });
  await click('Apply import');
  await click('Refresh import baseline');
  expect(container.textContent).toContain('External evidence added');
  expect(container.textContent).toContain('Fresh preview warning');
  expect(container.querySelector<HTMLTextAreaElement>('[aria-label="Merged Problem Markdown"]')!.value).toBe('Preserved reviewed merge');
  expect(button('Apply import').disabled).toBe(true);
  expect(writes.some((item) => item.url.endsWith('/imports'))).toBe(false);
  const previews = vi.mocked(fetch).mock.calls.filter(([input]) => String(input).includes('/imports/preview'));
  if (filename.endsWith('.zip')) {
    expect((previews[1][1]!.body as FormData).get('file')).toBe((previews[0][1]!.body as FormData).get('file'));
  } else expect(JSON.parse(String(previews[1][1]!.body))).toEqual({ section: 'problem', content: 'Incoming problem' });
  confirm.mockReturnValue(true);
  await click('Use refreshed import baseline');
  expect(container.textContent).toContain('Previous import baseline');
  expect(container.textContent).toContain('Original problem');
  await change('[aria-label="Merged Problem Markdown"]', 'Preserved reviewed merge\nExternal evidence added');
  expect(writes.some((item) => item.url.endsWith('/imports'))).toBe(false);
  rejectImport = false;
  await click('Apply import');
  expect(writes.find((item) => item.url.endsWith('/imports'))!.body).toMatchObject({
    preview_id: 'preview-fresh', expected_revisions: { problem: 'b'.repeat(64) },
    merged_content: { problem: 'Preserved reviewed merge\nExternal evidence added' },
  });
});

it('requires replacement confirmation again after refreshing an import baseline and retains merges across modes', async () => {
  await mount();
  await upload('problem.md');
  await change('[aria-label="Merged Problem Markdown"]', 'Retained merge');
  await change('[aria-label="Import mode"]', 'replace');
  await act(async () => container.querySelector<HTMLInputElement>('.track-import input[type="checkbox"]')!.click());
  await click('Refresh import baseline');
  expect(button('Apply import').disabled).toBe(true);
  confirm.mockReturnValue(true);
  await click('Use refreshed import baseline');
  expect(button('Apply import').disabled).toBe(true);
  await change('[aria-label="Import mode"]', 'merge');
  expect(container.querySelector<HTMLTextAreaElement>('[aria-label="Merged Problem Markdown"]')!.value).toBe('Retained merge');
});

it('blocks section changes and navigation while a conditional save is pending', async () => {
  await mount();
  await click('Edit');
  await change('[aria-label="Problem Markdown"]', 'Pending revised evidence');
  const originalFetch = vi.mocked(fetch).getMockImplementation()!;
  let finish!: (response: Response) => void;
  vi.mocked(fetch).mockImplementation((input, init) => init?.method === 'PUT' && String(input).endsWith('/sections/problem')
    ? new Promise<Response>((resolve) => { finish = resolve; }) : originalFetch(input, init));
  await click('Save section');
  expect(button('Literature').disabled).toBe(true);
  expect(container.querySelector<HTMLTextAreaElement>('textarea')!.disabled).toBe(true);
  confirm.mockReturnValue(true);
  await act(async () => container.querySelector<HTMLAnchorElement>('a[href="/settings"]')!.click());
  expect(window.location.pathname).toBe('/tracks');
  expect(confirm).not.toHaveBeenCalled();
  await act(async () => finish(Response.json({ ...track.sections[0], content: 'Pending revised evidence' })));
  expect(container.querySelector<HTMLTextAreaElement>('textarea')!.disabled).toBe(false);
  expect(container.textContent).toContain('Saved locally');
});

it('recovers history into a draft, reuses paper IDs and displays only saved experiment status', async () => {
  await mount();
  await click('Version history');
  await click(`View version old-1 · ${new Date(stamp).toLocaleString()}`);
  await click('Recover into draft');
  expect(container.querySelector<HTMLTextAreaElement>('textarea')!.value).toBe('Prior evidence');
  expect(writes.some((item) => item.url.endsWith('/sections/problem'))).toBe(false);
  await click('Add existing');
  await change('[aria-label="Library paper"]', 'paper-1');
  await click('Link paper');
  expect(writes.some((item) => item.url.endsWith('/papers/paper-1'))).toBe(true);
  await click('Remove paper');
  await click('Experiments');
  expect(container.textContent).toContain('EXP-001');
  expect(container.textContent).toContain('Planned: 1');
  expect(container.textContent).not.toContain('%');
  await change('[aria-label="Experiments Markdown"]', 'Unsaved Running experiment');
  expect(container.textContent).toContain('Planned: 1');
  const assist = container.querySelector<HTMLAnchorElement>('a[href*="research="]')!;
  expect(new URL(assist.href).searchParams.get('track')).toBe('track-1');
  expect(new URL(assist.href).searchParams.get('research')).toContain('do not save files');
});

it('adds existing reviews, confirms moves from other tracks and retains notebook drafts', async () => {
  await mount();
  await click('Edit');
  await change('[aria-label="Problem Markdown"]', 'Keep this personal draft');
  await click('Add existing');
  await change('[aria-label="Saved review"]', 'review-1');
  await click('Add review to track');
  expect(writes.find((item) => item.url.endsWith('/review-1/track'))?.body).toEqual({ track_id: 'track-1', expected_revision: 4 });
  const linked = container.querySelector('[aria-label="Linked reviews"]')!;
  expect(linked.textContent).toContain('Existing review');
  expect(linked.querySelector('a[href*="review=review-1"]')).not.toBeNull();
  expect(linked.querySelector('a[href*="knowledge%2Ffindings.md"]')).not.toBeNull();
  await change('[aria-label="Saved review"]', 'review-2');
  await click('Add review to track');
  expect(confirm).toHaveBeenCalledWith(expect.stringContaining('Another track'));
  expect(writes.some((item) => item.url.endsWith('/review-2/track'))).toBe(false);
  confirm.mockReturnValue(true);
  await click('Add review to track');
  expect(linked.textContent).toContain('Another track review');
  expect(container.querySelector<HTMLTextAreaElement>('[aria-label="Problem Markdown"]')?.value).toBe('Keep this personal draft');
  await click('Reader highlights');
  expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes('/tracks/track-1/reader-notes'))).toBe(true);
  await click('Notebook');
  expect(container.querySelector<HTMLTextAreaElement>('[aria-label="Problem Markdown"]')?.value).toBe('Keep this personal draft');
});

it('preserves selection and shows review conflicts until saved reviews are refreshed', async () => {
  await mount();
  await click('Add existing');
  await change('[aria-label="Saved review"]', 'review-1');
  reviewConflict = true;
  await click('Add review to track');
  expect(container.textContent).toContain('The review changed');
  expect(container.querySelector<HTMLSelectElement>('[aria-label="Saved review"]')?.value).toBe('review-1');
  expect(container.querySelector('[aria-label="Linked reviews"]')?.textContent).not.toContain('Existing review');
  reviews[0].revision = 5;
  await click('Refresh saved reviews');
  reviewConflict = false;
  await click('Add review to track');
  expect(writes.filter((item) => item.url.endsWith('/review-1/track')).at(-1)?.body.expected_revision).toBe(5);
});

it('links and detaches existing chats and builds track-associated unsent assistance prompts', async () => {
  let selected = '';
  const onError = vi.fn();
  const render = () => <RouterProvider><TrackChatSelector conversationId="chat-1" trackId={selected} disabled={false} onError={onError} onChange={(id) => { selected = id; root.render(render()); }} /></RouterProvider>;
  await act(async () => root.render(render()));
  await change('[aria-label="Conversation research track"]', 'track-1');
  expect(writes[writes.length - 1].url).toBe('/api/tracks/track-1/conversations/chat-1');
  expect(container.querySelector('a[href="/tracks?id=track-1"]')).not.toBeNull();
  await change('[aria-label="Conversation research track"]', '');
  expect(selected).toBe('');
  const remove = vi.mocked(fetch).mock.calls.find(([input, init]) => String(input).includes('/conversations/chat-1') && init?.method === 'DELETE');
  expect(remove).toBeDefined();
  expect(onError).not.toHaveBeenCalled();
  const target = new URL(trackAssistUrl(track, 'literature', 'literature-map'), 'http://localhost');
  expect(target.searchParams.get('track')).toBe(track.id);
  expect(target.searchParams.get('research')).toContain('literature-map skill');
  expect(target.searchParams.get('research')).toContain('Discuss proposed changes in chat');
});
