// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AppShell } from '../../app/AppShell';
import { useAsk } from '../../app/ask';
import { Link, RouterProvider, useLocation } from '../../app/router';
import { ThemeProvider } from '../../shared/theme/ThemeProvider';
import ChatPage, { ResearchChatPage } from './ChatPage';
import { conversationDraftKey, saveChatDraft } from './chatDrafts';

const MODEL = { provider_profile_id: 'local', model: 'first' };
const PAPER = { document_id: 'paper-1', title: 'A relevant paper', page: 4, selection: 'Selected evidence' };
const SETTINGS = { agent_context_window_tokens: 32768, default_model_references: { chat: MODEL } };
const PROVIDERS = [{
  id: 'local', name: 'Local', kind: 'openai_compatible', state: 'ready',
  models: ['first', 'second'].map((name) => ({
    id: name, name, enabled: true, context_window_tokens: 32768, reasoning_efforts: [],
  })),
}];
const ATTACHMENT = {
  path: 'inbox/notes.md', name: 'notes.md', media_type: 'text/plain', size_bytes: 5, document_id: null,
};

function conversation(id: string) {
  return {
    id, title: `Chat ${id}`, kind: 'autonomous', model_reference: MODEL,
    session_policy: {}, status: 'idle', last_message_preview: '',
    created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z', track_id: null,
    items: [{ id: `${id}-answer`, role: 'assistant', type: 'message', text: `Transcript for ${id}` }],
  };
}

function runningRun(conversationId: string) {
  return {
    id: `run-${conversationId}`, conversation_id: conversationId, agent_name: 'Research',
    status: 'running', input: 'An ongoing question', final_output: null, usage: {}, error: null,
    cancel_requested: false, created_at: '2026-01-01T00:00:00Z', started_at: null, finished_at: null,
    items: [], events: [],
  };
}

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

function ShellPages() {
  const ask = useAsk();
  const { pathname } = useLocation();
  return pathname === '/chat' ? <ChatPage /> : (
    <>
      <button onClick={() => ask.ask()}>Open assistant</button>
      <button onClick={() => ask.ask(undefined, { context: PAPER })}>Ask about paper</button>
      <Link to="/chat">Go to chat</Link>
      <p>Reading workspace</p>
    </>
  );
}

describe('chat continuity and recovery', () => {
  let root: Root;
  let container: HTMLDivElement;
  let conversations: ReturnType<typeof conversation>[];
  let activeRuns: Record<string, ReturnType<typeof runningRun>[]>;
  let steeringGate: Promise<Response> | null;
  let messageGate: Promise<Response> | null;
  let failures: Set<string>;
  let requests: { url: string; method: string; body: Record<string, unknown> }[];

  beforeEach(() => {
    conversations = [conversation('one'), conversation('two')];
    activeRuns = {};
    steeringGate = null;
    messageGate = null;
    failures = new Set();
    requests = [];
    localStorage.clear();
    sessionStorage.clear();
    window.history.replaceState({}, '', '/');
    (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
    vi.stubGlobal('scrollTo', vi.fn());
    vi.stubGlobal('confirm', vi.fn(() => true));
    vi.stubGlobal('EventSource', class {
      addEventListener() {}
      close() {}
    });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? 'GET';
      const body = typeof init?.body === 'string' ? JSON.parse(init.body) : {};
      requests.push({ url, method, body });
      if (url.endsWith('/api/settings')) {
        return failures.has('settings') ? response({ message: 'Settings unavailable' }, 503) : response(SETTINGS);
      }
      if (url.endsWith('/api/providers')) {
        return failures.has('providers') ? response({ message: 'Models unavailable' }, 503) : response(PROVIDERS);
      }
      if (url.endsWith('/api/tracks')) return response([]);
      if (url.endsWith('/api/agent/attachments')) return response(ATTACHMENT, 201);
      if (url.endsWith('/api/agent/conversations')) {
        if (method === 'POST') {
          const created = { ...conversation('created'), model_reference: body.model_reference, items: [] };
          conversations.push(created);
          return response(created, 201);
        }
        return failures.has('list') ? response({ message: 'History unavailable' }, 503) : response(conversations);
      }
      const sending = /\/conversations\/([^/]+)\/messages$/.exec(url);
      if (sending) return messageGate ?? response({
        conversation: conversations.find((item) => item.id === sending[1]),
        run: runningRun(sending[1]),
      });
      if (/\/runs\/[^/]+\/steering$/.test(url)) {
        return steeringGate ?? response({ id: 'steering-1', content: body.content, status: 'queued' });
      }
      if (/\/runs\/[^/]+\/stop-and-answer$/.test(url)) return response({
        stopped_run: { ...runningRun('one'), status: 'cancelled' },
        answer_run: { ...runningRun('one'), id: 'answer-run' },
      });
      if (url.includes('/api/runs?conversation_id=')) {
        const id = new URL(url, 'http://localhost').searchParams.get('conversation_id')!;
        return response(activeRuns[id] ?? []);
      }
      const get = /\/api\/agent\/conversations\/([^/]+)$/.exec(url);
      if (get) return failures.has('conversation') ? response({ message: 'Conversation unavailable' }, 503)
        : response(conversations.find((item) => item.id === get[1]) ?? conversation(get[1]));
      throw new Error(`Unexpected request ${method} ${url}`);
    }));
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  async function mount(shell = false) {
    if (shell) await import('./AskPanel');
    await act(async () => {
      root.render(<ThemeProvider><RouterProvider>{shell ? <AppShell><ShellPages /></AppShell> : <ChatPage />}</RouterProvider></ThemeProvider>);
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
  }

  const composer = () => container.querySelector<HTMLTextAreaElement>('.composer-box textarea')!;
  const button = (label: string) => container.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`)!;
  const byText = (text: string) => [...container.querySelectorAll<HTMLElement>('button, a')]
    .find((element) => element.textContent?.trim() === text)!;

  async function click(element: HTMLElement) {
    await act(async () => { element.click(); });
  }

  async function type(value: string) {
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(composer(), value);
      composer().dispatchEvent(new Event('input', { bubbles: true }));
    });
  }

  async function attach() {
    const input = container.querySelector<HTMLInputElement>('input[type="file"]')!;
    await act(async () => {
      Object.defineProperty(input, 'files', { configurable: true, value: [new File(['notes'], 'notes.md')] });
      input.dispatchEvent(new Event('change', { bubbles: true }));
    });
  }

  async function openConversation(id: string) {
    const element = [...container.querySelectorAll<HTMLButtonElement>('.conversation-open')]
      .find((item) => item.title === `Chat ${id}`)!;
    await click(element);
  }

  it('preserves assistant text, paper context and attachments across hide, navigation, reinvocation and full-chat handoff', async () => {
    await mount(true);
    await click(byText('Ask about paper'));
    expect(container.querySelector('h1')?.textContent).toBe('New chat');
    await type('  Compare this passage with my notes  ');
    await attach();
    await click(button('Close assistant'));
    expect(container.querySelector('.ask-panel')).toBeNull();
    await click(byText('Notes'));
    expect(window.location.pathname).toBe('/library/notes');
    await click(byText('Open assistant'));
    expect(composer().value).toBe('  Compare this passage with my notes  ');
    expect(container.querySelector('[aria-label="Reading context"]')?.textContent).toContain(PAPER.title);
    expect(button('Remove notes.md')).not.toBeNull();

    await click(container.querySelector<HTMLAnchorElement>('[aria-label="Open full chat history"]')!);
    expect(window.location.pathname).toBe('/chat');
    expect(composer().value).toBe('  Compare this passage with my notes  ');
    expect(button('Remove notes.md')).not.toBeNull();
    expect(container.querySelector('[aria-label="Reading context"]')?.textContent).toContain(PAPER.title);
    expect(requests.filter((item) => item.method === 'POST' && item.url.endsWith('/messages'))).toEqual([]);
    expect(window.confirm).not.toHaveBeenCalled();
  });

  it('hands the selected assistant conversation and its unsent draft to full chat', async () => {
    await mount(true);
    await click(byText('Open assistant'));
    await openConversation('two');
    await type('Follow up in the same thread');
    await click(button('Close assistant'));
    await click(byText('Open assistant'));
    expect(container.querySelector('h1')?.textContent).toBe('Chat two');
    const link = container.querySelector<HTMLAnchorElement>('[aria-label="Open full chat history"]')!;
    expect(new URL(link.href).searchParams.get('conversation')).toBe('two');
    await click(link);
    expect(container.querySelector('h1')?.textContent).toBe('Chat two');
    expect(container.textContent).toContain('Transcript for two');
    expect(composer().value).toBe('Follow up in the same thread');
  });

  it('restores full-chat drafts after leaving the page without a hidden navigation guard', async () => {
    window.history.replaceState({}, '', '/chat');
    await mount(true);
    await type('Draft that should survive navigation');
    await attach();
    await click(byText('Notes'));
    await click(byText('Go to chat'));
    expect(composer().value).toBe('Draft that should survive navigation');
    expect(button('Remove notes.md')).not.toBeNull();
    expect(container.textContent).toContain('Transcript for one');
    expect(window.confirm).not.toHaveBeenCalled();
  });

  it('keeps independent unsent text and attachments with each conversation', async () => {
    await mount();
    await type('First thread draft');
    await attach();
    await openConversation('two');
    expect(composer().value).toBe('');
    expect(container.querySelector('[aria-label="Attached files"]')).toBeNull();
    await type('Second thread draft');
    await openConversation('one');
    expect(composer().value).toBe('First thread draft');
    expect(button('Remove notes.md')).not.toBeNull();
    await openConversation('two');
    expect(composer().value).toBe('Second thread draft');
  });

  it('retains the original draft and attachments when opening another conversation fails', async () => {
    await mount();
    await type('Unsent text in the original chat');
    await attach();
    failures.add('conversation');
    await openConversation('two');
    expect(container.querySelector('h1')?.textContent).toBe('Chat one');
    expect(composer().value).toBe('Unsent text in the original chat');
    expect(button('Remove notes.md')).not.toBeNull();
    failures.delete('conversation');
    await openConversation('two');
    await openConversation('one');
    expect(composer().value).toBe('Unsent text in the original chat');
    expect(button('Remove notes.md')).not.toBeNull();
  });

  it('starts paper-only Ask in a suitable new thread and transmits paper context on ordinary send', async () => {
    activeRuns.one = [runningRun('one')];
    await act(async () => {
      root.render(<RouterProvider><ResearchChatPage embedded readingContext={PAPER} /></RouterProvider>);
    });
    expect(container.querySelector('h1')?.textContent).toBe('New chat');
    expect(requests.some((item) => item.url.endsWith('/conversations/one'))).toBe(false);
    await type('Explain this passage');
    await click(button('Send message'));
    const sent = requests.find((item) => item.url.endsWith('/messages'))!;
    expect(sent.url).toContain('/conversations/created/messages');
    expect(sent.body.reading_context).toEqual({
      document_id: PAPER.document_id, page: PAPER.page, selection: PAPER.selection,
    });
    expect(requests.some((item) => item.url.endsWith('/steering'))).toBe(false);
  });

  it('does not send text-only steering when the composer promises paper context', async () => {
    saveChatDraft(conversationDraftKey('one'), {
      conversationId: 'one', content: 'Use this paper', reading: PAPER, attachments: [],
      selectedIdea: '', otherIdea: '', intent: 'chat', trackId: '', modelReference: MODEL,
      responseEffort: 'auto', webEnabled: true,
    });
    activeRuns.one = [runningRun('one')];
    window.history.replaceState({}, '', '/chat?conversation=one');
    await mount();
    expect(button('Queue steering message').disabled).toBe(true);
    expect(container.textContent).toContain('Paper context cannot be added to ongoing work');
    await act(async () => {
      composer().dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    });
    expect(requests.some((item) => item.url.endsWith('/steering'))).toBe(false);
    expect(composer().value).toBe('Use this paper');
  });

  it.each(['settings', 'providers', 'list'])('recovers a failed %s bootstrap without losing successful requests or drafts', async (failed) => {
    failures.add(failed);
    await mount();
    expect(container.querySelector('[role="alert"]')).not.toBeNull();
    expect(container.textContent).not.toContain('Loading chats…');
    if (failed !== 'settings') await type('Keep this successful composer state');
    const successful = requests.filter((item) => item.url.endsWith('/api/settings') || item.url.endsWith('/api/providers')
      || item.url.endsWith('/api/agent/conversations'))
      .filter((item) => !item.url.endsWith(failed === 'list' ? '/api/agent/conversations' : `/api/${failed}`));
    failures.delete(failed);
    await click(byText('Retry loading chats'));
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(composer()).not.toBeNull();
    if (failed !== 'settings') expect(composer().value).toBe('Keep this successful composer state');
    for (const request of successful) {
      expect(requests.filter((item) => item.url === request.url && item.method === 'GET')).toHaveLength(1);
    }
  });

  it('offers retry for an initial conversation failure rather than silently spinning', async () => {
    failures.add('conversation');
    window.history.replaceState({}, '', '/chat?conversation=one');
    await mount();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('Conversation unavailable');
    failures.delete('conversation');
    await click(byText('Retry loading chats'));
    expect(container.querySelector('h1')?.textContent).toBe('Chat one');
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  it('explains and confirms a model change without silently discarding the transcript or unsent context', async () => {
    saveChatDraft(conversationDraftKey('one'), {
      conversationId: 'one', content: 'Keep my question', reading: PAPER, attachments: [ATTACHMENT],
      selectedIdea: '', otherIdea: '', intent: 'chat', trackId: '', modelReference: MODEL,
      responseEffort: 'quick', webEnabled: false,
    });
    window.history.replaceState({}, '', '/chat?conversation=one');
    await mount();
    vi.mocked(window.confirm).mockReturnValueOnce(false);
    const chooseSecond = async () => {
      await click(button('Main model'));
      await click([...container.querySelectorAll<HTMLButtonElement>('[role="option"]')]
        .find((item) => item.textContent?.includes('second'))!);
    };
    await chooseSecond();
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining('without the existing transcript'));
    expect(container.querySelector('h1')?.textContent).toBe('Chat one');
    expect(container.textContent).toContain('Transcript for one');
    expect(composer().value).toBe('Keep my question');

    await chooseSecond();
    expect(container.querySelector('h1')?.textContent).toBe('New chat');
    expect(composer().value).toBe('Keep my question');
    expect(button('Remove notes.md')).not.toBeNull();
    expect(container.querySelector('[aria-label="Reading context"]')?.textContent).toContain(PAPER.title);
    expect(container.querySelector<HTMLSelectElement>('[aria-label="Response effort"]')?.value).toBe('quick');
    expect(requests.some((item) => item.url.endsWith('/conversations') && item.method === 'POST')).toBe(false);
    await click(button('Send message'));
    const created = requests.find((item) => item.url.endsWith('/conversations') && item.method === 'POST')!;
    expect(created.body.model_reference).toEqual({ ...MODEL, model: 'second' });
    const sent = requests.find((item) => item.url.endsWith('/messages'))!;
    expect(sent.body).toMatchObject({
      content: 'Keep my question', attachment_paths: [ATTACHMENT.path], web_enabled: false,
      reading_context: { document_id: PAPER.document_id, page: PAPER.page, selection: PAPER.selection },
    });
    await openConversation('one');
    expect(container.textContent).toContain('Transcript for one');
  });

  it.each(['success', 'failure'])('ignores delayed steering %s after changing conversations', async (outcome) => {
    activeRuns.one = [runningRun('one')];
    const pending = deferred<Response>();
    steeringGate = pending.promise;
    await mount();
    await type('Steering for the first run');
    await click(button('Queue steering message'));
    await openConversation('two');
    await type('Newer text in a different chat');
    await act(async () => {
      pending.resolve(outcome === 'success'
        ? response({ id: 'old-steering', content: 'Steering for the first run', status: 'queued' })
        : response({ message: 'Old steering failed' }, 503));
    });
    expect(composer().value).toBe('Newer text in a different chat');
    expect(container.querySelector('.steering-queued')).toBeNull();
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  it('does not overwrite a newer draft when steering fails in the same run', async () => {
    activeRuns.one = [runningRun('one')];
    const pending = deferred<Response>();
    steeringGate = pending.promise;
    await mount();
    await type('Earlier steering text');
    await click(button('Queue steering message'));
    await type('Newer steering draft');
    await act(async () => { pending.resolve(response({ message: 'Steering rejected' }, 503)); });
    expect(composer().value).toBe('Newer steering draft');
    expect(container.textContent).toContain('Your newer draft is unchanged');
    await click(byText('Add unsent message to draft'));
    expect(composer().value).toBe('Earlier steering text\n\nNewer steering draft');
  });

  it('ignores a steering response for a replaced run and allows steering the replacement', async () => {
    activeRuns.one = [runningRun('one')];
    const pending = deferred<Response>();
    steeringGate = pending.promise;
    await mount();
    await type('Old run guidance');
    await click(button('Queue steering message'));
    await click(button('Stop and answer with available information'));
    await type('Guidance for the replacement');
    expect(button('Queue steering message').disabled).toBe(false);
    await act(async () => {
      pending.resolve(response({ id: 'old-steering', content: 'Old run guidance', status: 'queued' }));
    });
    expect(composer().value).toBe('Guidance for the replacement');
    expect(container.querySelector('.steering-queued')).toBeNull();
  });

  it('does not overwrite a newer draft when an ordinary message fails', async () => {
    const pending = deferred<Response>();
    messageGate = pending.promise;
    await mount();
    await type('Earlier ordinary message');
    await click(button('Send message'));
    await type('Newer composer text');
    await act(async () => { pending.resolve(response({ message: 'Message rejected' }, 503)); });
    expect(composer().value).toBe('Newer composer text');
    expect(container.textContent).toContain('Earlier ordinary message');
    expect(button('Send message').disabled).toBe(false);
  });

  it('does not clear a retyped identical draft or new attachment when an older send succeeds', async () => {
    const pending = deferred<Response>();
    messageGate = pending.promise;
    await mount();
    await type('The same words');
    await click(button('Send message'));
    await openConversation('two');
    await openConversation('one');
    await type('A deliberate edit');
    await type('The same words');
    await attach();
    await act(async () => {
      pending.resolve(response({ conversation: conversation('one'), run: runningRun('one') }));
    });
    await openConversation('two');
    await openConversation('one');
    expect(composer().value).toBe('The same words');
    expect(button('Remove notes.md')).not.toBeNull();
  });
});
