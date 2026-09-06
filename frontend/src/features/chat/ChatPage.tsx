import {
  Fragment,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
} from 'react';
import { subscribeToRun, type RunStreamEvent } from '../../api/events';
import { Icon } from '../../shared/components/Icons';
import { MarkdownViewer } from '../../shared/components/MarkdownViewer';
import { ErrorNotice, Loading } from '../../shared/components/Ui';
import {
  providersApi,
  type Provider,
  type Settings,
} from '../providers/api';
import {
  chatApi,
  type Conversation,
  type ConversationDetail,
  type ModelReference,
  type PromptSnapshot,
  type ResearchMode,
  type Run,
  type SteeringMessage,
} from './api';
import {
  ChatModelPicker,
  preferredChatModel,
  resolveModelReference,
} from './ChatModelPicker';
import {
  readStoredReasoningEffort,
  reasoningEffortsForModel,
  ReasoningEffortSelect,
  storeReasoningEffort,
  type ReasoningEffort,
} from './ReasoningEffortSelect';
import {
  applyChatStreamEvent,
  emptyChatStream,
  restoreChatStream,
  type ChatStreamState,
} from './chatStream';
import {
  buildTurnTimeline,
  describeLiveActivity,
  emptyTurnTimeline,
  type TimelineSource,
  type TurnTimeline,
} from './chatTimeline';
import {
  ActivitySidebar,
  LiveActivityBar,
  SourceChips,
  SourceImages,
  TurnTimelineView,
} from './TurnTimeline';
import './chat.css';
import { useThrottledRates } from './useThrottledRates';

const SUGGESTIONS = [
  'Summarise the key findings across the papers in my library.',
  'Compare the methods used in the two most recent papers I added.',
  'What open research questions remain in this area?',
  'Draft research notes with citations for my current topic.',
];
const COMMON_CONTEXT_WINDOWS = [8_192, 16_384, 32_768, 65_536, 131_072, 262_144];
const RESEARCH_MODES: { value: ResearchMode; label: string; description: string }[] = [
  {
    value: 'learn',
    label: 'Learn / ask',
    description: 'Narrow sourced Q&A from papers, notes, or the web. No mandatory full summary or notes.',
  },
  {
    value: 'understand',
    label: 'Understand paper',
    description: 'Explain relevant passages and prerequisites with citations, without a full review.',
  },
  {
    value: 'review',
    label: 'Review / research',
    description: 'Read paper evidence and require cited summaries plus durable paper notes.',
  },
];

function configuredContextWindow(
  providers: Provider[],
  reference: ModelReference,
  fallback: number,
) {
  const provider = providers.find((item) => item.id === reference.provider_profile_id);
  return provider?.models.find((model) => model.name === reference.model)?.context_window_tokens
    ?? fallback;
}

function contextWindowLabel(tokens: number) {
  return tokens % 1_024 === 0 ? `${tokens / 1_024}K` : tokens.toLocaleString();
}

/* The two rails are resizable so a wide tool result can be read without leaving the chat. */
const LIST_WIDTH_KEY = 'scholarweave.chat.list-width';
const ACTIVITY_WIDTH_KEY = 'scholarweave.chat.activity-width';
const LIST_WIDTH = { value: 272, min: 200, max: 520 };
const ACTIVITY_WIDTH = { value: 320, min: 260, max: 760 };

type WidthBounds = { value: number; min: number; max: number };

function clampWidth(width: number, bounds: WidthBounds): number {
  return Math.round(Math.min(bounds.max, Math.max(bounds.min, width)));
}

function readStoredWidth(key: string, bounds: WidthBounds): number {
  if (typeof window === 'undefined') return bounds.value;
  const stored = Number(window.localStorage.getItem(key));
  return Number.isFinite(stored) && stored > 0 ? clampWidth(stored, bounds) : bounds.value;
}

function storeWidth(key: string, width: number) {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(key, String(width));
  } catch {
    /* A full or blocked store only costs the remembered width, never the session. */
  }
}

/** A drag handle on the pane edge; keyboard arrows move it too, so it is not mouse-only. */
function PaneResizer({
  side,
  label,
  width,
  bounds,
  onResize,
  onResizingChange,
}: {
  side: 'left' | 'right';
  label: string;
  width: number;
  bounds: WidthBounds;
  onResize: (width: number) => void;
  onResizingChange: (resizing: boolean) => void;
}) {
  const [active, setActive] = useState(false);
  const originRef = useRef({ pointer: 0, width });

  const finish = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!active) return;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
    setActive(false);
    onResizingChange(false);
  };

  return (
    <div
      className={`pane-resizer ${side}${active ? ' active' : ''}`}
      role="separator"
      tabIndex={0}
      aria-orientation="vertical"
      aria-label={label}
      aria-valuenow={width}
      aria-valuemin={bounds.min}
      aria-valuemax={bounds.max}
      onPointerDown={(event) => {
        event.preventDefault();
        event.currentTarget.setPointerCapture?.(event.pointerId);
        originRef.current = { pointer: event.clientX, width };
        setActive(true);
        onResizingChange(true);
      }}
      onPointerMove={(event) => {
        if (!active) return;
        const delta = event.clientX - originRef.current.pointer;
        onResize(clampWidth(originRef.current.width + (side === 'right' ? delta : -delta), bounds));
      }}
      onPointerUp={finish}
      onPointerCancel={finish}
      onDoubleClick={() => onResize(bounds.value)}
      onKeyDown={(event) => {
        const step = event.shiftKey ? 48 : 16;
        if (event.key === 'ArrowLeft') {
          event.preventDefault();
          onResize(clampWidth(width + (side === 'right' ? -step : step), bounds));
        } else if (event.key === 'ArrowRight') {
          event.preventDefault();
          onResize(clampWidth(width + (side === 'right' ? step : -step), bounds));
        } else if (event.key === 'Home') {
          event.preventDefault();
          onResize(bounds.value);
        }
      }}
    />
  );
}

export function ResearchChatPage() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [current, setCurrent] = useState<ConversationDetail | null>(null);
  const [modelReference, setModelReference] = useState<ModelReference>({});
  const [preferredModelReference, setPreferredModelReference] = useState<ModelReference>({});
  const [reasoningEffort, setReasoningEffort] = useState<ReasoningEffort | null>(
    readStoredReasoningEffort,
  );
  const [contextWindowTokens, setContextWindowTokens] = useState(32_768);
  const [webEnabled, setWebEnabled] = useState(true);
  const [deepWork, setDeepWork] = useState(false);
  const [fastAnswer, setFastAnswer] = useState(false);
  const [researchMode, setResearchMode] = useState<ResearchMode>('learn');
  const [webSearchLimit, setWebSearchLimit] = useState(1);
  const [run, setRun] = useState<Run | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [stream, setStream] = useState<ChatStreamState>(emptyChatStream);
  const [optimisticUser, setOptimisticUser] = useState<string | null>(null);
  const [steeringMessages, setSteeringMessages] = useState<
    (Omit<SteeringMessage, 'status'> & { status: 'queued' | 'applied' })[]
  >([]);
  const [content, setContent] = useState('');
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [queueingSteering, setQueueingSteering] = useState(false);
  const [stopping, setStopping] = useState<'stop' | 'answer' | null>(null);
  const [pinnedToBottom, setPinnedToBottom] = useState(true);
  const [sidebarOpen, setSidebarOpen] = useState(
    () => typeof window === 'undefined' || window.innerWidth > 900,
  );
  const [activityOpen, setActivityOpen] = useState(false);
  const [listWidth, setListWidth] = useState(() => readStoredWidth(LIST_WIDTH_KEY, LIST_WIDTH));
  const [activityWidth, setActivityWidth] = useState(
    () => readStoredWidth(ACTIVITY_WIDTH_KEY, ACTIVITY_WIDTH),
  );
  const [resizing, setResizing] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const messageListRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const openRequestRef = useRef(0);
  const runLifecycleRef = useRef(0);
  const effectiveModelReference = useMemo(
    () => resolveModelReference(modelReference, settings),
    [modelReference.provider_profile_id, modelReference.model, settings],
  );
  const supportedReasoningEfforts = useMemo(
    () => reasoningEffortsForModel(providers, effectiveModelReference),
    [providers, effectiveModelReference.provider_profile_id, effectiveModelReference.model],
  );
  const deepWorkLocked = current?.kind === 'deep_work';

  const refreshList = () => chatApi.list().then(setConversations);
  const open = async (
    id: string,
    contextProviders = providers,
    contextSettings: Settings | null = settings,
  ) => {
    const request = ++openRequestRef.current;
    setRun(null);
    setRuns([]);
    setStream(emptyChatStream);
    setOptimisticUser(null);
    setSteeringMessages([]);
    setSending(false);
    setStopping(null);
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
    setDeepWork(conversation.kind === 'deep_work');
    setResearchMode(conversation.kind === 'deep_work' ? 'review' : 'learn');
    setFastAnswer(false);
    setModelReference(conversation.model_reference);
    setContextWindowTokens(
      configuredContextWindow(
        contextProviders,
        resolveModelReference(conversation.model_reference, contextSettings),
        contextSettings?.agent_context_window_tokens ?? 32_768,
      ),
    );
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
        setContextWindowTokens(
          configuredContextWindow(
            nextProviders,
            preferredModel,
            nextSettings.agent_context_window_tokens,
          ),
        );
        if (items[0]) {
          await open(items[0].id, nextProviders, nextSettings);
        }
      })
      .catch(setError)
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (
      reasoningEffort
      && effectiveModelReference.provider_profile_id
      && effectiveModelReference.model
      && !supportedReasoningEfforts?.includes(reasoningEffort)
    ) {
      setReasoningEffort(null);
      storeReasoningEffort(null);
    }
  }, [reasoningEffort, supportedReasoningEfforts]);

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
    if (!run || stopping) return;

    const lifecycle = ++runLifecycleRef.current;
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
        if (cancelled || lifecycle !== runLifecycleRef.current) return;
        setRun(nextRun);
        setRuns((previous) => mergeRun(previous, nextRun));
        if (nextConversation) setCurrent(nextConversation);
        setOptimisticUser(null);
        setSteeringMessages([]);
        setStream(displayStream(nextRun));
        void refreshList();
      } catch (nextError) {
        if (!cancelled && lifecycle === runLifecycleRef.current) setError(nextError);
      } finally {
        if (!cancelled && lifecycle === runLifecycleRef.current) {
          setSending(false);
          window.setTimeout(() => composerRef.current?.focus(), 0);
        }
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
        if (
          event.event_type === 'steering.queued'
          || event.event_type === 'steering.applied'
        ) {
          const messageId = String(event.payload.message_id ?? '');
          const messageContent = String(event.payload.content ?? '');
          const status = event.event_type === 'steering.applied' ? 'applied' : 'queued';
          setSteeringMessages((messages) =>
            messages.some((message) => message.id === messageId)
              ? messages.map((message) =>
                  message.id === messageId
                    ? {
                        ...message,
                        content: messageContent || message.content,
                        status: status === 'applied' ? 'applied' : message.status,
                      }
                    : message
                )
              : [...messages, { id: messageId, content: messageContent, status }]
          );
        }

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
  }, [run?.id, run?.status, current?.id, sending, stopping]);

  const resetThread = () => {
    openRequestRef.current += 1;
    setRun(null);
    setRuns([]);
    setStream(emptyChatStream);
    setOptimisticUser(null);
    setSteeringMessages([]);
    setSending(false);
    setStopping(null);
    setPinnedToBottom(true);
  };

  const create = () => {
    setCurrent(null);
    setResearchMode('learn');
    setDeepWork(false);
    setFastAnswer(false);
    setModelReference(preferredModelReference);
    setContextWindowTokens(
      configuredContextWindow(
        providers,
        preferredModelReference,
        settings?.agent_context_window_tokens ?? 32_768,
      ),
    );
    resetThread();
    composerRef.current?.focus();
  };

  const selectModel = (reference: ModelReference) => {
    setModelReference(reference);
    setPreferredModelReference(reference);
    setContextWindowTokens(
      configuredContextWindow(
        providers,
        reference,
        settings?.agent_context_window_tokens ?? 32_768,
      ),
    );
    void providersApi
      .updateSettings({ last_chat_model_reference: reference })
      .then(setSettings)
      .catch(setError);
    if (current) {
      setCurrent(null);
      resetThread();
    }
  };

  const selectReasoningEffort = (effort: ReasoningEffort | null) => {
    setReasoningEffort(effort);
    storeReasoningEffort(effort);
  };

  const stopRun = async (answerWithAvailableInformation: boolean) => {
    if (!run || !['pending', 'running'].includes(run.status) || stopping) return;
    const request = openRequestRef.current;
    const previousLifecycle = runLifecycleRef.current;
    runLifecycleRef.current += 1;
    setStopping(answerWithAvailableInformation ? 'answer' : 'stop');
    setError(null);
    try {
      if (answerWithAvailableInformation) {
        const response = await chatApi.stopAndAnswer(run.id);
        if (request !== openRequestRef.current) return;
        setRuns((previous) =>
          mergeRun(mergeRun(previous, response.stopped_run), response.answer_run),
        );
        setRun(response.answer_run);
        setStream(displayStream(response.answer_run));
        setSending(true);
      } else {
        const stoppedRun = await chatApi.cancelRun(run.id);
        if (request !== openRequestRef.current) return;
        setRuns((previous) => mergeRun(previous, stoppedRun));
        setRun(stoppedRun);
        setStream(displayStream(stoppedRun));
      }
    } catch (nextError) {
      if (request === openRequestRef.current) {
        runLifecycleRef.current = previousLifecycle;
        setError(nextError);
      }
    } finally {
      if (request === openRequestRef.current) setStopping(null);
    }
  };

  const remove = async (id: string) => {
    const conversation = conversations.find((item) => item.id === id);
    if (
      !window.confirm(
        `Delete "${conversation?.title ?? 'this chat'}"? This cannot be undone.`,
      )
    ) return;
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
    if (!submitted) return;
    if (sending) {
      if (
        !run
        || !['pending', 'running'].includes(run.status)
        || queueingSteering
      ) return;
      setQueueingSteering(true);
      setError(null);
      setContent('');
      try {
        const message = await chatApi.steer(run.id, submitted);
        setSteeringMessages((messages) =>
          messages.some((item) => item.id === message.id)
            ? messages
            : [...messages, { ...message, status: 'queued' }]
        );
        setPinnedToBottom(true);
      } catch (nextError) {
        setContent(submitted);
        setError(nextError);
      } finally {
        setQueueingSteering(false);
        window.setTimeout(() => composerRef.current?.focus(), 0);
      }
      return;
    }
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
        const created = await chatApi.create(modelReference);
        if (request !== openRequestRef.current) return;
        conversation = await chatApi.get(created.id);
        if (request !== openRequestRef.current) return;
        setCurrent(conversation);
        setModelReference(conversation.model_reference);
        void refreshList();
      }
      const response = await chatApi.send(
        conversation.id,
        submitted,
        reasoningEffort,
        webEnabled,
        deepWork,
        !deepWork && fastAnswer,
        webSearchLimit,
        contextWindowTokens,
        deepWork ? 'review' : researchMode,
      );
      if (request !== openRequestRef.current) return;
      setCurrent((active) => (
        active
          ? { ...active, ...response.conversation }
          : active
      ));
      setRun(response.run);
      setRuns((previous) => mergeRun(previous, response.run));
      setStream(displayStream(response.run));
      void refreshList();
      window.setTimeout(() => composerRef.current?.focus(), 0);
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
  const activityTimelines = runs
    .map((candidate, index) => ({
      id: candidate.id,
      label: `Run ${index + 1}`,
      timeline: activityTimeline(timelineFor(candidate)),
      snapshot: promptSnapshotFromRun(candidate),
    }))
    .filter(({ timeline, snapshot }) => timeline.steps.length > 0 || snapshot != null);
  const activityCount = activityTimelines.reduce(
    (total, { timeline }) => total + timeline.steps.length,
    0,
  );
  const streamingTimeline = liveReasoningTimeline(liveTimeline);
  const runActive = sending || Boolean(run && !isTerminalRun(run));
  const liveActivity = runActive
    ? describeLiveActivity(liveTimeline, { writing: Boolean(stream.assistant) })
    : null;
  const activeDelegate = [...liveTimeline.steps]
    .reverse()
    .find((step) => step.kind === 'agent' && step.status === 'running');
  const compaction = compactionIndicator(runs, run, stream.events);
  const rawMetrics = useMemo(() => {
    const values = new Map<string, TurnMetrics>();
    for (const candidate of runs) {
      const metrics = turnMetrics(candidate);
      if (metrics) values.set(candidate.id, metrics);
    }
    if (run) {
      const metrics = turnMetrics(run, stream.events);
      if (metrics) values.set(run.id, metrics);
    }
    return values;
  }, [runs, run, stream.events]);
  const metricsByRun = useThrottledRates(rawMetrics);
  const liveMetrics = run ? metricsByRun.get(run.id) ?? null : null;

  // Every run is anchored, so a turn without tools still keeps its trace in place.
  const reasoningAnchors = useMemo(
    () => anchorRunsToItems(current?.items ?? [], runs),
    [current?.items, runs],
  );
  const recoveredRunResponses = useMemo(
    () => missingRunResponsesByUserIndex(current?.items ?? [], runs),
    [current?.items, runs],
  );

  if (loading || !settings) return <Loading label="Loading agent conversations…" />;

  const hasTranscript = Boolean(current?.items.length || run || optimisticUser);

  /** Re-asking is the only way to redo a turn, since the transcript itself is append-only. */
  const retry = (prompt: string) => {
    if (sending || !prompt.trim()) return;
    void send(prompt);
  };

  return (
    <div className="chat-page">
      {error ? <ErrorNotice error={error} /> : null}
      <div
        className={`chat-layout${sidebarOpen ? '' : ' collapsed'}${activityOpen ? ' activity-open' : ''}${resizing ? ' resizing' : ''}`}
        style={{
          '--chat-list-width': `${listWidth}px`,
          '--chat-activity-width': `${activityWidth}px`,
        } as CSSProperties}
      >
        <aside className="conversation-list" aria-label="Conversations">
          <div className="conversation-list-head">
            <button className="button block new-chat" type="button" onClick={create}>
              <Icon name="plus" size={15} />
              New chat
            </button>
            <div className="conversation-search">
              <Icon name="search" size={14} />
              <input
                type="search"
                value={query}
                placeholder="Search chats"
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
                  {conversation.kind === 'deep_work' ? (
                    <span className="conversation-mode">
                      <Icon name="agents" size={11} />
                      Deep Work
                    </span>
                  ) : null}
                  <span className="conversation-time">{relativeTime(conversation.updated_at)}</span>
                </button>
                <button
                  type="button"
                  className="conversation-delete"
                  title="Delete chat"
                  aria-label={`Delete ${conversation.title}`}
                  disabled={current?.id === conversation.id && sending}
                  onClick={() => void remove(conversation.id)}
                >
                  <Icon name="trash" size={14} />
                </button>
              </div>
            ))}
            {!filtered.length ? (
              <p className="conversation-empty">
                {conversations.length ? 'No chats match your search.' : 'No chats yet.'}
              </p>
            ) : null}
          </div>
          <PaneResizer
            side="right"
            label="Resize chat list"
            width={listWidth}
            bounds={LIST_WIDTH}
            onResize={(next) => {
              setListWidth(next);
              storeWidth(LIST_WIDTH_KEY, next);
            }}
            onResizingChange={setResizing}
          />
        </aside>

        <section className="chat-surface">
          <header className="chat-header">
            <button
              type="button"
              className="chat-list-toggle"
              aria-label={sidebarOpen ? 'Hide chat list' : 'Show chat list'}
              title={sidebarOpen ? 'Hide chat list' : 'Show chat list'}
              onClick={() => setSidebarOpen((value) => !value)}
            >
              <Icon name="sidebar" size={16} />
            </button>
            <h1>{current?.title ?? (deepWork ? 'New deep work' : 'New research')}</h1>
            {deepWork ? (
              <span className="chat-mode-badge">
                <Icon name="agents" size={12} />
                Deep Work
              </span>
            ) : null}
            {activeDelegate?.kind === 'agent' ? (
              <button
                type="button"
                className="delegate-status"
                onClick={() => setActivityOpen(true)}
                title="Open delegated worker activity"
              >
                <span className="spinner tiny" aria-hidden="true" />
                {activeDelegate.name} running
              </button>
            ) : null}
            {compaction ? (
              <span
                className={`compaction-indicator ${compaction.state}`}
                role="status"
                title={compaction.title}
              >
                {compaction.state === 'active' ? (
                  <span className="spinner tiny" aria-hidden="true" />
                ) : (
                  <Icon name={compaction.state === 'failed' ? 'close' : 'check'} size={13} />
                )}
                {compaction.label}
              </span>
            ) : null}
            <button
              type="button"
              className={`activity-toggle${activityOpen ? ' active' : ''}`}
              aria-label={activityOpen ? 'Hide run activity' : 'Show run activity'}
              aria-expanded={activityOpen}
              onClick={() => setActivityOpen((value) => !value)}
            >
              <Icon name="tools" size={15} />
              {activityCount ? <span>{activityCount}</span> : null}
            </button>
          </header>

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
                  <h2>{deepWork ? 'What needs deeper research?' : 'What should we research?'}</h2>
                  <p>
                    {deepWork
                      ? 'Use focused workers for broad comparisons, literature reviews, and multi-source synthesis.'
                      : 'Ask for evidence, comparisons, open questions, or written notes.'}
                  </p>
                  <div className="suggestion-grid">
                    {SUGGESTIONS.map((suggestion) => (
                      <button
                        type="button"
                        className="suggestion"
                        key={suggestion}
                        disabled={sending}
                        onClick={() => void send(suggestion)}
                      >
                        {suggestion}
                      </button>
                    ))}
                  </div>
                </div>
              ) : null}
              {current?.items.map((item, index) => {
                if (!item.text?.trim()) return null;
                const role = item.role ?? item.type;
                if (role !== 'user' && role !== 'assistant') return null;
                const responseRun = reasoningAnchors.responseByIndex.get(index) ?? null;
                const recoveredResponses = recoveredRunResponses.get(index) ?? [];
                const recoveredRun = reasoningAnchors.byIndex.get(index) ?? null;
                return (
                  <Fragment key={index}>
                    <Message
                      role={role}
                      text={item.text}
                      metrics={responseRun ? metricsByRun.get(responseRun.id) ?? null : null}
                      sources={responseRun ? timelineFor(responseRun).sources : null}
                      onRetry={role === 'assistant' ? () => retry(promptFor(current.items, index)) : null}
                      canRetry={!sending}
                    />
                    {recoveredResponses.map((text, responseIndex) => (
                      <Message
                        role="assistant"
                        text={text}
                        metrics={
                          recoveredRun && responseIndex === recoveredResponses.length - 1
                            ? metricsByRun.get(recoveredRun.id) ?? null
                            : null
                        }
                        sources={
                          recoveredRun && responseIndex === recoveredResponses.length - 1
                            ? timelineFor(recoveredRun).sources
                            : null
                        }
                        onRetry={() => retry(item.text ?? '')}
                        canRetry={!sending}
                        key={`${recoveredRun?.id ?? index}-recovered-${responseIndex}`}
                      />
                    ))}
                  </Fragment>
                );
              })}
              {optimisticUser ? (
                <Message role="user" text={optimisticUser} className="optimistic" />
              ) : null}
              {steeringMessages.map((message) => (
                <Message
                  role="user"
                  text={message.content}
                  className={`optimistic steering-${message.status}`}
                  key={message.id}
                />
              ))}
              {streamingTimeline ? <TurnTimelineView timeline={streamingTimeline} /> : null}
              {stream.assistant ? (
                <Message
                  role="assistant"
                  text={stream.assistant}
                  className="streaming"
                  streaming
                  metrics={liveMetrics}
                  sources={liveTimeline.sources}
                />
              ) : null}
              {runActive && !stream.assistant && liveMetrics ? (
                <TurnMetadata metrics={liveMetrics} />
              ) : null}
              {liveActivity ? (
                <LiveActivityBar
                  key={run?.id ?? 'pending-run'}
                  activity={liveActivity}
                  onOpenActivity={activityOpen ? undefined : () => setActivityOpen(true)}
                />
              ) : null}
              {run?.error ? <div className="notice error chat-run-error">{run.error}</div> : null}
            </div>

            {!pinnedToBottom && hasTranscript ? (
              <button
                type="button"
                className="scroll-to-bottom"
                aria-label="Jump to latest"
                onClick={() => {
                  setPinnedToBottom(true);
                  messageListRef.current?.scrollTo({
                    top: messageListRef.current.scrollHeight,
                    behavior: 'smooth',
                  });
                }}
              >
                <Icon name="arrowRight" size={15} />
              </button>
            ) : null}

            <div className="composer">
              <div className="composer-inner">
                {/* One control surface: what you type, what answers, and how you send it. */}
                <div className="composer-box">
                  <textarea
                    ref={composerRef}
                    rows={1}
                    value={content}
                    onChange={(event) => setContent(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter' && !event.shiftKey) {
                        event.preventDefault();
                        void send();
                      }
                    }}
                    placeholder={
                      sending
                        ? 'Steer the next model call…'
                        : 'Ask for a research outcome…'
                    }
                  />
                  <div className="composer-bar">
                    <ChatModelPicker
                      providers={providers}
                      settings={settings}
                      value={modelReference}
                      disabled={sending}
                      onChange={selectModel}
                    />
                    <details className="composer-options">
                      <summary>
                        <Icon name="settings" size={13} />
                        Options
                      </summary>
                      <div className="composer-options-popover">
                        <ReasoningEffortSelect
                          value={reasoningEffort}
                          supportedEfforts={supportedReasoningEfforts}
                          disabled={sending}
                          onChange={selectReasoningEffort}
                        />
                        <label
                          className="composer-context"
                          title="Context window used for chat history and automatic compaction"
                        >
                          <span>Context</span>
                          <select
                            aria-label="Context size"
                            value={contextWindowTokens}
                            onChange={(event) => setContextWindowTokens(Number(event.target.value))}
                            disabled={sending}
                          >
                            {[...new Set([...COMMON_CONTEXT_WINDOWS, contextWindowTokens])]
                              .sort((left, right) => left - right)
                              .map((tokens) => (
                                <option key={tokens} value={tokens}>
                                  {contextWindowLabel(tokens)}
                                </option>
                              ))}
                          </select>
                        </label>
                      </div>
                    </details>
                    <button
                      className={`composer-capability${webEnabled ? ' active' : ''}`}
                      type="button"
                      aria-label="Toggle web access"
                      aria-pressed={webEnabled}
                      title={webEnabled ? 'Web access enabled' : 'Web access disabled'}
                      disabled={sending}
                      onClick={() => {
                        setWebEnabled((enabled) => {
                          if (enabled) setFastAnswer(false);
                          return !enabled;
                        });
                      }}
                    >
                      <Icon name="globe" size={13} />
                      Web
                    </button>
                    <label className="research-mode-control">
                      <span>Mode</span>
                      <select
                        aria-label="Research mode"
                        value={deepWork ? 'review' : researchMode}
                        disabled={sending || deepWork}
                        title={RESEARCH_MODES.find((mode) => mode.value === researchMode)?.description}
                        onChange={(event) => {
                          const selected = RESEARCH_MODES.find(
                            (mode) => mode.value === event.target.value,
                          );
                          if (selected) {
                            setResearchMode(selected.value);
                            setFastAnswer(false);
                          }
                        }}
                      >
                        {RESEARCH_MODES.map((mode) => (
                          <option key={mode.value} value={mode.value}>{mode.label}</option>
                        ))}
                      </select>
                    </label>
                    <button
                      className={`composer-capability${deepWork ? ' active' : ''}`}
                      type="button"
                      aria-label="Toggle deep work"
                      aria-pressed={deepWork}
                      title={
                        deepWorkLocked
                          ? 'Deep Work is permanently enabled for this conversation'
                          : 'Permanently upgrade this conversation to use a coordinator and focused worker'
                      }
                      disabled={sending || deepWorkLocked}
                      onClick={() => {
                        setDeepWork((enabled) => {
                          if (!enabled) {
                            setFastAnswer(false);
                            setResearchMode('review');
                          }
                          return !enabled;
                        });
                      }}
                    >
                      <Icon name="agents" size={13} />
                      Deep work
                    </button>
                    {!deepWork ? (
                      <div className={`fast-answer-control${fastAnswer ? ' active' : ''}`}>
                        <button
                          className="composer-capability"
                          type="button"
                          aria-label="Toggle fast answer"
                          aria-pressed={fastAnswer}
                          title="Web-only shortcut. Use Learn / ask for quick paper Q&A."
                          disabled={sending}
                          onClick={() => {
                            setFastAnswer((enabled) => {
                              if (!enabled) {
                                setWebEnabled(true);
                                setDeepWork(false);
                                setResearchMode('learn');
                              }
                              return !enabled;
                            });
                          }}
                        >
                          <Icon name="bulb" size={13} />
                          Fast web answer
                        </button>
                        {fastAnswer ? (
                          <input
                            type="number"
                            min={1}
                            max={100}
                            step={1}
                            value={webSearchLimit}
                            aria-label="Fast answer web search limit"
                            title="Maximum web searches"
                            disabled={sending}
                            onChange={(event) => {
                              const value = Number.parseInt(event.target.value, 10);
                              if (Number.isFinite(value)) {
                                setWebSearchLimit(Math.min(100, Math.max(1, value)));
                              }
                            }}
                          />
                        ) : null}
                      </div>
                    ) : null}
                    <span className="composer-hint">
                      <kbd>Enter</kbd> to send
                    </span>
                    {sending && run && ['pending', 'running'].includes(run.status) ? (
                      <>
                        <div className="composer-run-actions">
                          <button
                            className="composer-run-stop"
                            type="button"
                            aria-label="Stop current run"
                            disabled={stopping !== null}
                            onClick={() => void stopRun(false)}
                          >
                            {stopping === 'stop'
                              ? <span className="spinner tiny" aria-hidden="true" />
                              : <Icon name="stop" size={13} />}
                            Stop
                          </button>
                          <button
                            className="composer-run-answer"
                            type="button"
                            aria-label="Stop and answer with available information"
                            disabled={stopping !== null}
                            onClick={() => void stopRun(true)}
                          >
                            {stopping === 'answer'
                              ? <span className="spinner tiny" aria-hidden="true" />
                              : null}
                            Answer now
                          </button>
                        </div>
                        <button
                          className="composer-icon composer-send"
                          type="button"
                          aria-label="Queue steering message"
                          disabled={queueingSteering || !content.trim()}
                          onClick={() => void send()}
                        >
                          {queueingSteering
                            ? <span className="spinner tiny" aria-hidden="true" />
                            : <Icon name="arrowRight" size={16} />}
                        </button>
                      </>
                    ) : (
                      <button
                        className="composer-icon composer-send"
                        type="button"
                        aria-label="Send message"
                        disabled={sending || !content.trim()}
                        onClick={() => void send()}
                      >
                        <Icon name="arrowRight" size={16} />
                      </button>
                    )}
                  </div>
                </div>
              </div>
            </div>
          </div>
        </section>
        <ActivitySidebar
          open={activityOpen}
          timelines={activityTimelines}
          onClose={() => setActivityOpen(false)}
          resizer={
            <PaneResizer
              side="left"
              label="Resize activity panel"
              width={activityWidth}
              bounds={ACTIVITY_WIDTH}
              onResize={(next) => {
                setActivityWidth(next);
                storeWidth(ACTIVITY_WIDTH_KEY, next);
              }}
              onResizingChange={setResizing}
            />
          }
        />
      </div>
    </div>
  );
}

export default function ChatPage() {
  return <ResearchChatPage />;
}

type CompactionIndicator = {
  state: 'active' | 'complete' | 'failed';
  label: string;
  title: string;
};

function compactionIndicator(
  runs: Run[],
  activeRun: Run | null,
  liveEvents: RunStreamEvent[],
): CompactionIndicator | null {
  const contextEvents = runs.flatMap((candidate) => {
    let events: RunStreamEvent[] = candidate.events;
    if (candidate.id === activeRun?.id) {
      const bySequence = new Map<number, RunStreamEvent>(
        candidate.events.map((event) => [event.sequence, event]),
      );
      for (const event of liveEvents) bySequence.set(event.sequence, event);
      events = [...bySequence.values()];
    }
    const orderedEvents = [...events].sort((left, right) => left.sequence - right.sequence);
    const identityEvents = orderedEvents.filter((event) => (
      ['agent.started', 'context.prepared', 'model.telemetry'].includes(event.event_type)
      && event.payload.delegated !== true
      && (event.payload.context_scope == null || event.payload.context_scope === 'main')
      && (typeof event.payload.agent_name === 'string' || typeof event.payload.agent_id === 'string')
    ));
    const entryAgent = identityEvents[0]?.payload;
    const mainAgent = entryAgent && {
      ...entryAgent,
      agent_id: entryAgent.agent_id ?? identityEvents.find((event) => (
        event.payload.agent_name === entryAgent.agent_name
        && typeof event.payload.agent_id === 'string'
      ))?.payload.agent_id,
      agent_name: entryAgent.agent_name ?? identityEvents.find((event) => (
        event.payload.agent_id === entryAgent.agent_id
        && typeof event.payload.agent_name === 'string'
      ))?.payload.agent_name,
    };
    return orderedEvents
      .filter((event) => isMainContextEvent(event, mainAgent, candidate.agent_name))
      .filter((event) => (
        event.event_type.startsWith('context.compact')
        || event.event_type === 'context.prepared'
        || event.event_type === 'context.sized'
        || event.event_type === 'model.retry'
        || event.event_type === 'model.completed'
        || event.event_type === 'model.telemetry'
      ))
      .map((event) => ({ runId: candidate.id, event }));
  });
  const latestModelEvent = [...contextEvents].reverse().find(({ event }) => (
    (event.event_type === 'model.retry' || event.event_type === 'model.completed')
    && event.payload.delegated !== true
  ));
  if (
    latestModelEvent?.event.event_type === 'model.retry'
    && latestModelEvent.runId === activeRun?.id
    && activeRun != null
    && !isTerminalRun(activeRun)
  ) {
    return {
      state: 'active',
      label: 'Recomputing response',
      title: latestModelEvent.event.payload.reason === 'context_length_exceeded'
        ? 'The provider rejected the context size. Recomputing with a smaller request; completed tools will not be repeated.'
        : `Retrying the unfinished response with ${Number(
          latestModelEvent.event.payload.max_tokens,
        ).toLocaleString()} output tokens. Completed tools will not be repeated.`,
    };
  }
  const compactions = contextEvents.filter(({ event }) => (
    event.event_type.startsWith('context.compact')
  ));
  const sizingEntry = [...contextEvents].reverse().find(({ event }) => (
    (event.event_type === 'context.prepared' || event.event_type === 'context.sized'
      || event.event_type === 'model.telemetry')
    && contextSizing(event) !== null
  ));
  const prepared = sizingEntry && [...contextEvents].reverse().find(({ event, runId }) => (
    runId === sizingEntry.runId && event.sequence < sizingEntry.event.sequence
    && (event.event_type === 'context.prepared' || event.event_type === 'context.sized')
  ))?.event;
  const sizing = sizingEntry ? contextSizing(sizingEntry.event, prepared) : null;
  if (!compactions.length) {
    return sizing ? { state: 'complete', label: `Context ${sizing.label}`, title: sizing.title } : null;
  }

  const completed = compactions.filter(({ event }) => event.event_type === 'context.compacted');
  const latest = compactions[compactions.length - 1];
  const active = latest.event.event_type === 'context.compaction_started'
    && latest.runId === activeRun?.id
    && activeRun != null
    && !isTerminalRun(activeRun);
  if (active) {
    return {
      state: 'active',
      label: 'Compacting context',
      title: compactionTitle(latest.event),
    };
  }
  if (latest.event.event_type === 'context.compaction_failed') {
    return {
      state: 'failed',
      label: 'Compaction issue',
      title: String(latest.event.payload.error ?? 'Context compaction failed.'),
    };
  }
  const lastCompleted = completed[completed.length - 1]?.event ?? latest.event;
  const label = completed.length > 1 ? `Context compacted x${completed.length}` : 'Context compacted';
  return {
    state: 'complete',
    label: sizing ? `${label} · ${sizing.label}` : label,
    title: sizing
      ? `${compactionTitle(lastCompleted)} ${sizing.title}`
      : compactionTitle(lastCompleted),
  };
}

function isMainContextEvent(
  event: RunStreamEvent,
  mainAgent: Record<string, unknown> | undefined,
  mainName: string,
): boolean {
  const payload = event.payload;
  if (payload.delegated === true
    || (payload.context_scope != null && payload.context_scope !== 'main')) return false;
  if (typeof payload.agent_id === 'string' && typeof mainAgent?.agent_id === 'string'
    && payload.agent_id !== mainAgent.agent_id) return false;
  const name = mainAgent?.agent_name ?? mainName;
  return typeof payload.agent_name !== 'string' || payload.agent_name === name;
}

function contextSizing(
  event: RunStreamEvent,
  prepared?: RunStreamEvent,
): { label: string; title: string } | null {
  if (event.event_type === 'model.telemetry') {
    if (event.payload.completed !== true || event.payload.finish_reason === 'length'
      || event.payload.usage_complete === false) return null;
    const usage = isRecord(event.payload.usage) ? event.payload.usage : null;
    const input = metricNumber(usage?.input_tokens ?? usage?.prompt_tokens);
    const output = metricNumber(usage?.output_tokens ?? usage?.completion_tokens);
    if (input == null || output == null) return null;
    const used = metricNumber(input + output);
    if (used == null) return null;
    const capacity = positiveMetricNumber(prepared?.payload.context_window_tokens);
    return {
      label: `${used.toLocaleString()}${capacity == null ? '' : ` / ${capacity.toLocaleString()}`}`,
      title: `MAIN context: ${used.toLocaleString()} tokens used (${input.toLocaleString()} input + ${output.toLocaleString()} generated) from the latest completed MAIN call.`
        + (capacity == null ? ' Full context capacity unavailable.' : ` Full context window: ${capacity.toLocaleString()} tokens.`)
        + ' Not overall run usage.',
    };
  }
  const estimated = event.payload.estimated_input_tokens;
  const budget = event.payload.input_budget_tokens;
  if (
    typeof estimated !== 'number' || !Number.isFinite(estimated) || estimated < 0
    || typeof budget !== 'number' || !Number.isFinite(budget) || budget <= 0
  ) return null;
  const label = `~${estimated.toLocaleString()} / ${budget.toLocaleString()}`;
  const reserve = event.payload.response_headroom_tokens;
  const schemas = event.payload.tool_schema_tokens;
  const agent = typeof event.payload.agent_name === 'string' ? `${event.payload.agent_name}: ` : '';
  return {
    label,
    title: `${agent}MAIN context: Estimated input ${estimated.toLocaleString()} of ${budget.toLocaleString()} tokens, including tool schemas.`
      + (typeof schemas === 'number' && Number.isFinite(schemas)
        ? ` Tool schemas: ~${schemas.toLocaleString()} tokens.` : '')
      + (typeof reserve === 'number' && Number.isFinite(reserve)
        ? ` Response headroom: ${reserve.toLocaleString()} tokens.` : ''),
  };
}

function compactionTitle(event: RunStreamEvent): string {
  const before = Number(event.payload.estimated_tokens_before);
  const after = Number(event.payload.estimated_tokens_after);
  if (Number.isFinite(before) && Number.isFinite(after)) {
    return `Context reduced from about ${before.toLocaleString()} to ${after.toLocaleString()} tokens.`;
  }
  if (Number.isFinite(before)) {
    return `Compaction triggered at about ${before.toLocaleString()} tokens.`;
  }
  return 'Context compaction was triggered for this conversation.';
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
  // No avatar, no role caption: the shape of the turn already says who is speaking.
  return (
    <article className={`message role-${role} ${className}`.trim()}>
      <div className="message-body">
        <MarkdownViewer content={text} />
        {streaming ? <i className="stream-cursor" aria-hidden="true" /> : null}
        {isAssistant && sources?.length ? <SourceImages sources={sources} /> : null}
        {isAssistant && sources?.length ? <SourceChips sources={sources} /> : null}
      </div>
      <MessageActions
        content={text}
        metrics={isAssistant ? metrics : null}
        onRetry={isAssistant ? onRetry : null}
        canRetry={canRetry}
      />
    </article>
  );
}

/** Everything you can do with a message, revealed only when you reach for it. */
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

  const copy = async () => {
    try {
      await copyToClipboard(content);
      setCopyStatus('copied');
      window.setTimeout(() => setCopyStatus('idle'), 1800);
    } catch {
      setCopyStatus('failed');
    }
  };

  return (
    <div className="message-actions">
      <button
        type="button"
        className={copyStatus === 'failed' ? 'failed' : ''}
        title={copyStatus === 'copied' ? 'Copied' : 'Copy'}
        aria-label={copyStatus === 'copied' ? 'Copied' : 'Copy'}
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
      {metrics ? <TurnMetadata metrics={metrics} /> : null}
    </div>
  );
}

function isTerminalRun(run: Run): boolean {
  return ['completed', 'failed', 'cancelled'].includes(run.status);
}

function isTerminalEvent(eventType: string): boolean {
  return ['run.completed', 'run.failed', 'run.cancelled'].includes(eventType);
}

function displayStream(run: Run): ChatStreamState {
  const restored = restoreChatStream(run.events);
  return run.status === 'completed' ? { ...restored, assistant: '' } : restored;
}

function liveReasoningTimeline(timeline: TurnTimeline): TurnTimeline | null {
  const steps = timeline.steps.filter(
    (step) => step.kind === 'reasoning' && step.streaming,
  );
  if (!steps.length) return null;
  return {
    ...timeline,
    steps,
    sources: [],
    toolCount: 0,
    agentCount: 0,
    running: true,
  };
}

function activityTimeline(timeline: TurnTimeline): TurnTimeline {
  return {
    ...timeline,
    steps: timeline.steps.filter(
      (step) => step.kind !== 'reasoning' || !step.streaming,
    ),
  };
}

function promptSnapshotFromRun(run: Run): PromptSnapshot | null {
  const snapshotEvent = run.events.find((event) => event.event_type === 'prompt.snapshot');
  if (!snapshotEvent) return null;
  return snapshotEvent.payload as PromptSnapshot;
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

export function missingRunResponsesByUserIndex(
  items: ConversationDetail['items'],
  runs: Run[],
): Map<number, string[]> {
  const recovered = new Map<number, string[]>();
  const anchors = anchorRunsToItems(items, runs);

  for (const [userIndex, runForItem] of anchors.byIndex) {
    if (!isTerminalRun(runForItem)) continue;
    const visibleResponses: string[] = [];
    for (let index = userIndex + 1; index < items.length; index += 1) {
      const role = items[index].role ?? items[index].type;
      if (role === 'user') break;
      if (role === 'assistant' && items[index].text?.trim()) {
        visibleResponses.push(items[index].text!.trim());
      }
    }

    const missing: string[] = [];
    const unmatchedVisible = [...visibleResponses];
    for (const runItem of runForItem.items) {
      if (runItem.type !== 'message_output_item') continue;
      const response = typeof runItem.content === 'string' ? runItem.content.trim() : '';
      if (!response) continue;
      const match = unmatchedVisible.indexOf(response);
      if (match >= 0) {
        unmatchedVisible.splice(match, 1);
      } else {
        missing.push(response);
      }
    }
    if (missing.length) recovered.set(userIndex, missing);
  }

  return recovered;
}

interface TurnMetrics {
  inputTokens: number | null;
  outputTokens: number | null;
  inputEstimated: boolean;
  outputEstimated: boolean;
  promptRate: number | null;
  generationRate: number | null;
  durationSeconds: number | null;
  modelCalls: number | null;
  usageComplete: boolean | null;
}

export function turnMetrics(
  run: Run,
  events: readonly RunStreamEvent[] = [],
): TurnMetrics | null {
  const persistedUsage = run.usage as Record<string, unknown>;
  const usage = isTerminalRun(run) && isRecord(persistedUsage.performance)
    ? persistedUsage
    : latestUsageUpdate(events) ?? persistedUsage;
  const performance = isRecord(usage.performance) ? usage.performance : null;
  const usageComplete = typeof performance?.usage_complete === 'boolean'
    ? performance.usage_complete : null;
  const tokenCount = usageComplete === false ? positiveMetricNumber : metricNumber;
  const inputTokens = performance
    ? tokenCount(performance.input_tokens)
    : positiveMetricNumber(usage.input_tokens);
  const outputTokens = performance
    ? tokenCount(performance.output_tokens)
    : positiveMetricNumber(usage.output_tokens);
  const durationSeconds = elapsedSeconds(run.started_at, run.finished_at);
  const modelCalls = metricNumber(performance?.model_calls);
  if (inputTokens == null && outputTokens == null && durationSeconds == null
    && modelCalls == null && usageComplete !== false) return null;
  const serverTiming = performance?.timing_source === 'server';
  return {
    inputTokens,
    outputTokens,
    inputEstimated: performance?.input_tokens_estimated === true,
    outputEstimated: performance?.output_tokens_estimated === true,
    promptRate: serverTiming ? metricNumber(performance.prompt_tokens_per_second) : null,
    generationRate: serverTiming ? metricNumber(performance.generation_tokens_per_second) : null,
    durationSeconds,
    modelCalls,
    usageComplete,
  };
}

function latestUsageUpdate(
  events: readonly RunStreamEvent[],
): Record<string, unknown> | null {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event.event_type === 'usage.updated' && isRecord(event.payload.performance)) {
      return event.payload;
    }
  }
  return null;
}

function TurnMetadata({ metrics }: { metrics: TurnMetrics }) {
  const inputPrefix = metrics.inputEstimated ? '~' : '';
  const outputPrefix = metrics.outputEstimated ? '~' : '';
  const estimated = metrics.inputEstimated || metrics.outputEstimated;
  const details = [
    'Overall',
    metrics.inputTokens != null ? `${inputPrefix}${formatMetric(metrics.inputTokens)} in` : 'Input tokens unavailable',
    metrics.outputTokens != null ? `${outputPrefix}${formatMetric(metrics.outputTokens)} out` : 'Output tokens unavailable',
    metrics.modelCalls != null ? `${formatMetric(metrics.modelCalls)} calls` : null,
    metrics.usageComplete === false ? 'Usage incomplete (known tokens only)' : null,
    metrics.promptRate != null
      ? `${formatRate(metrics.promptRate)} prompt`
      : 'Prompt speed unavailable',
    metrics.generationRate != null
      ? `${formatRate(metrics.generationRate)} generation`
      : 'Generation speed unavailable',
    metrics.durationSeconds != null ? `${formatDuration(metrics.durationSeconds)} total` : null,
  ].filter((value): value is string => value !== null);
  return (
    <div
      className="turn-metadata"
      title={'Overall run usage across MAIN, delegates, retries, and compaction; delegated usage is already included. '
        + 'Speeds are server active-time weighted averages over timed tokens, not wall-clock rates. '
        + (estimated ? 'Token counts prefixed with ~ are historical estimates.' : '')}
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
