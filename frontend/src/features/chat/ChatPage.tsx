import {
  Fragment,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { subscribeToRun, type RunStreamEvent } from '../../api/events';
import { Link } from '../../app/router';
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
import { ChatModelPicker, modelReferenceLabel } from './ChatModelPicker';
import {
  applyChatStreamEvent,
  emptyChatStream,
  reasoningFromEvents,
  restoreChatStream,
  type ChatStreamState,
} from './chatStream';
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
  const [activityOpen, setActivityOpen] = useState(false);
  const [activityRunId, setActivityRunId] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const messageListRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const autoOpenedActivityRunRef = useRef<string | null>(null);
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
    setActivityOpen(false);
    setActivityRunId(null);
    autoOpenedActivityRunRef.current = null;
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
        setModelReference(defaultBuilderModel(nextSettings));
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
    activityOpen,
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
    setActivityOpen(false);
    setActivityRunId(null);
    autoOpenedActivityRunRef.current = null;
  };

  const create = () => {
    setCurrent(null);
    if (settings) setModelReference(defaultBuilderModel(settings));
    resetThread();
    composerRef.current?.focus();
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

  const persistedActivityCount = run?.items.filter(isVisibleActivityItem).length ?? 0;
  const reasoningActive = run?.status === 'pending' || run?.status === 'running';

  // Reasoning and tool activity are stored per run, so every turn keeps its own detail.
  const persistedActivity = useMemo(() => {
    const activity = new Map<string, TurnActivity>();
    for (const candidate of runs) {
      const restored = restoreChatStream(candidate.events);
      activity.set(candidate.id, {
        tools: restored.tools,
        items: candidate.items.filter(isVisibleActivityItem),
        reasoning: restored.reasoning,
      });
    }
    return activity;
  }, [runs]);

  // Every run is anchored, so a turn without reasoning still keeps its tool activity in place.
  const reasoningAnchors = useMemo(
    () => anchorRunsToItems(current?.items ?? [], runs),
    [current?.items, runs],
  );

  const activityFor = (candidate: Run): TurnActivity => {
    const persisted = persistedActivity.get(candidate.id);
    const isCurrentRun = candidate.id === run?.id;
    const items = persisted?.items.length
      ? persisted.items
      : candidate.items.filter(isVisibleActivityItem);
    const live = isCurrentRun && stream.tools.length ? stream.tools : null;
    // A still-thinking turn shows its trace inline instead, so the panel would only duplicate it.
    const settled = persisted?.reasoning || (isCurrentRun ? stream.reasoning : '');
    return {
      tools: live ?? persisted?.tools ?? [],
      items,
      reasoning: isCurrentRun && reasoningActive ? '' : settled,
    };
  };

  // The panel keeps showing the newest turn that produced detail, so history never vanishes.
  const activityRun =
    (activityRunId ? runs.find((candidate) => candidate.id === activityRunId) : null)
    ?? [...runs].reverse().find((candidate) => hasTurnDetail(activityFor(candidate)))
    ?? null;
  const panelActivity = activityRun ? activityFor(activityRun) : null;
  const hasActivity = Boolean(panelActivity && hasTurnDetail(panelActivity));

  useEffect(() => {
    if (!run) return;
    const count = stream.tools.length || persistedActivityCount;
    if (!count || autoOpenedActivityRunRef.current === run.id) return;
    autoOpenedActivityRunRef.current = run.id;
    setActivityRunId(run.id);
    setActivityOpen(true);
  }, [run?.id, stream.tools.length, persistedActivityCount]);

  if (loading || !settings) return <Loading label="Loading agent conversations…" />;

  const activeModelLabel =
    modelReferenceLabel(modelReference, providers)
    || modelReferenceLabel(settings.default_model_references.chat ?? {}, providers)
    || 'No model configured';
  const hasTranscript = Boolean(current?.items.length || run || optimisticUser);

  // Reasoning is only shown inline while the agent is still thinking. Once the turn settles it
  // moves into the side panel next to that turn's tool calls, keeping the transcript to answers.
  const liveReasoning = reasoningActive ? stream.reasoning : '';

  /** Chips toggle the panel so a second click on the open turn closes it again. */
  const showTurnDetail = (runId: string | null) => {
    if (activityOpen && activityRun?.id === runId) {
      setActivityOpen(false);
      return;
    }
    setActivityRunId(runId);
    setActivityOpen(true);
  };

  // The active turn has no persisted user message yet, so its detail renders after the optimistic one.
  const pendingRun = run && !reasoningAnchors.anchored.has(run.id) ? run : null;
  const pendingActivity = pendingRun && !reasoningActive ? activityFor(pendingRun) : null;

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
                {hasActivity ? (
                  <button
                    type="button"
                    className={`activity-toggle${activityOpen ? ' active' : ''}`}
                    aria-expanded={activityOpen}
                    aria-controls="chat-tool-activity"
                    title={activityOpen ? 'Hide tool activity' : 'Show tool activity'}
                    onClick={() => setActivityOpen((value) => !value)}
                  >
                    <Icon name="tools" size={14} />
                    <span>Tools</span>
                    <strong>{activityCount(panelActivity!)}</strong>
                  </button>
                ) : null}
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
          <div className={`chat-workspace${activityOpen && hasActivity ? ' activity-open' : ''}`}>
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
                  const turnRun = reasoningAnchors.byIndex.get(index) ?? null;
                  const turnActivity = turnRun ? activityFor(turnRun) : null;
                  const responseRun = reasoningAnchors.responseByIndex.get(index) ?? null;
                  const showChip =
                    turnRun && turnActivity && hasTurnDetail(turnActivity)
                    && !(turnRun.id === run?.id && reasoningActive);
                  return (
                    <Fragment key={index}>
                      <Message
                        role={item.role ?? item.type}
                        text={item.text}
                        metrics={responseRun ? turnMetrics(responseRun) : null}
                      />
                      {showChip ? (
                        <TurnActivityChip
                          activity={turnActivity!}
                          active={activityOpen && activityRun?.id === turnRun!.id}
                          onOpen={() => showTurnDetail(turnRun!.id)}
                        />
                      ) : null}
                    </Fragment>
                  );
                })}
                {optimisticUser ? (
                  <Message role="user" text={optimisticUser} className="optimistic" />
                ) : null}
                {pendingActivity && hasTurnDetail(pendingActivity) ? (
                  <TurnActivityChip
                    activity={pendingActivity}
                    active={activityOpen && activityRun?.id === run?.id}
                    onOpen={() => showTurnDetail(run?.id ?? null)}
                  />
                ) : null}
                {liveReasoning ? <LiveReasoning content={liveReasoning} /> : null}
                {stream.assistant ? (
                  <Message
                    role="assistant"
                    text={stream.assistant}
                    className="streaming"
                    streaming
                    metrics={run ? turnMetrics(run) : null}
                  />
                ) : null}
                {sending && !stream.assistant && !stream.reasoning ? (
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
                        onChange={(reference) => {
                          setModelReference(reference);
                          if (current) {
                            setCurrent(null);
                            resetThread();
                          }
                        }}
                      />
                    </div>
                    <span className="composer-hint">
                      <kbd>Enter</kbd> send · <kbd>Shift</kbd>+<kbd>Enter</kbd> newline
                    </span>
                  </div>
                </div>
              </div>
            </div>
            {activityOpen && hasActivity && activityRun && panelActivity ? (
              <>
                <button
                  type="button"
                  className="tool-panel-scrim"
                  aria-label="Hide tool activity"
                  onClick={() => setActivityOpen(false)}
                />
                <ToolActivityPanel
                  run={activityRun}
                  activity={panelActivity}
                  turn={runTurnLabel(runs, activityRun)}
                  onClose={() => setActivityOpen(false)}
                />
              </>
            ) : null}
          </div>
        </section>
      </div>
    </div>
  );
}

/**
 * While a turn is running the reader watches the agent think, so the trace is plain and always
 * visible - no disclosure to collapse. Once the run settles this unmounts and the trace is
 * reachable from that turn's chip in the side panel.
 */
function LiveReasoning({ content }: { content: string }) {
  const bodyRef = useRef<HTMLDivElement>(null);
  const followTailRef = useRef(true);

  // Follow the streaming tail only while the reader is already at the bottom of the trace.
  useLayoutEffect(() => {
    const element = bodyRef.current;
    if (!element || !followTailRef.current) return;
    element.scrollTop = element.scrollHeight;
  }, [content]);

  return (
    <section className="reasoning-live" aria-label="Agent reasoning">
      <header className="reasoning-live-header">
        <span className="reasoning-card-icon">
          <span className="spinner tiny" aria-hidden="true" />
        </span>
        <span className="reasoning-card-label">
          <strong>Thinking</strong>
          <small>Moves to the turn details when this answer is done</small>
        </span>
        <span className="activity-live">Live</span>
      </header>
      <div
        className="reasoning-content"
        ref={bodyRef}
        tabIndex={0}
        role="region"
        aria-label="Reasoning trace"
        onScroll={(event) => {
          const element = event.currentTarget;
          followTailRef.current =
            element.scrollHeight - element.scrollTop - element.clientHeight < 24;
        }}
      >
        <MarkdownViewer content={content} />
        <i className="stream-cursor" aria-hidden="true" />
      </div>
    </section>
  );
}

function TurnActivityChip({
  activity,
  active,
  onOpen,
}: {
  activity: TurnActivity;
  active: boolean;
  onOpen: () => void;
}) {
  const count = activityCount(activity);
  const failed = activity.tools.some((tool) => tool.status === 'failed');
  const names = activity.tools.length
    ? activity.tools.map((tool) => humanize(tool.toolName))
    : activity.items
      .filter((item) => item.type === 'tool_call_item')
      .map((item) => item.title || item.description || 'Tool');
  const preview = Array.from(new Set(names)).slice(0, 3).join(' · ');
  const label = [
    activity.reasoning ? 'Reasoning' : '',
    count ? `${count} ${count === 1 ? 'tool step' : 'tool steps'}` : '',
  ].filter(Boolean).join(' · ');
  return (
    <button
      type="button"
      className={`turn-activity-chip${active ? ' active' : ''}${failed ? ' failed' : ''}`}
      aria-expanded={active}
      aria-controls="chat-tool-activity"
      onClick={onOpen}
    >
      <span className="activity-icon tool">
        <Icon name={count ? 'tools' : 'sparkle'} size={14} />
      </span>
      <span className="turn-activity-label">
        <strong>{label || 'Turn details'}</strong>
        <small>{preview || 'Review how the agent reached this answer'}</small>
      </span>
      <Icon className="activity-chevron" name="arrowRight" size={13} />
    </button>
  );
}

function ToolActivityPanel({
  run,
  activity,
  turn,
  onClose,
}: {
  run: Run;
  activity: TurnActivity;
  turn: string;
  onClose: () => void;
}) {
  const { tools, items, reasoning } = activity;
  const count = activityCount(activity);
  const scrollRef = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const element = scrollRef.current;
    if (element) element.scrollTop = 0;
  }, [run.id]);

  const summary = [
    runActivityTitle(run.status),
    reasoning ? 'reasoning' : '',
    count ? `${count} ${count === 1 ? 'tool step' : 'tool steps'}` : '',
  ].filter(Boolean).join(' · ');

  return (
    <aside className="tool-panel" id="chat-tool-activity" aria-label="Turn details">
      <header className="tool-panel-header">
        <span className="tool-panel-title-icon"><Icon name="tools" size={15} /></span>
        <span className="tool-panel-title">
          <strong>{turn}</strong>
          <small>{summary}</small>
        </span>
        <button
          type="button"
          className="button ghost icon"
          aria-label="Hide turn details"
          onClick={onClose}
        >
          <Icon name="close" size={15} />
        </button>
      </header>
      <div className="tool-panel-scroll" ref={scrollRef}>
        {reasoning ? (
          <section className="activity-section">
            <h3>Reasoning</h3>
            <div className="panel-reasoning">
              <MarkdownViewer content={reasoning} />
            </div>
          </section>
        ) : null}
        {tools.length ? (
          <section className="activity-section">
            <h3>Tool calls</h3>
            <div className="live-tools" aria-live="polite">
              {tools.map((tool) => (
                <div className="live-tool" key={tool.sequence}>
                  <span className="activity-icon tool"><Icon name="tools" size={14} /></span>
                  <span className="live-tool-body">
                    <strong>{humanize(tool.toolName)}</strong>
                    <small>
                      {tool.status === 'running'
                        ? 'Using tool…'
                        : tool.status === 'failed'
                          ? 'Tool failed; agent can recover'
                          : 'Tool finished'}
                    </small>
                  </span>
                  <span className={`tool-status ${tool.status}`}>
                    {tool.status === 'running'
                      ? <span className="spinner tiny" aria-hidden="true" />
                      : <Icon name={tool.status === 'failed' ? 'close' : 'check'} size={13} />}
                    {tool.status === 'running'
                      ? 'Running'
                      : tool.status === 'failed'
                        ? 'Failed'
                        : 'Done'}
                  </span>
                </div>
              ))}
            </div>
          </section>
        ) : null}
        {items.length ? (
          <section className="activity-section">
            <h3>{tools.length ? 'Run details' : 'Calls and results'}</h3>
            <div className="activity-items">
              {items.map((item, index) => <RunActivityItem item={item} key={index} />)}
            </div>
          </section>
        ) : null}
        {run.error ? <div className="notice error">{run.error}</div> : null}
      </div>
    </aside>
  );
}

function Message({
  role,
  text,
  className = '',
  streaming = false,
  metrics = null,
}: {
  role: string;
  text: string;
  className?: string;
  streaming?: boolean;
  metrics?: TurnMetrics | null;
}) {
  return (
    <article className={`message role-${role} ${className}`.trim()}>
      <span className="message-avatar" aria-hidden="true">
        <Icon name={role === 'user' ? 'user' : 'sparkle'} size={14} />
      </span>
      <div className="message-body">
        <MessageHeader label={streaming ? `${role} · streaming` : role} content={text} />
        <MarkdownViewer content={text} />
        {streaming ? <i className="stream-cursor" aria-hidden="true" /> : null}
        {role === 'assistant' && metrics ? <TurnMetadata metrics={metrics} /> : null}
      </div>
    </article>
  );
}

function format(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value, null, 2);
}

function savedAgentId(item: Run['items'][number]): string | null {
  if (item.type !== 'tool_call_output_item' || typeof item.output !== 'object' || item.output === null) return null;
  const agentId = (item.output as Record<string, unknown>).agent_id;
  return typeof agentId === 'string' ? agentId : null;
}

function RunActivityItem({ item }: { item: Run['items'][number] }) {
  const savedId = savedAgentId(item);
  const isToolCall = item.type === 'tool_call_item';
  const isToolOutput = item.type === 'tool_call_output_item';
  const isHandoff = item.type === 'handoff_output_item';
  const title = isToolCall
    ? item.title || item.description || 'Tool requested'
    : isToolOutput
      ? 'Tool result'
      : isHandoff
        ? `Handed off to ${item.target_agent}`
        : humanize(item.type);
  const detail = isToolOutput
    ? item.output
    : isHandoff
      ? `${item.source_agent} → ${item.target_agent}`
      : isToolCall
        ? item.description
        : null;

  if (detail == null && !savedId) {
    return (
      <div className="activity-item compact">
        <span className="activity-icon tool"><Icon name={isHandoff ? 'agents' : 'tools'} size={14} /></span>
        <span className="activity-item-title"><strong>{title}</strong><small>{item.agent_name}</small></span>
        <Icon name="check" size={14} />
      </div>
    );
  }

  return (
    <details className="activity-item">
      <summary>
        <span className="activity-icon tool"><Icon name={isHandoff ? 'agents' : 'tools'} size={14} /></span>
        <span className="activity-item-title"><strong>{title}</strong><small>{item.agent_name}</small></span>
        <Icon className="activity-chevron" name="arrowRight" size={13} />
      </summary>
      <div className="activity-item-detail">
        {detail != null ? <pre>{format(detail)}</pre> : null}
        {savedId ? (
          <Link className="button secondary small" to={`/agents/${savedId}`}>
            Open saved agent
          </Link>
        ) : null}
      </div>
    </details>
  );
}

function isVisibleActivityItem(item: Run['items'][number]): boolean {
  return item.type !== 'message_output_item' && item.type !== 'reasoning_item';
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

/** Everything one turn produced on the way to its answer, rebuilt from that run alone. */
interface TurnActivity {
  tools: ChatStreamState['tools'];
  items: Run['items'];
  reasoning: string;
}

function activityCount(activity: TurnActivity): number {
  return (
    activity.tools.length
    || activity.items.filter((item) => item.type === 'tool_call_item').length
  );
}

/** A turn is worth opening if it reasoned or called anything, not just if it used tools. */
function hasTurnDetail(activity: TurnActivity): boolean {
  return activity.items.length > 0 || activity.tools.length > 0 || Boolean(activity.reasoning);
}

/** Turns are labelled by position so a reopened panel says which turn it belongs to. */
function runTurnLabel(runs: Run[], run: Run): string {
  const index = runs.findIndex((candidate) => candidate.id === run.id);
  return index >= 0 ? `Turn ${index + 1} details` : 'Turn details';
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

function runActivityTitle(status: Run['status']): string {
  if (status === 'completed') return 'Work completed';
  if (status === 'failed') return 'Run failed';
  if (status === 'cancelled') return 'Run cancelled';
  if (status === 'paused') return 'Waiting for approval';
  return 'Working on your request';
}

function humanize(value: string): string {
  const words = value.replace(/[_-]+/g, ' ').trim();
  return words ? words.charAt(0).toLocaleUpperCase() + words.slice(1) : 'Tool';
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

function MessageHeader({ label, content }: { label: string; content: string }) {
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

function defaultBuilderModel(settings: Settings): ModelReference {
  return settings.default_model_references.chat ?? {};
}
