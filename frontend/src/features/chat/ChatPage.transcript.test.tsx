// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';

/**
 * Long research turns emit several assistant messages and a burst of tool calls, and later
 * turns often use no tools at all. This drives the real ChatPage through that shape to make
 * sure the inline trace and its sources stay attached to their own turn instead of being
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
  let clock = Date.parse('2024-05-01T10:00:00.000Z');
  const push = (event_type: string, payload: Record<string, unknown>) => {
    sequence += 1;
    clock += 1500;
    events.push({
      id: `${runId}-e${sequence}`,
      run_id: runId,
      sequence,
      event_type,
      payload,
      created_at: new Date(clock).toISOString(),
    });
  };

  /** Emit the full call/output pair the runtime produces for one tool. */
  const emitTool = (toolName: string, key: string) => {
    const callId = `${runId}-${key}`;
    push('run.item', {
      name: 'tool_called',
      item: {
        type: 'tool_call_item',
        agent_name: 'ScholarWeave autonomous agent',
        title: toolName,
        description: `called ${toolName}`,
        raw_item: {
          name: toolName,
          call_id: callId,
          arguments: JSON.stringify({ query: 'retrieval augmented generation' }),
        },
      },
    });
    push('tool.started', { tool_name: toolName });
    push('tool.completed', { tool_name: toolName });
    push('run.item', {
      name: 'tool_output',
      item: {
        type: 'tool_call_output_item',
        agent_name: 'ScholarWeave autonomous agent',
        raw_item: { call_id: callId },
        output: {
          results: [
            {
              title: 'Dense retrieval survey',
              url: 'https://arxiv.org/abs/2401.00001',
              image_url: 'https://images.example.test/dense-retrieval.jpg',
            },
            { title: 'RAG benchmarks', url: 'https://openreview.net/forum?id=abc' },
          ],
        },
      },
    });
    items.push({
      id: `${runId}-t${key}`,
      run_id: runId,
      type: 'tool_call_item',
      role: null,
      title: toolName,
      description: `called ${toolName}`,
      text: null,
      payload: {},
      created_at: new Date(clock).toISOString(),
    });
    items.push({
      id: `${runId}-o${key}`,
      run_id: runId,
      type: 'tool_call_output_item',
      role: null,
      title: `${toolName} result`,
      description: null,
      text: 'ok',
      payload: {},
      created_at: new Date(clock).toISOString(),
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
    if (toolName) emitTool(toolName, String(replyIndex));
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
      created_at: new Date(clock).toISOString(),
    });
  });
  // Tools beyond the reply count still belong to the turn, mirroring multi-call research runs.
  turn.toolNames.slice(turn.replies.length).forEach((toolName, offset) => {
    emitTool(toolName, `x${offset}`);
  });

  const sessionItems = [
    { id: `${runId}-u`, type: 'message', role: 'user', text: turn.input, created_at: new Date(clock).toISOString() },
    ...turn.replies.map((reply, index) => ({
      id: `${runId}-s${index}`,
      type: 'message',
      role: 'assistant',
      text: reply,
      created_at: new Date(clock).toISOString(),
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
  failNextStopAndAnswer = false;

  reset() {
    this.sessionItems = [];
    this.runs = [];
    this.turnIndex = 0;
    this.listeners.clear();
    this.failNextStopAndAnswer = false;
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
    const runIndex = this.runs.findIndex((candidate) => candidate.id === runId);
    this.runs[runIndex] = {
      ...this.runs[runIndex],
      status: 'completed',
      events: BUILT[index].events,
      items: BUILT[index].items,
      started_at: new Date().toISOString(),
      finished_at: new Date().toISOString(),
    };
    this.sessionItems.push(...BUILT[index].sessionItems);
  }

  cancelRun(runId: string) {
    const index = this.runs.findIndex((candidate) => candidate.id === runId);
    const run = {
      ...this.runs[index],
      status: 'cancelled',
      cancel_requested: true,
      finished_at: new Date().toISOString(),
    };
    this.runs[index] = run;
    return run;
  }

  stopAndAnswer(runId: string) {
    const stoppedRun = this.cancelRun(runId);
    const answerRun = this.startRun('Answer from available information');
    answerRun.input = [
      { role: 'user', content: 'Answer from available information' },
    ];
    return { stopped_run: stoppedRun, answer_run: answerRun };
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
      if (url.endsWith('/api/providers/speech/builtin/status')) {
        return respond({
          state: 'ready',
          available: true,
          installed: true,
          running: false,
          model: 'nvidia/nemotron-speech-streaming-en-0.6b',
          downloaded_bytes: 0,
          total_bytes: 463_945_051,
          error: null,
        });
      }
      if (url.includes(`/api/agent/conversations/${CONVERSATION_ID}/messages`) && method === 'POST') {
        const { content } = JSON.parse(String(init?.body));
        return respond({ conversation: {}, run: server.startRun(content) });
      }
      if (url.endsWith(`/api/agent/conversations/${CONVERSATION_ID}`)) {
        return respond({ ...conversationSummary(), items: server.sessionItems });
      }
      if (url.includes('/api/runs?conversation_id=')) return respond(server.runs);
      const stopAndAnswerMatch = /\/api\/runs\/([^/?]+)\/stop-and-answer$/.exec(url);
      if (stopAndAnswerMatch && method === 'POST') {
        if (server.failNextStopAndAnswer) {
          server.failNextStopAndAnswer = false;
          throw new Error('Stop request failed');
        }
        const response = server.stopAndAnswer(stopAndAnswerMatch[1]);
        server.listeners.get(stopAndAnswerMatch[1])?.deliver({
          sequence: 9999,
          event_type: 'run.cancelled',
          payload: {},
        });
        return respond(response);
      }
      const cancelMatch = /\/api\/runs\/([^/?]+)\/cancel$/.exec(url);
      if (cancelMatch && method === 'POST') return respond(server.cancelRun(cancelMatch[1]));
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

function text(node: Element | null) {
  return node?.textContent ?? '';
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

  const send = async (value: string) => {
    const textarea = container.querySelector('textarea')!;
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype,
      'value',
    )!.set!;
    await act(async () => {
      setter.call(textarea, value);
      textarea.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await act(async () => {
      container
        .querySelector('.composer-send')!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });
    await flush(2);
  };

  /** Play one turn end to end: stream its events, then persist and close the run. */
  const runTurn = async (turn: number, { stream = true } = {}) => {
    await send(TURNS[turn].input);
    const runId = `run-${turn + 1}`;
    const source = server.listeners.get(runId);
    if (stream) {
      for (const event of BUILT[turn].events) {
        await act(async () => {
          source?.deliver(event);
        });
      }
    }
    server.completeRun(runId, turn);
    await act(async () => {
      server.listeners.get(runId)?.deliver({ sequence: 9999, event_type: 'run.completed', payload: {} });
    });
    await flush();
  };

  it('keeps every turn trace inline as the thread grows', async () => {
    const { default: ChatPage } = await import('./ChatPage');
    await mount(ChatPage as () => JSX.Element);

    for (let turn = 0; turn < TURNS.length; turn += 1) {
      await runTurn(turn);

      // Every settled turn keeps its own trace, because every turn reasoned even without tools.
      expect(container.querySelectorAll('.turn-timeline')).toHaveLength(turn + 1);
      // Traces collapse once the turn is done, so the transcript stays answer-first.
      expect(container.querySelectorAll('.timeline-detail')).toHaveLength(0);
      expect(container.querySelectorAll('.timeline-row.live')).toHaveLength(0);
    }

    const traces = container.querySelectorAll('.turn-timeline');
    // The tool-heavy first turn shows its calls; the tool-free last turn shows thinking only.
    expect(text(traces[0])).toContain('Searched');
    expect(text(traces[0])).toContain('tool calls');
    expect(text(traces[TURNS.length - 1])).toContain('Thought for');
    expect(text(traces[TURNS.length - 1])).not.toContain('Used tool');

    // A reload must rebuild the same detail purely from persisted runs.
    act(() => root.unmount());
    await mount(ChatPage as () => JSX.Element);

    expect(container.querySelectorAll('.turn-timeline')).toHaveLength(TURNS.length);
    expect(container.querySelectorAll('.timeline-detail')).toHaveLength(0);
    expect(text(container.querySelectorAll('.turn-timeline')[0])).toContain('Searched');
  });

  it('streams the trace live and keeps it expandable once settled', async () => {
    const { default: ChatPage } = await import('./ChatPage');
    await mount(ChatPage as () => JSX.Element);
    await send(TURNS[0].input);

    const source = server.listeners.get('run-1');
    // Watch the very first reasoning burst, before any tool has interrupted it.
    await act(async () => {
      source?.deliver(BUILT[0].events[0]);
    });
    await flush(2);

    // Mid-run the reader watches thinking unfold in place, already expanded.
    const live = container.querySelector('.timeline-row.live');
    expect(live).not.toBeNull();
    expect(text(live)).toContain('Thinking');
    expect(text(container.querySelector('.timeline-detail.reasoning'))).toContain('weighing the evidence');

    for (const event of BUILT[0].events.slice(1)) {
      await act(async () => {
        source?.deliver(event);
      });
    }
    await flush(3);

    server.completeRun('run-1', 0);
    await act(async () => {
      source?.deliver({ sequence: 9999, event_type: 'run.completed', payload: {} });
    });
    await flush();

    // Settled turns collapse to one-line summaries that carry their own duration.
    expect(container.querySelector('.timeline-row.live')).toBeNull();
    expect(container.querySelector('.timeline-detail')).toBeNull();
    const rows = container.querySelectorAll('.turn-timeline .timeline-row');
    expect(rows.length).toBeGreaterThan(1);
    expect(text(rows[0])).toMatch(/Thought for \d/);

    // The trace is still there on demand, reasoning text and all.
    await act(async () => {
      rows[0].querySelector('.timeline-head')!.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });
    await flush(1);
    expect(text(container.querySelector('.timeline-detail.reasoning'))).toContain('Step 1: weighing the evidence.');
  });

  it('stops an active run immediately', async () => {
    const { default: ChatPage } = await import('./ChatPage');
    await mount(ChatPage as () => JSX.Element);
    await send(TURNS[0].input);

    const stop = container.querySelector('[aria-label="Stop current run"]');
    expect(stop).not.toBeNull();
    expect(container.querySelector('[aria-label="Stop and answer with available information"]')).not.toBeNull();
    await act(async () => {
      stop!.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });
    await flush();

    expect(server.runs[0].status).toBe('cancelled');
    expect(container.querySelector('[aria-label="Stop current run"]')).toBeNull();
  });

  it('stops research and transitions to an available-information answer run', async () => {
    const { default: ChatPage } = await import('./ChatPage');
    await mount(ChatPage as () => JSX.Element);
    await send(TURNS[0].input);

    await act(async () => {
      container
        .querySelector('[aria-label="Stop and answer with available information"]')!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });
    await flush(2);

    expect(server.runs).toHaveLength(2);
    expect(server.runs[0].status).toBe('cancelled');
    expect(server.runs[1].status).toBe('pending');
    expect(container.querySelector('[aria-label="Stop current run"]')).not.toBeNull();
  });

  it('resumes tracking the active run when a stop request fails', async () => {
    const { default: ChatPage } = await import('./ChatPage');
    await mount(ChatPage as () => JSX.Element);
    await send(TURNS[0].input);
    server.failNextStopAndAnswer = true;

    await act(async () => {
      container
        .querySelector('[aria-label="Stop and answer with available information"]')!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });
    await flush(2);
    expect(container.querySelector('[aria-label="Stop current run"]')).not.toBeNull();

    server.completeRun('run-1', 0);
    await act(async () => {
      server.listeners.get('run-1')?.deliver({
        sequence: 9999,
        event_type: 'run.completed',
        payload: {},
      });
    });
    await flush();

    expect(container.querySelector('[aria-label="Stop current run"]')).toBeNull();
    expect(text(container)).not.toContain('Working');
  });

  it('names the tool it ran and exposes the sources it found', async () => {
    const { default: ChatPage } = await import('./ChatPage');
    await mount(ChatPage as () => JSX.Element);
    await runTurn(0);

    const toolRow = [...container.querySelectorAll('.timeline-row')].find((row) =>
      text(row).includes('Searched'),
    );
    expect(toolRow).toBeDefined();
    // The row states what was searched rather than the raw function name.
    expect(text(toolRow!)).toContain('retrieval augmented generation');

    await act(async () => {
      toolRow!.querySelector('.timeline-head')!.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });
    await flush(1);
    const detail = container.querySelector('.timeline-detail.tool');
    expect(text(detail)).toContain('Dense retrieval survey');

    // The answer itself lists the references the turn actually consulted.
    const answerSources = container.querySelector('.message.role-assistant .source-chips');
    expect(answerSources).not.toBeNull();
    const links = [...answerSources!.querySelectorAll('a')].map((link) => link.getAttribute('href'));
    expect(links).toContain('https://arxiv.org/abs/2401.00001');
    expect(links).toContain('https://openreview.net/forum?id=abc');
    const resultImage = container.querySelector('.message.role-assistant .source-image img');
    expect(resultImage?.getAttribute('src')).toBe('https://images.example.test/dense-retrieval.jpg');
    expect(resultImage?.getAttribute('alt')).toBe('Dense retrieval survey');
  });

  it('rebuilds the trace for turns whose live stream was never seen', async () => {
    const { default: ChatPage } = await import('./ChatPage');
    await mount(ChatPage as () => JSX.Element);

    for (let turn = 0; turn < TURNS.length; turn += 1) await runTurn(turn, { stream: false });

    const traces = container.querySelectorAll('.turn-timeline');
    expect(traces).toHaveLength(TURNS.length);
    expect(text(traces[0])).toContain('Searched');
    expect(text(traces[0])).toContain('Thought for');
  });
});
