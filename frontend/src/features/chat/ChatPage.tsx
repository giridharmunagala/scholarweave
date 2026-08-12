import {
  Fragment,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { subscribeToRun, type RunStreamEvent } from '../../api/events';
import { Icon } from '../../shared/components/Icons';
import { MarkdownViewer } from '../../shared/components/MarkdownViewer';
import { EmptyState, ErrorNotice, Loading, StatusPill } from '../../shared/components/Ui';
import { providersApi, type Provider, type Settings } from '../providers/api';
import {
  chatApi,
  type Conversation,
  type ConversationDetail,
  type ModelReference,
  type Run,
} from './api';
import {
  ChatModelPicker,
  modelReferenceLabel,
  preferredChatModel,
} from './ChatModelPicker';
import {
  applyChatStreamEvent,
  emptyChatStream,
  restoreChatStream,
  type ChatStreamState,
} from './chatStream';
import {
  buildTurnTimeline,
  emptyTurnTimeline,
  type TimelineSource,
  type TurnTimeline,
} from './chatTimeline';
import { SourceChips, TurnTimelineView } from './TurnTimeline';
import './chat.css';

const SUGGESTIONS = [
  'Summarise the key findings across the papers in my library.',
  'Compare the methods used in the two most recent papers I added.',
  'What open research questions remain in this area?',
  'Draft research notes with citations for my current topic.',
];

export default function ChatPage() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [current, setCurrent] = useState<ConversationDetail | null>(null);
  const [modelReference, setModelReference] = useState<ModelReference>({});
  const [preferredModelReference, setPreferredModelReference] = useState<ModelReference>({});
  const [run, setRun] = useState<Run | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [stream, setStream] = useState<ChatStreamState>(emptyChatStream);
  const [optimisticUser, setOptimisticUser] = useState<string | null>(null);
  const [content, setContent] = useState('');
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [pinnedToBottom, setPinnedToBottom] = useState(true);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const messageListRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const openRequestRef = useRef(0);

  const refreshList = () => chatApi.list().then(setConversations);
  const open = async (id: string) => {
    const request = ++openRequestRef.current;
    setRun(null);
    setRuns([]);
    setStream(emptyChatStream);
    setOptimisticUser(null);
    setSending(false);
    setPinnedToBottom(true);
    const [conversationResult, runsResult] = await Promise.allSettled([
      chatApi.get(id),
      chatApi.runs(id),
    ]);
    if (request !== openRequestRef.current) return;
    if (conversationResult.status === 'rejected') throw conversationResult.reason;
    if (runsResult.status === 'rejected') setError(runsResult.reason);
    const conversation = conversationResult.value;
    const allRuns = runsResult.status === 'fulfilled' ? runsResult.value : [];
    const conversationRuns = runsForConversation(allRuns, id);
    const latestRun = conversationRuns[conversationRuns.length - 1] ?? null;
    setCurrent(conversation);
    setModelReference(conversation.model_reference);
    setRun(latestRun);
    setRuns(conversationRuns);
    setStream(latestRun ? displayStream(latestRun) : emptyChatStream);
    setSending(Boolean(latestRun && !isTerminalRun(latestRun)));
    setOptimisticUser(
      latestRun && !isTerminalRun(latestRun)
        ? missingRunInput(conversation, latestRun)
        : null,
    );
  };

  useEffect(() => {
    Promise.all([
      chatApi.list(),
      providersApi.list(),
      providersApi.settings(),
    ])
      .then(async ([items, nextProviders, nextSettings]) => {
        setConversations(items);
        setProviders(nextProviders);
        setSettings(nextSettings);
        const preferredModel = preferredChatModel(nextSettings);
        setPreferredModelReference(preferredModel);
        setModelReference(preferredModel);
        if (items[0]) await open(items[0].id);
      })
      .catch(setError)
      .finally(() => setLoading(false));
  }, []);

  useLayoutEffect(() => {
    const list = messageListRef.current;
    if (!list || !pinnedToBottom) return;
    list.scrollTop = list.scrollHeight;
  }, [
    current?.id,
    current?.items.length,
    optimisticUser,
    stream.reasoning,
    stream.assistant,
    stream.tools.length,
    run?.status,
    pinnedToBottom,
  ]);

  // Auto-grow the composer up to a bounded height so long prompts stay visible.
  useEffect(() => {
    const textarea = composerRef.current;
    if (!textarea) return;
    textarea.style.height = 'auto';
    textarea.style.height = `${Math.min(textarea.scrollHeight, 240)}px`;
  }, [content]);

  useEffect(() => {
    if (!run) return;

    let cancelled = false;
    let finalizing = false;
    let polling = false;
    let flushTimer: number | undefined;
    let pendingEvents: RunStreamEvent[] = [];
    const conversationId = run.conversation_id ?? current?.id ?? null;

    // Deltas arrive faster than the browser can paint, so they are coalesced into one
    // urgent update per frame budget rather than deferred, which would let long answers
    // starve the reasoning and tool indicators.
    const flushEvents = () => {
      flushTimer = undefined;
      if (!pendingEvents.length || cancelled) return;
      const events = pendingEvents;
      pendingEvents = [];
      setStream((currentStream) => events.reduce(applyChatStreamEvent, currentStream));
    };

    const queueEvent = (event: RunStreamEvent) => {
      pendingEvents.push(event);
      if (flushTimer === undefined) {
        flushTimer = window.setTimeout(flushEvents, 50);
      }
    };

    const finish = async (knownRun?: Run) => {
      if (finalizing || cancelled) return;
      finalizing = true;
      if (flushTimer !== undefined) {
        window.clearTimeout(flushTimer);
        flushTimer = undefined;
      }
      if (pendingEvents.length) {
        const events = pendingEvents;
        pendingEvents = [];
        setStream((currentStream) => events.reduce(applyChatStreamEvent, currentStream));
      }
      try {
        const [nextRun, nextConversation] = await Promise.all([
          knownRun ? Promise.resolve(knownRun) : chatApi.run(run.id),
          conversationId ? chatApi.get(conversationId) : Promise.resolve(null),
        ]);
        if (cancelled) return;
        setRun(nextRun);
        setRuns((previous) => mergeRun(previous, nextRun));
        if (nextConversation) setCurrent(nextConversation);
        setOptimisticUser(null);
        setStream(displayStream(nextRun));
        void refreshList();
      } catch (nextError) {
        if (!cancelled) setError(nextError);
      } finally {
        if (!cancelled) setSending(false);
      }
    };

    const inspectRun = async () => {
      if (polling || finalizing || cancelled) return;
      polling = true;
      try {
        const nextRun = await chatApi.run(run.id);
        if (isTerminalRun(nextRun)) await finish(nextRun);
      } catch (nextError) {
        if (!cancelled) setError(nextError);
      } finally {
        polling = false;
      }
    };

    if (isTerminalRun(run)) {
      if (sending) void finish(run);
      return () => {
        cancelled = true;
      };
    }

    const lastSequence = run.events.reduce((max, event) => Math.max(max, event.sequence), -1);
    const unsubscribe = subscribeToRun(
      run.id,
      lastSequence,
      (event) => {
        if (isTerminalEvent(event.event_type)) {
          void finish();
          return;
        }
        queueEvent(event);
      },
      () => void inspectRun(),
    );

    return () => {
      cancelled = true;
      unsubscribe();
      if (flushTimer !== undefined) window.clearTimeout(flushTimer);
    };
  }, [run?.id, run?.status, current?.id, sending]);

  const resetThread = () => {
    openRequestRef.current += 1;
    setRun(null);
    setRuns([]);
    setStream(emptyChatStream);
    setOptimisticUser(null);
    setSending(false);
    setPinnedToBottom(true);
  };

  const create = () => {
    setCurrent(null);
    setModelReference(preferredModelReference);
    resetThread();
    composerRef.current?.focus();
  };

  const selectModel = (reference: ModelReference) => {
    setModelReference(reference);
    setPreferredModelReference(reference);
    void providersApi
      .updateSettings({ last_chat_model_reference: reference })
      .then(setSettings)
      .catch(setError);
    if (current) {
      setCurrent(null);
      resetThread();
    }
  };

  const remove = async (id: string) => {
    try {
      await chatApi.remove(id);
      const remaining = await chatApi.list();
      setConversations(remaining);
      if (current?.id === id) {
        resetThread();
        if (remaining[0]) await open(remaining[0].id);
        else setCurrent(null);
      }
    } catch (nextError) {
      setError(nextError);
    }
  };

  const send = async (override?: string) => {
    const submitted = (override ?? content).trim();
    if (!submitted || sending) return;
    const request = openRequestRef.current;
    setSending(true);
    setError(null);
    setContent('');
    setOptimisticUser(submitted);
    setRun(null);
    setStream(emptyChatStream);
    setPinnedToBottom(true);
    try {
      let conversation = current;
      if (!conversation) {
        const created = await chatApi.create(submitted.slice(0, 50), modelReference);
        if (request !== openRequestRef.current) return;
        conversation = await chatApi.get(created.id);
        if (request !== openRequestRef.current) return;
        setCurrent(conversation);
        setModelReference(conversation.model_reference);
        void refreshList();
      }
      const response = await chatApi.send(conversation.id, submitted);
      if (request !== openRequestRef.current) return;
      setRun(response.run);
      setRuns((previous) => mergeRun(previous, response.run));
      setStream(displayStream(response.run));
      void refreshList();
    } catch (nextError) {
      if (request !== openRequestRef.current) return;
      setContent(submitted);
      setOptimisticUser(null);
      setSending(false);
      setError(nextError);
    }
  };

  const filtered = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    if (!needle) return conversations;
    return conversations.filter((conversation) =>
      `${conversation.title} ${conversation.last_message_preview}`
        .toLocaleLowerCase()
        .includes(needle),
    );
  }, [conversations, query]);

  // Every run keeps its own trace, so an older turn never borrows the newest turn's activity.
  const persistedTimelines = useMemo(() => {
    const timelines = new Map<string, TurnTimeline>();
    for (const candidate of runs) {
      timelines.set(
        candidate.id,
        buildTurnTimeline(candidate.events, { settled: isTerminalRun(candidate) }),
      );
    }
    return timelines;
  }, [runs]);

  // The active run is rebuilt from the events seen so far so the trace grows as it happens.
  const liveTimeline = useMemo(
    () => buildTurnTimeline(stream.events, { settled: Boolean(run && isTerminalRun(run)) }),
    [stream.events, run?.status],
  );

  const timelineFor = (candidate: Run): TurnTimeline => {
    const persisted = persistedTimelines.get(candidate.id) ?? emptyTurnTimeline;
    if (candidate.id !== run?.id) return persisted;
    return liveTimeline.steps.length >= persisted.steps.length ? liveTimeline : persisted;
  };

  // Every run is anchored, so a turn without tools still keeps its trace in place.
  const reasoningAnchors = useMemo(
    () => anchorRunsToItems(current?.items ?? [], runs),
    [current?.items, runs],
  );

  if (loading || !settings) return <Loading label="Loading agent conversations…" />;

  const activeModelLabel =
    modelReferenceLabel(modelReference, providers)
    || modelReferenceLabel(settings.default_model_references.chat ?? {}, providers)
    || 'No model configured';
  const hasTranscript = Boolean(current?.items.length || run || optimisticUser);

  // The active turn has no persisted user message yet, so its trace renders after the optimistic one.
  const pendingRun = run && !reasoningAnchors.anchored.has(run.id) ? run : null;
  const pendingTimeline = pendingRun ? timelineFor(pendingRun) : null;

  /** Re-asking is the only way to redo a turn, since the transcript itself is append-only. */
  const retry = (prompt: string) => {
    if (sending || !prompt.trim()) return;
    void send(prompt);
  };

  return (
    <div className="page page-wide chat-page">
      {error ? <ErrorNotice error={error} /> : null}
      <div className={`chat-layout${sidebarOpen ? '' : ' collapsed'}`}>
        <aside className="conversation-list panel" aria-label="Conversations">
          <div className="conversation-list-head">
            <div className="conversation-list-actions">
              <button className="button block new-chat" type="button" onClick={create}>
                <Icon name="plus" size={15} />
                New chat
              </button>
              <button
                type="button"
                className="button ghost icon chat-sidebar-toggle"
                aria-label={sidebarOpen ? 'Hide chat list' : 'Show chat list'}
                title={sidebarOpen ? 'Hide chat list' : 'Show chat list'}
                onClick={() => setSidebarOpen((value) => !value)}
              >
                <Icon name="sidebar" size={16} />
              </button>
            </div>
            <div className="conversation-search">
              <Icon name="search" size={14} />
              <input
                type="search"
                value={query}
                placeholder="Search chats…"
                aria-label="Search conversations"
                onChange={(event) => setQuery(event.target.value)}
              />
            </div>
          </div>
          <div className="conversation-scroll">
            {filtered.map((conversation) => (
              <div
                className={current?.id === conversation.id ? 'conversation active' : 'conversation'}
                key={conversation.id}
              >
                <button
                  type="button"
                  className="conversation-open"
                  onClick={() => {
                    void open(conversation.id).catch(setError);
                  }}
                >
                  <strong>{conversation.title}</strong>
                  <small>{conversation.last_message_preview || 'No messages yet'}</small>
                  <span className="conversation-time">{relativeTime(conversation.updated_at)}</span>
                </button>
                <button
                  type="button"
                  className="conversation-delete"
                  title="Delete chat"
                  aria-label={`Delete ${conversation.title}`}
                  disabled={sending}
                  onClick={() => void remove(conversation.id)}
                >
                  <Icon name="trash" size={14} />
                </button>
              </div>
            ))}
            {!filtered.length ? (
              <p className="conversation-empty">
                {conversations.length ? 'No chats match your search.' : 'No research chats yet.'}
              </p>
            ) : null}
          </div>
        </aside>
        <section className="chat-surface panel">
          <header className="chat-header">
            <div className="chat-header-inner">
              <div className="chat-header-title">
                <strong>{current?.title ?? 'New research chat'}</strong>
                <small>
                  <Icon name="sparkle" size={12} />
                  {activeModelLabel}
                </small>
              </div>
              <div className="chat-header-meta">
                {sending ? (
                  <span className="chat-working">
                    <span className="spinner tiny" aria-hidden="true" />
                    Working…
                  </span>
                ) : null}
                {run ? <StatusPill value={run.status} /> : null}
              </div>
            </div>
          </header>
          <div className="chat-workspace">
            <div className="chat-thread">
              <div
                className="message-list"
                ref={messageListRef}
                onScroll={(event) => {
                  const element = event.currentTarget;
                  const distance = element.scrollHeight - element.scrollTop - element.clientHeight;
                  setPinnedToBottom(distance < 80);
                }}
              >
                {!hasTranscript ? (
                  <div className="chat-welcome">
                    <EmptyState
                      icon="agents"
                      title="What should we research?"
                      description="Ask the agent to find evidence, compare papers, answer questions, identify open areas, or write research notes. It chooses and calls the tools it needs."
                    />
                    <div className="suggestion-grid">
                      {SUGGESTIONS.map((suggestion) => (
                        <button
                          type="button"
                          className="suggestion"
                          key={suggestion}
                          disabled={sending}
                          onClick={() => void send(suggestion)}
                        >
                          <Icon name="sparkle" size={14} />
                          <span>{suggestion}</span>
                        </button>
                      ))}
                    </div>
                  </div>
                ) : null}
                {current?.items.map((item, index) => {
                  if (!item.text?.trim()) return null;
                  const role = item.role ?? item.type;
                  const turnRun = reasoningAnchors.byIndex.get(index) ?? null;
                  const responseRun = reasoningAnchors.responseByIndex.get(index) ?? null;
                  // The trace sits between the question and the answer, where it happened.
                  const timeline = turnRun ? timelineFor(turnRun) : null;
                  return (
                    <Fragment key={index}>
                      <Message
                        role={role}
                        text={item.text}
                        metrics={responseRun ? turnMetrics(responseRun) : null}
                        sources={responseRun ? timelineFor(responseRun).sources : null}
                        onRetry={role === 'assistant' ? () => retry(promptFor(current.items, index)) : null}
                        canRetry={!sending}
                      />
                      {timeline ? <TurnTimelineView timeline={timeline} /> : null}
                    </Fragment>
                  );
                })}
                {optimisticUser ? (
                  <Message role="user" text={optimisticUser} className="optimistic" />
                ) : null}
                {pendingTimeline ? <TurnTimelineView timeline={pendingTimeline} /> : null}
                {stream.assistant ? (
                  <Message
                    role="assistant"
                    text={stream.assistant}
                    className="streaming"
                    streaming
                    metrics={run ? turnMetrics(run) : null}
                    sources={liveTimeline.sources}
                  />
                ) : null}
                {sending && !stream.assistant && !stream.reasoning && !liveTimeline.steps.length ? (
                  <div className="thinking-bubble" role="status" aria-label="The agent is thinking">
                    <span /> <span /> <span />
                  </div>
                ) : null}
                {run?.error ? <div className="notice error chat-run-error">{run.error}</div> : null}
              </div>
              {!pinnedToBottom && hasTranscript ? (
                <button
                  type="button"
                  className="scroll-to-bottom"
                  onClick={() => {
                    setPinnedToBottom(true);
                    messageListRef.current?.scrollTo({
                      top: messageListRef.current.scrollHeight,
                      behavior: 'smooth',
                    });
                  }}
                >
                  <Icon name="arrowRight" size={14} />
                  Jump to latest
                </button>
              ) : null}
              <div className="composer">
                <div className="composer-inner">
                  <div className="composer-box">
                    <textarea
                      ref={composerRef}
                      rows={1}
                      disabled={sending}
                      value={content}
                      onChange={(event) => setContent(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter' && !event.shiftKey) {
                          event.preventDefault();
                          void send();
                        }
                      }}
                      placeholder="Ask for a research outcome…"
                    />
                    <button
                      className="button icon composer-send"
                      type="button"
                      aria-label="Send message"
                      disabled={sending || !content.trim()}
                      onClick={() => void send()}
                    >
                      {sending
                        ? <span className="spinner tiny" aria-hidden="true" />
                        : <Icon name="arrowRight" size={16} />}
                    </button>
                  </div>
                  <div className="composer-footer">
                    <div className="composer-model">
                      <span className="composer-model-label">
                        <Icon name="sparkle" size={13} />
                        Model
                      </span>
                      <ChatModelPicker
                        providers={providers}
                        settings={settings}
                        value={modelReference}
                        disabled={sending}
                        onChange={selectModel}
                      />
                    </div>
                    <span className="composer-hint">
                      <kbd>Enter</kbd> send · <kbd>Shift</kbd>+<kbd>Enter</kbd> newline
                    </span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}

function Message({
  role,
  text,
  className = '',
  streaming = false,
  metrics = null,
  sources = null,
  onRetry = null,
  canRetry = true,
}: {
  role: string;
  text: string;
  className?: string;
  streaming?: boolean;
  metrics?: TurnMetrics | null;
  sources?: TimelineSource[] | null;
  onRetry?: (() => void) | null;
  canRetry?: boolean;
}) {
  const isAssistant = role === 'assistant';
  return (
    <article className={`message role-${role} ${className}`.trim()}>
      <span className="message-avatar" aria-hidden="true">
        <Icon name={role === 'user' ? 'user' : 'sparkle'} size={14} />
      </span>
      <div className="message-body">
        <MessageHeader
          label={streaming ? `${role} · streaming` : role}
          content={text}
          showCopy={!isAssistant}
        />
        <MarkdownViewer content={text} />
        {streaming ? <i className="stream-cursor" aria-hidden="true" /> : null}
        {isAssistant && sources?.length ? <SourceChips sources={sources} /> : null}
        {isAssistant ? (
          <MessageActions
            content={text}
            metrics={metrics}
            onRetry={onRetry}
            canRetry={canRetry}
          />
        ) : null}
      </div>
    </article>
  );
}

/** The answer's footer: what you can do with it, and what it cost, on one line. */
function MessageActions({
  content,
  metrics,
  onRetry,
  canRetry,
}: {
  content: string;
  metrics: TurnMetrics | null;
  onRetry: (() => void) | null;
  canRetry: boolean;
}) {
  const [copyStatus, setCopyStatus] = useState<'idle' | 'copied' | 'failed'>('idle');
  const [speaking, setSpeaking] = useState(false);
  const speech = typeof window !== 'undefined' ? window.speechSynthesis : undefined;

  useEffect(() => () => speech?.cancel(), [speech]);

  const copy = async () => {
    try {
      await copyToClipboard(content);
      setCopyStatus('copied');
      window.setTimeout(() => setCopyStatus('idle'), 1800);
    } catch {
      setCopyStatus('failed');
    }
  };

  const speak = () => {
    if (!speech) return;
    if (speaking) {
      speech.cancel();
      setSpeaking(false);
      return;
    }
    const utterance = new SpeechSynthesisUtterance(content.slice(0, 4000));
    utterance.onend = () => setSpeaking(false);
    utterance.onerror = () => setSpeaking(false);
    speech.cancel();
    speech.speak(utterance);
    setSpeaking(true);
  };

  return (
    <div className="message-actions">
      <button
        type="button"
        className={copyStatus === 'failed' ? 'failed' : ''}
        title={copyStatus === 'copied' ? 'Copied' : 'Copy answer'}
        aria-label={copyStatus === 'copied' ? 'Copied' : 'Copy answer'}
        onClick={() => void copy()}
      >
        <Icon name={copyStatus === 'copied' ? 'check' : 'copy'} size={14} />
      </button>
      {onRetry ? (
        <button
          type="button"
          title="Ask again"
          aria-label="Ask again"
          disabled={!canRetry}
          onClick={onRetry}
        >
          <Icon name="refresh" size={14} />
        </button>
      ) : null}
      {speech ? (
        <button
          type="button"
          className={speaking ? 'active' : ''}
          title={speaking ? 'Stop reading' : 'Read aloud'}
          aria-label={speaking ? 'Stop reading' : 'Read aloud'}
          onClick={speak}
        >
          <Icon name={speaking ? 'stop' : 'speaker'} size={14} />
        </button>
      ) : null}
      {metrics ? <TurnMetadata metrics={metrics} /> : null}
    </div>
  );
}

function isTerminalRun(run: Run): boolean {
  return ['completed', 'failed', 'cancelled', 'paused'].includes(run.status);
}

function isTerminalEvent(eventType: string): boolean {
  return ['run.completed', 'run.failed', 'run.cancelled', 'run.paused'].includes(eventType);
}

function displayStream(run: Run): ChatStreamState {
  const restored = restoreChatStream(run.events);
  return run.status === 'completed' ? { ...restored, assistant: '' } : restored;
}

function runsForConversation(runs: Run[], conversationId: string): Run[] {
  return runs
    .filter((candidate) => candidate.conversation_id === conversationId)
    .sort(
      (left, right) =>
        new Date(left.created_at).getTime() - new Date(right.created_at).getTime(),
    );
}

function mergeRun(runs: Run[], next: Run): Run[] {
  const index = runs.findIndex((candidate) => candidate.id === next.id);
  if (index < 0) return [...runs, next];
  return runs.map((candidate, candidateIndex) => (candidateIndex === index ? next : candidate));
}

function missingRunInput(conversation: ConversationDetail, run: Run): string | null {
  if (typeof run.input !== 'string') return null;
  const latestUserMessage = [...conversation.items]
    .reverse()
    .find((item) => item.role === 'user' && item.text);
  return latestUserMessage?.text === run.input ? null : run.input;
}

/** Retrying an answer means re-asking the question that produced it. */
function promptFor(items: ConversationDetail['items'], index: number): string {
  for (let cursor = index - 1; cursor >= 0; cursor -= 1) {
    const item = items[cursor];
    if ((item.role ?? item.type) === 'user' && item.text?.trim()) return item.text;
  }
  return '';
}

interface ReasoningAnchors {
  byIndex: Map<number, Run>;
  responseByIndex: Map<number, Run>;
  anchored: Set<string>;
}

// Pair each run with the user turn that started it so historical turns keep their own detail.
export function anchorRunsToItems(items: ConversationDetail['items'], runs: Run[]): ReasoningAnchors {
  const byIndex = new Map<number, Run>();
  const responseByIndex = new Map<number, Run>();
  const anchored = new Set<string>();
  const pending = [...runs];
  const unmatchedUserIndices: number[] = [];
  items.forEach((item, index) => {
    if (!item.text || (item.role ?? item.type) !== 'user') return;
    const matched = pending.findIndex(
      (candidate) => typeof candidate.input === 'string' && candidate.input === item.text,
    );
    if (matched < 0) {
      unmatchedUserIndices.push(index);
      return;
    }
    const [runForItem] = pending.splice(matched, 1);
    byIndex.set(index, runForItem);
    anchored.add(runForItem.id);
  });

  for (const index of unmatchedUserIndices) {
    const runForItem = pending.shift();
    if (!runForItem) break;
    byIndex.set(index, runForItem);
    anchored.add(runForItem.id);
  }

  // A turn can emit several assistant messages, so usage belongs on its final answer.
  for (const [userIndex, runForItem] of byIndex) {
    let lastAssistantIndex = -1;
    for (let index = userIndex + 1; index < items.length; index += 1) {
      const role = items[index].role ?? items[index].type;
      if (role === 'user') break;
      if (items[index].text && role === 'assistant') lastAssistantIndex = index;
    }
    if (lastAssistantIndex >= 0) responseByIndex.set(lastAssistantIndex, runForItem);
  }
  return { byIndex, responseByIndex, anchored };
}

interface TurnMetrics {
  inputTokens: number | null;
  outputTokens: number | null;
  inputEstimated: boolean;
  outputEstimated: boolean;
  promptRate: number | null;
  generationRate: number | null;
  durationSeconds: number | null;
}

export function turnMetrics(run: Run): TurnMetrics | null {
  const usage = run.usage as Record<string, unknown>;
  const performance = isRecord(usage.performance) ? usage.performance : null;
  const inputTokens = performance
    ? metricNumber(performance.input_tokens)
    : positiveMetricNumber(usage.input_tokens);
  const outputTokens = performance
    ? metricNumber(performance.output_tokens)
    : positiveMetricNumber(usage.output_tokens);
  const durationSeconds = elapsedSeconds(run.started_at, run.finished_at);
  if (inputTokens == null && outputTokens == null && durationSeconds == null) return null;
  return {
    inputTokens,
    outputTokens,
    inputEstimated: performance?.input_tokens_estimated === true,
    outputEstimated: performance?.output_tokens_estimated === true,
    promptRate: metricNumber(performance?.prompt_tokens_per_second),
    generationRate: metricNumber(performance?.generation_tokens_per_second),
    durationSeconds,
  };
}

function TurnMetadata({ metrics }: { metrics: TurnMetrics }) {
  const inputPrefix = metrics.inputEstimated ? '~' : '';
  const outputPrefix = metrics.outputEstimated ? '~' : '';
  const estimated = metrics.inputEstimated || metrics.outputEstimated;
  const details = [
    metrics.inputTokens != null ? `${inputPrefix}${formatMetric(metrics.inputTokens)} in` : null,
    metrics.outputTokens != null ? `${outputPrefix}${formatMetric(metrics.outputTokens)} out` : null,
    metrics.promptRate != null
      ? `${metrics.inputEstimated ? '~' : ''}${formatRate(metrics.promptRate)} prompt`
      : null,
    metrics.generationRate != null
      ? `${metrics.outputEstimated ? '~' : ''}${formatRate(metrics.generationRate)} generation`
      : null,
    metrics.durationSeconds != null ? `${formatDuration(metrics.durationSeconds)} total` : null,
  ].filter((value): value is string => value !== null);
  return (
    <div
      className="turn-metadata"
      title={estimated ? 'Token counts and rates prefixed with ~ are estimates.' : 'Turn usage and speed.'}
      aria-label={`Turn metadata: ${details.join(', ')}`}
    >
      {details.map((detail) => <span key={detail}>{detail}</span>)}
    </div>
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function metricNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
}

function positiveMetricNumber(value: unknown): number | null {
  const metric = metricNumber(value);
  return metric != null && metric > 0 ? metric : null;
}

function elapsedSeconds(startedAt: string | null, finishedAt: string | null): number | null {
  if (!startedAt || !finishedAt) return null;
  const elapsed = (new Date(finishedAt).getTime() - new Date(startedAt).getTime()) / 1000;
  return Number.isFinite(elapsed) && elapsed >= 0 ? elapsed : null;
}

function formatMetric(value: number): string {
  return new Intl.NumberFormat(undefined, {
    notation: value >= 1000 ? 'compact' : 'standard',
    maximumFractionDigits: value >= 1000 ? 1 : 0,
  }).format(value);
}

function formatRate(value: number): string {
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} tok/s`;
}

function formatDuration(value: number): string {
  if (value < 1) return `${Math.round(value * 1000)}ms`;
  if (value < 60) return `${value.toFixed(value < 10 ? 1 : 0)}s`;
  return `${Math.floor(value / 60)}m ${Math.round(value % 60)}s`;
}

export function relativeTime(value: string): string {
  const timestamp = new Date(value).getTime();
  if (Number.isNaN(timestamp)) return '';
  const seconds = Math.round((Date.now() - timestamp) / 1000);
  if (seconds < 60) return 'now';
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  if (seconds < 604800) return `${Math.floor(seconds / 86400)}d`;
  return new Date(timestamp).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

function MessageHeader({
  label,
  content,
  showCopy = true,
}: {
  label: string;
  content: string;
  showCopy?: boolean;
}) {
  const [copyStatus, setCopyStatus] = useState<'idle' | 'copied' | 'failed'>('idle');

  const copy = async () => {
    try {
      await copyToClipboard(content);
      setCopyStatus('copied');
      window.setTimeout(() => setCopyStatus('idle'), 1800);
    } catch {
      setCopyStatus('failed');
    }
  };

  const statusLabel = copyStatus === 'copied' ? 'Copied' : copyStatus === 'failed' ? 'Copy failed' : 'Copy message';
  return (
    <div className="message-header">
      <span>{label}</span>
      {showCopy ? (
        <button
          type="button"
          className={`message-copy${copyStatus === 'failed' ? ' failed' : ''}`}
          aria-label={statusLabel}
          title={statusLabel}
          onClick={() => void copy()}
        >
          <Icon name={copyStatus === 'copied' ? 'check' : 'copy'} size={14} />
          <span>{copyStatus === 'idle' ? 'Copy' : statusLabel}</span>
        </button>
      ) : null}
    </div>
  );
}

async function copyToClipboard(content: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(content);
    return;
  }

  const textarea = document.createElement('textarea');
  textarea.value = content;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand('copy');
  textarea.remove();
  if (!copied) throw new Error('The browser denied clipboard access.');
}
