// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import ChatPage from './ChatPage';
import type { ConversationAttachment, ResponseEffort } from './api';

const MODEL = { provider_profile_id: 'local', model: 'research-model' };
const SETTINGS = {
  agent_context_window_tokens: 32768,
  agent_context_use_model_window: true,
  default_model_references: { chat: MODEL },
};
const PROVIDERS = [{
  id: 'local',
  name: 'Local',
  kind: 'openai_compatible',
  archived: false,
  models: [{
    id: 'research-model', name: 'research-model', enabled: true,
    context_window_tokens: 32768, reasoning_efforts: [],
  }],
}];

function conversation(id: string) {
  return {
    id, title: `Chat ${id}`, kind: 'autonomous', model_reference: MODEL,
    session_policy: {}, status: 'idle', last_message_preview: '',
    created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
    items: [],
  };
}

function attachment(name = 'notes.md'): ConversationAttachment {
  const pdf = name.toLowerCase().endsWith('.pdf');
  return {
    path: pdf ? 'research/papers/paper-1/text.md' : `research/uploads/${name}`,
    name,
    media_type: pdf ? 'application/pdf' : 'text/plain',
    size_bytes: 123,
    document_id: pdf ? 'paper-1' : null,
  };
}

function response(body: unknown, status = 200): Response {
  return { ok: status < 400, status, json: async () => body } as Response;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

describe('chat attachments', () => {
  let container: HTMLDivElement;
  let root: Root;
  let conversations: ReturnType<typeof conversation>[];
  let upload: ReturnType<typeof vi.fn<(file: File, signal?: AbortSignal | null) => Promise<Response>>>;
  let messageRequests: Record<string, unknown>[];
  let createRequests: number;
  let failMessage: boolean;
  let messageGate: Promise<Response> | null;
  let openGate: Promise<Response> | null;

  beforeEach(() => {
    conversations = [];
    messageRequests = [];
    createRequests = 0;
    failMessage = false;
    messageGate = null;
    openGate = null;
    upload = vi.fn(async (file: File) => response(attachment(file.name), 201));
    window.localStorage.clear();
    window.history.replaceState({}, '', '/');
    (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
    vi.stubGlobal('EventSource', class {
      addEventListener() {}
      close() {}
    });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? 'GET';
      if (url.endsWith('/api/providers')) return response(PROVIDERS);
      if (url.endsWith('/api/settings')) return response(SETTINGS);
      if (url.endsWith('/api/agent/attachments')) {
        return upload((init!.body as FormData).get('file') as File, init?.signal);
      }
      if (url.endsWith('/api/agent/conversations')) {
        if (method === 'POST') {
          createRequests += 1;
          const created = conversation('created');
          conversations.push(created);
          return response(created, 201);
        }
        return response(conversations);
      }
      const sendMatch = /\/api\/agent\/conversations\/([^/]+)\/messages$/.exec(url);
      if (sendMatch) {
        const payload = JSON.parse(String(init?.body)) as Record<string, unknown>;
        messageRequests.push(payload);
        if (messageGate) return messageGate;
        if (failMessage) return response({ message: 'Could not start research' }, 503);
        return response({
          conversation: conversation(sendMatch[1]),
          run: {
            id: 'run-1', conversation_id: sendMatch[1], agent_name: 'Research',
            status: 'pending', input: payload.content, final_output: null,
            usage: {}, error: null, cancel_requested: false,
            created_at: '2026-01-01T00:00:00Z', started_at: null, finished_at: null,
            items: [], events: [],
          },
        });
      }
      const getMatch = /\/api\/agent\/conversations\/([^/]+)$/.exec(url);
      if (getMatch) {
        if (getMatch[1] === 'two' && openGate) return openGate;
        return response(conversation(getMatch[1]));
      }
      if (url.includes('/api/runs?conversation_id=')) return response([]);
      throw new Error(`Unhandled request: ${method} ${url}`);
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

  async function mount() {
    await act(async () => { root.render(<ChatPage />); });
  }

  function button(label: string) {
    return container.querySelector<HTMLButtonElement>(`[aria-label="${label}"]`)!;
  }

  async function click(element: HTMLElement) {
    await act(async () => { element.click(); });
  }

  function effortSelect() {
    return container.querySelector<HTMLSelectElement>('[aria-label="Response effort"]')!;
  }

  async function selectEffort(value: ResponseEffort) {
    await act(async () => {
      effortSelect().value = value;
      effortSelect().dispatchEvent(new Event('change', { bubbles: true }));
    });
  }

  async function type(value: string) {
    const textarea = container.querySelector('textarea')!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')!.set!
        .call(textarea, value);
      textarea.dispatchEvent(new Event('input', { bubbles: true }));
    });
  }

  async function selectFiles(files: File[]) {
    const input = container.querySelector<HTMLInputElement>('input[type="file"]')!;
    await act(async () => {
      Object.defineProperty(input, 'files', { configurable: true, value: files });
      input.dispatchEvent(new Event('change', { bubbles: true }));
    });
    expect(input.value).toBe('');
  }

  async function enter() {
    await act(async () => {
      container.querySelector('textarea')!.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'Enter', bubbles: true,
      }));
    });
  }

  it('uploads before the first conversation and requires an explicit request before sending paths', async () => {
    await mount();
    const input = container.querySelector<HTMLInputElement>('input[type="file"]')!;
    expect(input.accept).toBe('.md,.txt,.pdf');
    expect(input.multiple).toBe(true);
    expect(button('Attach file').disabled).toBe(false);
    const picker = vi.spyOn(input, 'click');
    await click(button('Attach file'));
    expect(picker).toHaveBeenCalledOnce();

    await selectFiles([new File(['notes'], 'notes.md'), new File(['%PDF'], 'paper.pdf')]);
    expect(upload).toHaveBeenCalledTimes(2);
    expect(container.querySelector('[aria-label="Attached files"]')?.textContent)
      .toContain('paper.pdf');
    expect(button('Send message').disabled).toBe(true);
    await enter();
    expect(createRequests).toBe(0);
    expect(messageRequests).toEqual([]);
    expect(container.textContent).toContain('default 2 MiB');
    expect(container.textContent).toContain('default 40 MiB');

    await type('Compare the attached paper with my notes');
    await click(button('Send message'));
    expect(createRequests).toBe(1);
    expect(messageRequests).toEqual([expect.objectContaining({
      content: 'Compare the attached paper with my notes',
      attachment_paths: ['research/uploads/notes.md', 'research/papers/paper-1/text.md'],
      response_effort: 'auto',
    })]);
    expect(container.querySelector('[aria-label="Attached files"]')).toBeNull();
    expect(container.querySelector('textarea')!.value).toBe('');
    expect(button('Attach file').disabled).toBe(true);
    await selectFiles([new File(['more notes'], 'later.md')]);
    expect(upload).toHaveBeenCalledTimes(2);
  });

  it('blocks sending while preparing a PDF without disabling Quick or losing its offline selection', async () => {
    const pending = deferred<Response>();
    upload.mockReturnValueOnce(pending.promise);
    await mount();
    await type('Explain this paper');
    await selectEffort('quick');
    await click(button('Toggle web access'));
    await selectFiles([new File(['%PDF'], 'paper.pdf')]);

    expect(container.querySelector('.composer-upload-status')?.textContent)
      .toContain('Uploading / preparing: paper.pdf');
    expect(container.querySelector('.composer-upload-status')?.textContent).not.toContain('%');
    expect(button('Send message').disabled).toBe(true);
    expect(button('Attach file').disabled).toBe(true);
    expect(effortSelect().disabled).toBe(false);
    expect(effortSelect().value).toBe('quick');
    const suggestion = container.querySelector<HTMLButtonElement>('.suggestion')!;
    expect(suggestion.disabled).toBe(true);
    await click(suggestion);
    await click(button('Send message'));
    await enter();
    expect(messageRequests).toEqual([]);
    expect(createRequests).toBe(0);
    expect(container.querySelector('textarea')!.value).toBe('Explain this paper');

    await act(async () => { pending.resolve(response(attachment('paper.pdf'), 201)); });
    expect(container.querySelector('.composer-upload-status')).toBeNull();
    expect(button('Send message').disabled).toBe(false);
    expect(effortSelect().disabled).toBe(false);
    expect(effortSelect().value).toBe('quick');
    expect(container.textContent).not.toContain('cannot use attached files');
    await click(button('Send message'));
    expect(messageRequests[0]).toMatchObject({
      response_effort: 'quick',
      web_enabled: false,
      attachment_paths: ['research/papers/paper-1/text.md'],
    });
    expect(effortSelect().value).toBe('auto');
  });

  it('lets Quick use attached text and web access together', async () => {
    await mount();
    await selectFiles([new File(['notes'], 'notes.md')]);
    await selectEffort('quick');
    await type('Check these notes against current sources');
    await click(button('Send message'));
    expect(messageRequests[0]).toMatchObject({
      response_effort: 'quick',
      web_enabled: true,
      attachment_paths: ['research/uploads/notes.md'],
    });
  });

  it('preserves uploaded files when a suggestion replaces the draft without auto-sending', async () => {
    await mount();
    await selectFiles([new File(['notes'], 'notes.txt')]);
    const suggestion = container.querySelector<HTMLButtonElement>('.suggestion')!;
    await click(suggestion);
    expect(container.querySelector('textarea')!.value).toBe(suggestion.textContent);
    expect(button('Remove notes.txt')).not.toBeNull();
    expect(messageRequests).toEqual([]);
    await click(button('Send message'));
    expect(messageRequests[0].attachment_paths).toEqual(['research/uploads/notes.txt']);
  });

  it('preserves the exact draft and attachments on a rejected send, then clears them on acceptance', async () => {
    failMessage = true;
    await mount();
    await selectEffort('thorough');
    await type('  Explain the assumptions in my notes  ');
    await selectFiles([new File(['notes'], 'notes.md')]);
    await click(button('Send message'));

    expect(container.querySelector('textarea')!.value).toBe('  Explain the assumptions in my notes  ');
    expect(button('Remove notes.md')).not.toBeNull();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('Could not start research');
    expect(button('Send message').disabled).toBe(false);
    expect(effortSelect().value).toBe('thorough');
    expect(container.querySelector('[aria-label="Stop current run"]')).toBeNull();
    failMessage = false;
    await click(button('Send message'));
    expect(messageRequests).toHaveLength(2);
    expect(messageRequests[1].attachment_paths).toEqual(['research/uploads/notes.md']);
    expect(messageRequests[1].response_effort).toBe('thorough');
    expect(upload).toHaveBeenCalledOnce();
    expect(container.querySelector('[aria-label="Attached files"]')).toBeNull();
    expect(effortSelect().value).toBe('auto');
  });

  it('keeps attachments until the send response accepts the message', async () => {
    const pending = deferred<Response>();
    messageGate = pending.promise;
    await mount();
    await selectEffort('thorough');
    await type('Explain my notes');
    await selectFiles([new File(['notes'], 'notes.md')]);
    await click(button('Send message'));
    expect(button('Remove notes.md').disabled).toBe(true);
    expect(effortSelect().value).toBe('thorough');
    await act(async () => { pending.resolve(response({ message: 'Try again' }, 503)); });
    expect(button('Remove notes.md').disabled).toBe(false);
    expect(container.querySelector('textarea')!.value).toBe('Explain my notes');
    expect(effortSelect().value).toBe('thorough');
  });

  it('shows failed upload errors without losing the draft or other uploaded files or starting a run', async () => {
    await mount();
    await type('Analyze both files');
    await selectFiles([new File(['notes'], 'notes.md')]);
    upload.mockResolvedValueOnce(response({ message: 'PDF extraction failed' }, 422));
    await selectFiles([new File(['%PDF'], 'broken.pdf')]);
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('broken.pdf: PDF extraction failed');
    expect(container.querySelector('textarea')!.value).toBe('Analyze both files');
    expect(button('Remove notes.md')).not.toBeNull();
    expect(container.querySelector('[aria-label="Remove broken.pdf"]')).toBeNull();
    expect(container.querySelector('.composer-upload-status')).toBeNull();
    expect(createRequests).toBe(0);
    expect(messageRequests).toEqual([]);
  });

  it('keeps successful files from a multi-file selection when another upload fails', async () => {
    upload
      .mockResolvedValueOnce(response({ message: 'File exceeds the upload limit' }, 413))
      .mockResolvedValueOnce(response(attachment('small.txt'), 201));
    await mount();
    await type('Compare these notes');
    await selectFiles([new File(['large'], 'large.txt'), new File(['small'], 'small.txt')]);
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('large.txt: File exceeds');
    expect(button('Remove small.txt')).not.toBeNull();
    expect(container.querySelector('[aria-label="Remove large.txt"]')).toBeNull();
    expect(container.querySelector('textarea')!.value).toBe('Compare these notes');
    expect(createRequests).toBe(0);
    expect(messageRequests).toEqual([]);
  });

  it('detaches files without deleting uploaded research or changing effort', async () => {
    await mount();
    await selectFiles([new File(['notes'], 'notes.md')]);
    await selectEffort('quick');
    expect(effortSelect().disabled).toBe(false);
    expect(container.textContent).toContain('Removing an attachment keeps the uploaded file');
    await click(button('Remove notes.md'));
    expect(container.querySelector('[aria-label="Attached files"]')).toBeNull();
    expect(effortSelect().disabled).toBe(false);
    expect(effortSelect().value).toBe('quick');
    expect(vi.mocked(fetch).mock.calls.some(([, init]) => init?.method === 'DELETE')).toBe(false);
    await type('Discuss my research');
    await click(button('Send message'));
    expect(messageRequests[0]).not.toHaveProperty('attachment_paths');
  });

  it('clears uploaded references and resets effort on a new chat and when opening a different chat', async () => {
    conversations = [conversation('one'), conversation('two')];
    await mount();
    await selectFiles([new File(['first'], 'first.md')]);
    await selectEffort('thorough');
    await click(container.querySelector<HTMLButtonElement>('.new-chat')!);
    expect(container.querySelector('[aria-label="Attached files"]')).toBeNull();
    expect(effortSelect().value).toBe('auto');
    await selectFiles([new File(['second'], 'second.md')]);
    await selectEffort('thorough');
    const second = [...container.querySelectorAll<HTMLButtonElement>('.conversation button')]
      .find((candidate) => candidate.textContent?.includes('Chat two'))!;
    await click(second);
    expect(container.querySelector('[aria-label="Attached files"]')).toBeNull();
    expect(effortSelect().value).toBe('auto');
    await type('Discuss this chat');
    await click(button('Send message'));
    expect(messageRequests[0]).not.toHaveProperty('attachment_paths');
    expect(messageRequests[0].response_effort).toBe('auto');
  });

  it.each(['resolve', 'reject'] as const)('ignores an old upload %s after switching chats, even during another upload', async (outcome) => {
    conversations = [conversation('one'), conversation('two')];
    const old = deferred<Response>();
    const next = deferred<Response>();
    const opening = deferred<Response>();
    upload.mockReturnValueOnce(old.promise).mockReturnValueOnce(next.promise);
    await mount();
    await type('Discuss my notes');
    await selectFiles([new File(['old'], 'old.md')]);
    const oldSignal = upload.mock.calls[0][1]!;
    openGate = opening.promise;
    const second = [...container.querySelectorAll<HTMLButtonElement>('.conversation button')]
      .find((candidate) => candidate.textContent?.includes('Chat two'))!;
    await click(second);
    expect(oldSignal.aborted).toBe(true);
    expect(button('Attach file').disabled).toBe(true);
    await enter();
    expect(messageRequests).toEqual([]);
    await act(async () => { opening.resolve(response(conversation('two'))); });
    await selectFiles([new File(['new'], 'new.md')]);
    await act(async () => {
      old.resolve(outcome === 'resolve'
        ? response(attachment('old.md'), 201)
        : response({ message: 'Old upload failed' }, 422));
    });
    expect(container.querySelector('[aria-label="Remove old.md"]')).toBeNull();
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(container.querySelector('.composer-upload-status')?.textContent).toContain('new.md');
    expect(button('Send message').disabled).toBe(true);
    await act(async () => { next.resolve(response(attachment('new.md'), 201)); });
    await click(button('Send message'));
    expect(messageRequests[0].attachment_paths).toEqual(['research/uploads/new.md']);
  });

  it('aborts a pending upload on new chat and ignores a late response', async () => {
    const pending = deferred<Response>();
    upload.mockReturnValueOnce(pending.promise);
    await mount();
    await selectFiles([new File(['notes'], 'notes.md')]);
    const signal = upload.mock.calls[0][1]!;
    await click(container.querySelector<HTMLButtonElement>('.new-chat')!);
    expect(signal.aborted).toBe(true);
    await act(async () => { pending.resolve(response(attachment(), 201)); });
    expect(container.querySelector('[aria-label="Attached files"]')).toBeNull();
    expect(container.querySelector('.composer-upload-status')).toBeNull();
    expect(button('Attach file').disabled).toBe(false);
  });

  it('rejects unsupported formats and over-capacity selections before upload', async () => {
    await mount();
    await type('Explain this');
    await selectFiles([new File(['code'], 'script.py')]);
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('choose a .md, .txt, or .pdf');
    expect(upload).not.toHaveBeenCalled();
    const files = Array.from({ length: 11 }, (_, index) => new File(['notes'], `${index}.txt`));
    await selectFiles(files);
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('up to 10 files');
    expect(upload).not.toHaveBeenCalled();
    expect(container.querySelector('textarea')!.value).toBe('Explain this');
    expect(messageRequests).toEqual([]);

    await selectFiles(files.slice(0, 10));
    expect(upload).toHaveBeenCalledTimes(10);
    expect(container.querySelectorAll('.composer-attachment')).toHaveLength(10);
    expect(button('Attach file').disabled).toBe(true);
    await click(button('Remove 0.txt'));
    expect(button('Attach file').disabled).toBe(false);
  });
});
