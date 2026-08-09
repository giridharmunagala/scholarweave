// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';

/**
 * Long research turns emit several assistant messages and a burst of tool calls, and later
 * turns often use no tools at all. This drives the real ChatPage through that shape to make
 * sure reasoning cards and tool activity stay attached to their own turn instead of being
 * wiped out by whatever the newest run happens to look like.
 */

const CONVERSATION_ID = 'conv-1';

interface Turn {
  input: string;
  toolNames: string[];
  replies: string[];
}

const TURNS: Turn[] = [
  { input: 'Survey retrieval augmented generation', toolNames: ['search_papers', 'fetch_paper', 'summarise'], replies: ['Searching the corpus now.', 'Here is a long synthesis. '.repeat(40)] },
  { input: 'Compare the two strongest baselines', toolNames: ['search_papers', 'summarise'], replies: ['Pulling both papers.', 'Baseline comparison. '.repeat(40)] },
  { input: 'Which datasets do they share?', toolNames: ['search_papers', 'fetch_paper'], replies: ['Checking dataset tables.', 'Shared datasets. '.repeat(40)] },
  { input: 'Summarise that in two sentences', toolNames: [], replies: ['Two sentence summary. '.repeat(20)] },
  { input: 'Now write it as a paragraph', toolNames: [], replies: ['Paragraph version. '.repeat(20)] },
];

/** Build the persisted events/items the backend would store for one turn. */
function buildTurn(turn: Turn, runId: string) {
  const events: Record<string, unknown>[] = [];
  const items: Record<string, unknown>[] = [];
  let sequence = 0;
  const push = (event_type: string, payload: Record<string, unknown>) => {
    sequence += 1;
    events.push({
      id: `${runId}-e${sequence}`,
      run_id: runId,
      sequence,
      event_type,
      payload,
      created_at: new Date().toISOString(),
    });
  };

  let reasoning = '';
  turn.replies.forEach((reply, replyIndex) => {
    reasoning += `Step ${replyIndex + 1}: weighing the evidence. `;
    push('model.stream', {
      raw_type: 'response.reasoning_text.delta',
      delta: reasoning,
      snapshot: true,
    });
    const toolName = turn.toolNames[replyIndex];
    if (toolName) {
      push('tool.started', { tool_name: toolName });
      push('tool.completed', { tool_name: toolName });
      items.push({
        id: `${runId}-t${replyIndex}`,
        run_id: runId,
        type: 'tool_call_item',
        role: null,
        title: toolName,
        description: `called ${toolName}`,
        text: null,
        payload: {},
        created_at: new Date().toISOString(),
      });
      items.push({
        id: `${runId}-o${replyIndex}`,
        run_id: runId,
        type: 'tool_call_output_item',
        role: null,
        title: `${toolName} result`,
        description: null,
        text: 'ok',
        payload: {},
        created_at: new Date().toISOString(),
      });
    }
    push('model.stream', { raw_type: 'response.output_text.delta', delta: reply, snapshot: true });
    items.push({
      id: `${runId}-m${replyIndex}`,
      run_id: runId,
      type: 'message_output_item',
      role: 'assistant',
      title: null,
      description: null,
      text: reply,
      payload: {},
      created_at: new Date().toISOString(),
    });
  });
  // Tools beyond the reply count still belong to the turn, mirroring multi-call research runs.
  turn.toolNames.slice(turn.replies.length).forEach((toolName, offset) => {
    push('tool.started', { tool_name: toolName });
    push('tool.completed', { tool_name: toolName });
    items.push({
      id: `${runId}-x${offset}`,
      run_id: runId,
      type: 'tool_call_item',
      role: null,
      title: toolName,
      description: `called ${toolName}`,
      text: null,
      payload: {},
      created_at: new Date().toISOString(),
    });
  });

  const sessionItems = [
    { id: `${runId}-u`, type: 'message', role: 'user', text: turn.input, created_at: new Date().toISOString() },
    ...turn.replies.map((reply, index) => ({
      id: `${runId}-s${index}`,
      type: 'message',
      role: 'assistant',
      text: reply,
      created_at: new Date().toISOString(),
    })),
  ];

  return { events, items, sessionItems };
}

const BUILT = TURNS.map((turn, index) => buildTurn(turn, `run-${index + 1}`));

class FakeServer {
  sessionItems: Record<string, unknown>[] = [];
  runs: Record<string, any>[] = [];
  turnIndex = 0;
  listeners = new Map<string, FakeEventSource>();

  reset() {
    this.sessionItems = [];
    this.runs = [];
    this.turnIndex = 0;
    this.listeners.clear();
  }

  startRun(content: string) {
    const run: Record<string, any> = {
      id: `run-${this.turnIndex + 1}`,
      agent_revision_id: null,
      conversation_id: CONVERSATION_ID,
      agent_name: 'ScholarWeave autonomous agent',
      status: 'pending',
      input: content,
      final_output: null,
      last_agent_name: null,
      usage: {},
      error: null,
      cancel_requested: false,
      created_at: new Date(Date.now() + this.turnIndex * 1000).toISOString(),
      started_at: null,
      finished_at: null,
      items: [],
      events: [],
      interruptions: [],
    };
    this.runs.push(run);
    this.turnIndex += 1;
    return run;
  }

  /** Persist the run exactly like the backend does once the turn settles. */
  completeRun(runId: string, index: number) {
    const run = this.runs.find((candidate) => candidate.id === runId)!;
    run.status = 'completed';
    run.events = BUILT[index].events;
    run.items = BUILT[index].items;
    run.started_at = new Date().toISOString();
    run.finished_at = new Date().toISOString();
    this.sessionItems.push(...BUILT[index].sessionItems);
  }
}

const server = new FakeServer();

class FakeEventSource {
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  private handlers = new Map<string, ((event: MessageEvent<string>) => void)[]>();
  runId: string;

  constructor(url: string) {
    this.runId = /\/runs\/([^/]+)\/events/.exec(url)![1];
    server.listeners.set(this.runId, this);
  }

  addEventListener(type: string, handler: (event: MessageEvent<string>) => void) {
    const existing = this.handlers.get(type) ?? [];
    existing.push(handler);
    this.handlers.set(type, existing);
  }

  close() {
    server.listeners.delete(this.runId);
  }

  deliver(event: Record<string, unknown>) {
    const message = { data: JSON.stringify(event) } as MessageEvent<string>;
    const named = this.handlers.get(String(event.event_type));
    if (named?.length) {
      for (const handler of named) handler(message);
      return;
    }
    this.onmessage?.(message);
  }
}

const SETTINGS = { default_model_references: { chat: { provider_id: 'p1', model: 'gemini' } } };
const PROVIDERS = [
  {
    id: 'p1',
    name: 'Local',
    kind: 'openai_compatible',
    base_url: null,
    archived: false,
    models: [{ id: 'gemini', name: 'gemini', enabled: true }],
  },
];

function respond(body: unknown) {
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) } as Response);
}

function conversationSummary() {
  return {
    id: CONVERSATION_ID,
    title: 'Recorded chat',
    kind: 'autonomous',
    agent_revision_id: null,
    model_reference: SETTINGS.default_model_references.chat,
    session_policy: {},
    status: 'idle',
    last_message_preview: '',
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  };
}

function installFetch() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? 'GET';
      if (url.endsWith('/api/agent/conversations')) return respond([conversationSummary()]);
      if (url.endsWith('/api/providers')) return respond(PROVIDERS);
      if (url.endsWith('/api/settings')) return respond(SETTINGS);
      if (url.includes(`/api/agent/conversations/${CONVERSATION_ID}/messages`) && method === 'POST') {
        const { content } = JSON.parse(String(init?.body));
        return respond({ conversation: {}, run: server.startRun(content) });
      }
      if (url.endsWith(`/api/agent/conversations/${CONVERSATION_ID}`)) {
        return respond({ ...conversationSummary(), items: server.sessionItems });
      }
      if (url.includes('/api/runs?conversation_id=')) return respond(server.runs);
      const runMatch = /\/api\/runs\/([^/?]+)$/.exec(url);
      if (runMatch) return respond(server.runs.find((run) => run.id === runMatch[1]));
      throw new Error(`Unhandled request: ${method} ${url}`);
    }),
  );
}

async function flush(times = 6) {
  for (let index = 0; index < times; index += 1) {
    await act(async () => {
      await Promise.resolve();
      await new Promise((resolve) => setTimeout(resolve, 40));
    });
  }
}

describe('chat transcript detail', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    server.reset();
    installFetch();
    vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource);
    (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement('div');
    document.body.appendChild(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  const mount = async (ChatPage: () => JSX.Element) => {
    root = createRoot(container);
    await act(async () => {
      root.render(<ChatPage />);
    });
    await flush();
  };

  const send = async (text: string) => {
    const textarea = container.querySelector('textarea')!;
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype,
      'value',
    )!.set!;
    await act(async () => {
      setter.call(textarea, text);
      textarea.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await act(async () => {
      container
        .querySelector('.composer-send')!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });
    await flush(2);
  };

  it('keeps every turn reasoning card and tool chip as the thread grows', async () => {
    const { default: ChatPage } = await import('./ChatPage');
    await mount(ChatPage as () => JSX.Element);

    for (let turn = 0; turn < TURNS.length; turn += 1) {
      await send(TURNS[turn].input);

      const runId = `run-${turn + 1}`;
      const source = server.listeners.get(runId);
      for (const event of BUILT[turn].events) {
        await act(async () => {
          source?.deliver(event);
        });
      }
      server.completeRun(runId, turn);
      await act(async () => {
        server.listeners.get(runId)?.deliver({ sequence: 9999, event_type: 'run.completed', payload: {} });
      });
      await flush();

      // Every settled turn keeps a chip, because every turn reasoned even when it used no tools.
      expect(container.querySelectorAll('.turn-activity-chip')).toHaveLength(turn + 1);
      // Reasoning has moved to the panel, so nothing lingers inline once a turn is done.
      expect(container.querySelectorAll('.reasoning-live')).toHaveLength(0);
      expect(container.querySelector('.activity-toggle')).not.toBeNull();
    }

    // A reload must rebuild the same detail purely from persisted runs.
    act(() => root.unmount());
    await mount(ChatPage as () => JSX.Element);

    expect(container.querySelectorAll('.turn-activity-chip')).toHaveLength(TURNS.length);
    expect(container.querySelectorAll('.reasoning-live')).toHaveLength(0);
    expect(container.querySelector('.activity-toggle')).not.toBeNull();
  });

  it('shows reasoning inline while thinking and hands it to the panel when done', async () => {
    const { default: ChatPage } = await import('./ChatPage');
    await mount(ChatPage as () => JSX.Element);
    await send(TURNS[0].input);

    const source = server.listeners.get('run-1');
    for (const event of BUILT[0].events) {
      await act(async () => {
        source?.deliver(event);
      });
    }
    await flush(3);

    // Mid-run the reader watches the trace directly in the transcript.
    const liveTrace = container.querySelector('.reasoning-live');
    expect(liveTrace).not.toBeNull();
    expect(liveTrace!.textContent).toContain('Thinking');
    expect(liveTrace!.textContent).toContain('weighing the evidence');

    server.completeRun('run-1', 0);
    await act(async () => {
      source?.deliver({ sequence: 9999, event_type: 'run.completed', payload: {} });
    });
    await flush();

    // Once settled the transcript is answers only and the trace is behind the turn chip.
    expect(container.querySelector('.reasoning-live')).toBeNull();
    const chip = container.querySelector('.turn-activity-chip');
    expect(chip).not.toBeNull();
    expect(chip!.textContent).toContain('Reasoning');

    // The panel auto-opened for this turn, and it now carries the trace the transcript dropped.
    expect(container.querySelector('.tool-panel')!.textContent).toContain('weighing the evidence');

    // Clicking the chip of the turn already on screen toggles the panel shut, then back open.
    await act(async () => {
      chip!.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });
    await flush(1);
    expect(container.querySelector('.tool-panel')).toBeNull();

    await act(async () => {
      container
        .querySelector('.turn-activity-chip')!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });
    await flush(1);
    expect(container.querySelector('.tool-panel')!.textContent).toContain('weighing the evidence');
  });

  it('opens the panel for the turn whose chip was clicked', async () => {
    const { default: ChatPage } = await import('./ChatPage');
    await mount(ChatPage as () => JSX.Element);

    for (let turn = 0; turn < TURNS.length; turn += 1) {
      await send(TURNS[turn].input);
      server.completeRun(`run-${turn + 1}`, turn);
      await act(async () => {
        server.listeners
          .get(`run-${turn + 1}`)
          ?.deliver({ sequence: 9999, event_type: 'run.completed', payload: {} });
      });
      await flush();
    }

    const chips = container.querySelectorAll('.turn-activity-chip');
    expect(chips).toHaveLength(TURNS.length);
    await act(async () => {
      chips[0].dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });
    await flush(1);

    const panel = container.querySelector('.tool-panel');
    expect(panel).not.toBeNull();
    expect(panel!.textContent).toContain('Turn 1 details');
    expect(panel!.textContent).toContain('Search papers');
    // The finished trace now lives in the panel rather than the transcript.
    expect(panel!.textContent).toContain('Step 1: weighing the evidence.');
  });
});
