import { checkIdeaAgainstLiteratureHref, type AskContext } from '../../app/ask';
import { IDEA_ACTIONS, ideaActionDraft, readIdeaDraft, recalledIdea, saveIdeaDraft } from './ideas';
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
import { Icon, type IconName } from '../../shared/components/Icons';
import { MarkdownViewer } from '../../shared/components/MarkdownViewer';
import { ErrorNotice, IconButton, Loading } from '../../shared/components/Ui';
import {
  providersApi,
  type Provider,
  type Settings,
} from '../providers/api';
import {
  chatApi,
  type Conversation,
  type ConversationAttachment,
  type ConversationDetail,
  type ModelReference,
  type PromptSnapshot,
  type ResponseEffort,
  type Run,
  type SteeringMessage,
} from './api';
import {
  ChatModelPicker,
  preferredChatModel,
  resolveModelReference,
} from './ChatModelPicker';
import {
  reasoningEffortsForModel,
  ReasoningEffortSelect,
  useModelReasoningEffort,
} from './ReasoningEffortSelect';
import {
  applyChatStreamEvent,
  emptyChatStream,
  mergeStreamEvents,
  restoreChatStream,
  type ChatStreamState,
} from './chatStream';
import {
  buildTurnTimeline,
  describeLiveActivity,
  emptyTurnTimeline,
  traceSteps,
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
import { buildSessionObservability } from './sessionObservability';
import { buildRunStages } from './runStages';
import { RunActivity } from './RunActivity';
import { SessionOverview } from './SessionMonitor';
import { TrackChatSelector } from '../tracks/TrackChatSelector';
import { Link, useNavigate } from '../../app/router';
import {
  contextualDraftKey,
  conversationDraftKey,
  forgetChatDraft,
  fullChatDraftHref,
  lastChatDraftKey,
  readChatDraft,
  saveChatDraft,
  type ChatDraft,
} from './chatDrafts';

const SUGGESTIONS = [
  'Help me think through an idea.',
  'Explain a concept in simple terms.',
  'Compare the evidence in my local files.',
  'Look up a question on the web and discuss what you find.',
];
const SUGGESTION_ICONS: IconName[] = ['bulb', 'sparkle', 'papers', 'globe'];
const COMMON_CONTEXT_WINDOWS = [8_192, 16_384, 32_768, 65_536, 131_072, 262_144];
const MAX_ATTACHMENTS = 10;
const OVERLAY_WIDTH = 1_100;
const RESPONSE_EFFORTS: { value: ResponseEffort; label: string; description: string }[] = [
  {
    value: 'auto',
    label: 'Auto',
    description: 'Match the effort to your request: chat, look up sources, or plan substantial work.',
  },
  {
    value: 'quick',
    label: 'Quick',
    description: 'Keep the scope small, using local files, attachments, or the web as needed.',
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
const ACTIVITY_WIDTH = { value: 380, min: 300, max: 760 };

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

export function ResearchChatPage({ embedded = false, draft, autoSend = false, readingContext: initialReading, intent, initialTrack, ideaObjective, ideaContext, onHandoffChange }: {
  embedded?: boolean; draft?: string; autoSend?: boolean; readingContext?: AskContext; intent?: 'chat' | 'ideation';
  initialTrack?: string; ideaObjective?: string; ideaContext?: string;
  onHandoffChange?: (href: string) => void;
} = {}) {
  const navigate = useNavigate();
  const [initialDraft] = useState(
    () => embedded ? draft?.trim() ?? '' : new URLSearchParams(window.location.search).get('research')?.trim() ?? '',
  );
  const [initialTrackId] = useState(() => initialTrack ?? (embedded ? '' : new URLSearchParams(window.location.search).get('track') ?? ''));
  const [initialReadingContext] = useState<AskContext | null>(() => {
    if (initialReading || embedded) return initialReading ?? null;
    try {
      const value = JSON.parse(new URLSearchParams(window.location.search).get('reading') ?? 'null') as AskContext | null;
      return value?.document_id && value.title ? value : null;
    } catch {
      return null;
    }
  });
  const [initialSession] = useState(() => {
    const params = new URLSearchParams(window.location.search);
    const conversationId = embedded ? null : params.get('conversation');
    const explicit = initialDraft || initialReadingContext || initialTrackId || intent === 'ideation'
      || (!embedded && params.get('intent') === 'ideation');
    const key = (!embedded ? params.get('draft') : null)
      ?? (conversationId ? conversationDraftKey(conversationId) : null)
      ?? (explicit ? contextualDraftKey([initialDraft, initialReadingContext, initialTrackId, intent ?? params.get('intent')]) : lastChatDraftKey(embedded ? 'assistant' : 'full'))
      ?? 'new';
    const saved = readChatDraft(key);
    return { key, saved, conversationId: conversationId ?? saved?.conversationId ?? null, explicit: Boolean(explicit) };
  });
  const [draftKey, setDraftKey] = useState(initialSession.key);
  const [initialConversationId] = useState(initialSession.conversationId);
  const [trackId, setTrackId] = useState(initialSession.saved?.trackId ?? initialTrackId);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [current, setCurrent] = useState<ConversationDetail | null>(null);
  const [modelReference, setModelReference] = useState<ModelReference>(initialSession.saved?.modelReference ?? {});
  const [preferredModelReference, setPreferredModelReference] = useState<ModelReference>({});
  const [contextWindowTokens, setContextWindowTokens] = useState(32_768);
  const [webEnabled, setWebEnabled] = useState(initialSession.saved?.webEnabled ?? true);
  const [responseEffort, setResponseEffort] = useState<ResponseEffort>(initialSession.saved?.responseEffort ?? 'auto');
  const [turnIntent, setTurnIntent] = useState<'chat' | 'ideation'>(() =>
    initialSession.saved?.intent ?? ((intent ?? (!embedded ? new URLSearchParams(window.location.search).get('intent') : null)) === 'ideation' ? 'ideation' : 'chat'));
  const [run, setRun] = useState<Run | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [historyState, setHistoryState] = useState<'ready' | 'loading' | 'unavailable'>('ready');
  const [stream, setStream] = useState<ChatStreamState>(emptyChatStream);
  const [optimisticUser, setOptimisticUser] = useState<string | null>(null);
  const [steeringMessages, setSteeringMessages] = useState<
    (Omit<SteeringMessage, 'status'> & { status: 'queued' | 'applied' })[]
  >([]);
  const [content, setContentState] = useState(initialSession.saved?.content ?? initialDraft);
  const contentVersionRef = useRef(initialSession.saved?.contentVersion ?? 0);
  const setContent = (value: string) => {
    contentVersionRef.current += 1;
    setContentState(value);
  };
  const [selectedIdea, setSelectedIdea] = useState(initialSession.saved?.selectedIdea ?? '');
  const [otherIdea, setOtherIdea] = useState(initialSession.saved?.otherIdea ?? '');
  const [attachments, setAttachments] = useState<ConversationAttachment[]>(initialSession.saved?.attachments ?? []);
  const [reading, setReading] = useState<AskContext | null>(initialSession.saved?.reading ?? initialReadingContext);
  const [ideaReadingEvidence] = useState(() => reading
    ? `${reading.title}${reading.page ? `, page ${reading.page}` : ''}\n${reading.selection ?? ''}` : '');
  const [uploadingNames, setUploadingNames] = useState<string[]>([]);
  const [attachmentError, setAttachmentError] = useState<unknown>(null);
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [sending, setSending] = useState(false);
  const [queueingSteering, setQueueingSteering] = useState(false);
  const [stopping, setStopping] = useState<'stop' | 'answer' | null>(null);
  const [pinnedToBottom, setPinnedToBottom] = useState(true);
  const [sidebarOpen, setSidebarOpen] = useState(
    () => !embedded && (typeof window === 'undefined' || window.innerWidth > OVERLAY_WIDTH),
  );
  const [activityOpen, setActivityOpen] = useState(false);
  const [focusMode, setFocusMode] = useState(false);
  const [listWidth, setListWidth] = useState(() => readStoredWidth(LIST_WIDTH_KEY, LIST_WIDTH));
  const [activityWidth, setActivityWidth] = useState(
    () => readStoredWidth(ACTIVITY_WIDTH_KEY, ACTIVITY_WIDTH),
  );
  const [resizing, setResizing] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [failedDraft, setFailedDraft] = useState<string | null>(initialSession.saved?.failedDraft ?? null);
  const pageRef = useRef<HTMLDivElement>(null);
  const messageListRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const optionsRef = useRef<HTMLDetailsElement>(null);
  const attachmentInputRef = useRef<HTMLInputElement>(null);
  const uploadControllerRef = useRef<AbortController | null>(null);
  const activityToggleRef = useRef<HTMLButtonElement>(null);
  const moreMenuRef = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    const close = (event: Event) => {
      const menu = moreMenuRef.current;
      if (!menu?.open) return;
      if (event instanceof KeyboardEvent ? event.key === 'Escape' : !menu.contains(event.target as Node)) {
        menu.open = false;
      }
    };
    document.addEventListener('pointerdown', close);
    document.addEventListener('keydown', close);
    return () => {
      document.removeEventListener('pointerdown', close);
      document.removeEventListener('keydown', close);
    };
  }, []);
  const openRequestRef = useRef(0);
  const runLifecycleRef = useRef(0);
  const steeringRequestRef = useRef(0);
  const mountedRef = useRef(true);
  const activeRunRef = useRef(run?.id);
  activeRunRef.current = run?.id;
  const steerableRunRef = useRef(false);
  steerableRunRef.current = Boolean(run && ['pending', 'running'].includes(run.status));
  const activeConversationRef = useRef(current?.id);
  activeConversationRef.current = current?.id;
  const pendingDraftRef = useRef<string | null>(null);
  const initialOpenedRef = useRef(false);
  const bootstrapRef = useRef<{ conversations?: Conversation[]; providers?: Provider[]; settings?: Settings }>({});
  const draftSnapshot: ChatDraft = {
    conversationId: current?.id ?? (loading ? initialConversationId : null),
    content: content || pendingDraftRef.current || '',
    attachments, reading, selectedIdea, otherIdea, intent: turnIntent,
    trackId, modelReference, responseEffort, webEnabled, failedDraft, contentVersion: contentVersionRef.current,
  };
  const snapshotRef = useRef(draftSnapshot);
  snapshotRef.current = draftSnapshot;
  const persistDraft = () => {
    saveChatDraft(draftKey, snapshotRef.current, embedded ? 'assistant' : 'full');
    if (snapshotRef.current.conversationId) {
      saveChatDraft(conversationDraftKey(snapshotRef.current.conversationId), snapshotRef.current);
    }
  };
  useEffect(() => {
    if (loading || historyState === 'loading') return;
    persistDraft();
    onHandoffChange?.(fullChatDraftHref(draftKey, current?.id));
  }, [draftKey, content, attachments, reading, selectedIdea, otherIdea, turnIntent, trackId, modelReference, responseEffort, webEnabled, failedDraft, current?.id, loading, historyState, onHandoffChange]);
  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);
  useEffect(() => {
    steeringRequestRef.current += 1;
    setQueueingSteering(false);
  }, [run?.id]);
  const effectiveModelReference = useMemo(
    () => resolveModelReference(modelReference, settings),
    [modelReference.provider_profile_id, modelReference.model, settings],
  );
  const supportedReasoningEfforts = useMemo(
    () => reasoningEffortsForModel(providers, effectiveModelReference),
    [providers, effectiveModelReference.provider_profile_id, effectiveModelReference.model],
  );
  const [reasoningEffort, selectReasoningEffort] =
    useModelReasoningEffort(effectiveModelReference, supportedReasoningEfforts);
  const showSidebar = sidebarOpen && !focusMode;
  const uploading = uploadingNames.length > 0;

  const clearAttachments = () => {
    uploadControllerRef.current?.abort();
    uploadControllerRef.current = null;
    setAttachments([]);
    setUploadingNames([]);
    setAttachmentError(null);
  };

  useEffect(() => () => {
    uploadControllerRef.current?.abort();
  }, []);

  useEffect(() => {
    const dismissOptions = (event: PointerEvent) => {
      const options = optionsRef.current;
      if (options?.open && event.target instanceof Node && !options.contains(event.target)) {
        options.open = false;
      }
    };
    document.addEventListener('pointerdown', dismissOptions, true);
    return () => document.removeEventListener('pointerdown', dismissOptions, true);
  }, []);

  const uploadAttachments = async (files: File[]) => {
    if (
      !files.length || uploadControllerRef.current || sending
      || (run && !isTerminalRun(run)) || historyState === 'loading'
    ) return;
    setAttachmentError(null);
    if (attachments.length + files.length > MAX_ATTACHMENTS) {
      setAttachmentError(new Error(`Attach up to ${MAX_ATTACHMENTS} files per message.`));
      return;
    }
    const unsupported = files.find((file) => !/\.(md|txt|pdf)$/i.test(file.name));
    if (unsupported) {
      setAttachmentError(new Error(`${unsupported.name}: choose a .md, .txt, or .pdf file.`));
      return;
    }
    const request = openRequestRef.current;
    const controller = new AbortController();
    uploadControllerRef.current = controller;
    const isCurrent = () => (
      request === openRequestRef.current
      && uploadControllerRef.current === controller
      && !controller.signal.aborted
    );
    setUploadingNames(files.map((file) => file.name));
    const errors: string[] = [];
    try {
      for (const file of files) {
        try {
          const attachment = await chatApi.uploadAttachment(file, controller.signal);
          if (!isCurrent()) return;
          setAttachments((previous) => [...previous, attachment]);
        } catch (nextError) {
          if (!isCurrent()) return;
          errors.push(`${file.name}: ${nextError instanceof Error ? nextError.message : 'Upload failed'}`);
        }
        if (!isCurrent()) return;
        setUploadingNames((names) => names.slice(1));
      }
    } finally {
      if (isCurrent()) {
        uploadControllerRef.current = null;
        setUploadingNames([]);
        if (errors.length) setAttachmentError(new Error(errors.join('\n')));
      }
    }
  };

  const closeActivity = () => {
    setActivityOpen(false);
    activityToggleRef.current?.focus();
  };

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.altKey && event.shiftKey && event.code === 'KeyF') {
        event.preventDefault();
        setFocusMode((value) => !value);
        setActivityOpen(false);
        composerRef.current?.focus();
      } else if (event.altKey && event.shiftKey && event.code === 'KeyA') {
        event.preventDefault();
        setActivityOpen((value) => !value);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);

  const refreshList = () => chatApi.list().then(setConversations).catch((nextError) => {
    if (mountedRef.current) setError(nextError);
  });
  const open = async (
    id: string,
    contextProviders = providers,
    contextSettings: Settings | null = settings,
    keepDraft = false,
  ) => {
    if (!keepDraft && !loading) persistDraft();
    const request = ++openRequestRef.current;
    setError(null);
    if (!keepDraft) setFailedDraft(null);
    steeringRequestRef.current += 1;
    pendingDraftRef.current = null;
    if (!keepDraft) {
      uploadControllerRef.current?.abort();
      uploadControllerRef.current = null;
      setUploadingNames([]);
      setAttachmentError(null);
    }
    setQueueingSteering(false);
    setHistoryState('loading');
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
    if (conversationResult.status === 'rejected') {
      setHistoryState('unavailable');
      throw conversationResult.reason;
    }
    setHistoryState(runsResult.status === 'fulfilled' ? 'ready' : 'unavailable');
    if (runsResult.status === 'rejected') setError(runsResult.reason);
    const conversation = conversationResult.value;
    const savedIdea = readIdeaDraft(conversation.id) ?? recalledIdea(conversation.items);
    if (!keepDraft) {
      const key = conversationDraftKey(id);
      const saved = readChatDraft(key);
      setDraftKey(key);
      setContent(saved?.content ?? '');
      setAttachments(saved?.attachments ?? []);
      setReading(saved?.reading ?? null);
      setSelectedIdea(saved?.selectedIdea ?? savedIdea);
      setOtherIdea(saved?.otherIdea ?? '');
      setFailedDraft(saved?.failedDraft ?? null);
      setResponseEffort(saved?.responseEffort ?? 'auto');
      setWebEnabled(saved?.webEnabled ?? true);
      setTurnIntent(saved?.intent ?? (savedIdea ? 'ideation' : 'chat'));
    } else if (!selectedIdea && savedIdea) {
      setSelectedIdea(savedIdea);
    }
    if (conversation.kind === 'deep_work') {
      navigate(`/deep-work?conversation=${encodeURIComponent(conversation.id)}`, { replace: true });
      return;
    }
    const allRuns = runsResult.status === 'fulfilled' ? runsResult.value : [];
    const conversationRuns = runsForConversation(allRuns, id);
    const latestRun = conversationRuns[conversationRuns.length - 1] ?? null;
    setCurrent(conversation);
    setTrackId(conversation.track_id ?? '');
    setModelReference(conversation.model_reference);
    setContextWindowTokens(
      latestRun?.context_window_tokens ?? configuredContextWindow(
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
    let cancelled = false;
    const load = async () => {
      const cached = bootstrapRef.current;
      setLoadError(null);
      const results = await Promise.allSettled([
        cached.conversations ? Promise.resolve(cached.conversations) : chatApi.list(),
        cached.providers ? Promise.resolve(cached.providers) : providersApi.list(),
        cached.settings ? Promise.resolve(cached.settings) : providersApi.settings(),
      ]);
      if (cancelled) return;
      const [listResult, providersResult, settingsResult] = results;
      if (listResult.status === 'fulfilled' && !cached.conversations) {
        cached.conversations = listResult.value;
        setConversations(listResult.value);
      }
      if (providersResult.status === 'fulfilled' && !cached.providers) {
        cached.providers = providersResult.value;
        setProviders(providersResult.value);
      }
      if (settingsResult.status === 'fulfilled' && !cached.settings) {
        cached.settings = settingsResult.value;
        setSettings(settingsResult.value);
        const preferredModel = preferredChatModel(settingsResult.value);
        setPreferredModelReference(preferredModel);
        if (!current) {
          if (!initialSession.saved) setModelReference(preferredModel);
          setContextWindowTokens(
            configuredContextWindow(
              cached.providers ?? [],
              initialSession.saved?.modelReference ?? preferredModel,
              settingsResult.value.agent_context_window_tokens,
            ),
          );
        }
      }
      const failures = results.flatMap((result, index) => result.status === 'rejected'
        ? [`${['Chat history', 'Models', 'Settings'][index]}: ${result.reason instanceof Error ? result.reason.message : 'Could not load'}`]
        : []);
      if (failures.length) setLoadError(new Error(failures.join('\n')));
      try {
        if (!initialOpenedRef.current) {
          initialOpenedRef.current = true;
          const firstChat = cached.conversations?.find((item) => item.kind !== 'deep_work');
          const openId = initialConversationId ?? (!initialSession.explicit && !initialSession.saved ? firstChat?.id : undefined);
          if (openId) await open(openId, cached.providers ?? [], cached.settings ?? null, Boolean(initialSession.saved || initialSession.explicit || !loading));
        }
      } catch (nextError) {
        if (!cancelled) {
          initialOpenedRef.current = false;
          setLoadError(nextError);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void load();
    return () => { cancelled = true; };
  }, [loadAttempt]);

  useEffect(() => {
    if (!loading && initialDraft) composerRef.current?.focus();
  }, [loading, initialDraft]);

  useLayoutEffect(() => {
    const list = messageListRef.current;
    if (!list || !pinnedToBottom) return;
    if (!current?.items.length && !optimisticUser && !stream.assistant) return;
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

  /*
   * Auto-grow the composer up to a bounded height so long prompts stay visible.
   * Width changes matter as much as content does — collapsing the chat list or
   * resizing the window reflows the text — so the same measurement runs from a
   * ResizeObserver instead of only when you type.
   */
  useLayoutEffect(() => {
    const textarea = composerRef.current;
    if (!textarea) return;
    let lastWidth = 0;
    const fit = () => {
      textarea.style.height = 'auto';
      textarea.style.height = `${Math.min(textarea.scrollHeight, 240)}px`;
    };
    fit();
    if (typeof ResizeObserver === 'undefined') return;
    // Only width changes reflow the text; reacting to our own height change
    // would loop forever.
    const observer = new ResizeObserver(([entry]) => {
      const width = entry.contentRect.width;
      if (width === lastWidth) return;
      lastWidth = width;
      fit();
    });
    observer.observe(textarea);
    return () => observer.disconnect();
    // `loading` gates whether the composer is mounted at all, so the first
    // measurement has to wait for it rather than only tracking the draft.
  }, [content, loading, settings]);

  useEffect(() => {
    if (!run || stopping) return;

    const lifecycle = ++runLifecycleRef.current;
    let cancelled = false;
    let finalizing = false;
    let polling = false;
    let lastEventAt = Date.now();
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
      let completed = false;
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
        if (!isTerminalRun(nextRun)) {
          setRun(nextRun);
          return;
        }
        completed = true;
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
        finalizing = false;
        if (completed && !cancelled && lifecycle === runLifecycleRef.current) {
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
        if (cancelled || lifecycle !== runLifecycleRef.current) return;
        if (isTerminalRun(nextRun)) await finish(nextRun);
        else {
          setRun(nextRun);
          setRuns((previous) => mergeRun(previous, nextRun));
          setStream((previous) => restoreChatStream(mergeStreamEvents(nextRun.events, previous.events)));
        }
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
        lastEventAt = Date.now();
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
    const watchdog = window.setInterval(() => {
      if (Date.now() - lastEventAt < 10_000) return;
      lastEventAt = Date.now();
      void inspectRun();
    }, 5000);

    return () => {
      if (flushTimer !== undefined) window.clearTimeout(flushTimer);
      flushEvents();
      cancelled = true;
      unsubscribe();
      window.clearInterval(watchdog);
    };
  }, [run?.id, run?.status, current?.id, sending, stopping]);

  const resetThread = (keepDraft = false) => {
    openRequestRef.current += 1;
    steeringRequestRef.current += 1;
    pendingDraftRef.current = null;
    setQueueingSteering(false);
    setError(null);
    if (!keepDraft) setFailedDraft(null);
    if (!keepDraft) {
      clearAttachments();
      setResponseEffort('auto');
    }
    setRun(null);
    setRuns([]);
    setHistoryState('ready');
    setStream(emptyChatStream);
    setOptimisticUser(null);
    setSteeringMessages([]);
    setSending(false);
    setStopping(null);
    setPinnedToBottom(true);
  };

  const create = () => {
    persistDraft();
    if (!current) {
      if (!content && pendingDraftRef.current) setContent(pendingDraftRef.current);
      uploadControllerRef.current?.abort();
      uploadControllerRef.current = null;
      setUploadingNames([]);
      setAttachmentError(null);
      resetThread(true);
      composerRef.current?.focus();
      return;
    }
    setCurrent(null);
    const saved = readChatDraft('new');
    setDraftKey('new');
    setContent(saved?.content ?? '');
    setReading(saved?.reading ?? null);
    setSelectedIdea(saved?.selectedIdea ?? '');
    setOtherIdea(saved?.otherIdea ?? '');
    setTrackId('');
    setModelReference(preferredModelReference);
    setContextWindowTokens(
      configuredContextWindow(
        providers,
        preferredModelReference,
        settings?.agent_context_window_tokens ?? 32_768,
      ),
    );
    resetThread();
    setFailedDraft(saved?.failedDraft ?? null);
    setAttachments(saved?.attachments ?? []);
    setResponseEffort(saved?.responseEffort ?? 'auto');
    setWebEnabled(saved?.webEnabled ?? true);
    setTurnIntent(saved?.intent ?? 'chat');
    composerRef.current?.focus();
  };

  const selectModel = (reference: ModelReference) => {
    if (reference.provider_profile_id === modelReference.provider_profile_id && reference.model === modelReference.model) return;
    if (current && !window.confirm(
      'Changing the model starts a new chat without the existing transcript. Your current chat stays saved. Your unsent message, paper context and attachments will move to the new chat. Continue?',
    )) return;
    persistDraft();
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
      setDraftKey(contextualDraftKey(['model-change', current.id, reference]));
      resetThread(true);
    }
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
      forgetChatDraft(conversationDraftKey(id));
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
    const draft = override ?? content;
    const consumesDraft = override === undefined || override === content;
    const submitted = draft.trim();
    if (!submitted || uploadControllerRef.current || historyState === 'loading') return;
    optionsRef.current?.removeAttribute('open');
    if (sending) {
      if (
        !run
        || !['pending', 'running'].includes(run.status)
        || queueingSteering
        || attachments.length
        || reading
      ) return;
      const request = openRequestRef.current;
      const steeringRequest = ++steeringRequestRef.current;
      const runId = run.id;
      const conversationId = current?.id;
      const isCurrent = () => mountedRef.current
        && request === openRequestRef.current
        && steeringRequest === steeringRequestRef.current
        && activeRunRef.current === runId
        && steerableRunRef.current
        && activeConversationRef.current === conversationId;
      setQueueingSteering(true);
      setError(null);
      if (consumesDraft) setContent('');
      const submittedVersion = contentVersionRef.current;
      try {
        const message = await chatApi.steer(runId, submitted);
        if (!isCurrent()) return;
        setSteeringMessages((messages) =>
          messages.some((item) => item.id === message.id)
            ? messages
            : [...messages, { ...message, status: 'queued' }]
        );
        setPinnedToBottom(true);
      } catch (nextError) {
        if (!isCurrent()) return;
        if (consumesDraft && contentVersionRef.current === submittedVersion) setContent(draft);
        else setFailedDraft(draft);
        setError(nextError);
      } finally {
        if (isCurrent()) {
          setQueueingSteering(false);
          window.setTimeout(() => composerRef.current?.focus(), 0);
        }
      }
      return;
    }
    const request = openRequestRef.current;
    const key = draftKey;
    const attachmentPaths = attachments.map((attachment) => attachment.path);
    setSending(true);
    setError(null);
    pendingDraftRef.current = consumesDraft ? draft : null;
    if (consumesDraft) setContent('');
    const submittedVersion = contentVersionRef.current;
    setOptimisticUser(submitted);
    setRun(null);
    setStream(emptyChatStream);
    setPinnedToBottom(true);
    try {
      let conversation = current;
      if (!conversation) {
        const created = await chatApi.create(modelReference, trackId || undefined);
        const saved = readChatDraft(key);
        if (saved) saveChatDraft(key, { ...saved, conversationId: created.id });
        if (!mountedRef.current || request !== openRequestRef.current) return;
        conversation = await chatApi.get(created.id);
        if (request !== openRequestRef.current) return;
        setCurrent(conversation);
        setModelReference(conversation.model_reference);
        void refreshList();
      }
      const response = await chatApi.send(
        conversation.id,
        submitted,
        {
          reasoning_effort: reasoningEffort ?? undefined,
          web_enabled: webEnabled,
          response_effort: responseEffort,
          turn_intent: turnIntent,
          context_window_tokens: contextWindowTokens,
          attachment_paths: attachmentPaths,
          ...(reading ? { reading_context: { document_id: reading.document_id, page: reading.page ?? null, selection: reading.selection ?? null } } : {}),
        },
      );
      for (const savedKey of [key, conversationDraftKey(conversation.id)]) {
        const saved = readChatDraft(savedKey);
        if (saved?.contentVersion === submittedVersion) saveChatDraft(savedKey, {
          ...saved, conversationId: conversation.id, content: consumesDraft ? '' : saved.content,
          attachments: [], reading: null, responseEffort: 'auto',
        });
      }
      if (!mountedRef.current || request !== openRequestRef.current) return;
      pendingDraftRef.current = null;
      if (turnIntent === 'ideation') saveIdeaDraft(conversation.id, selectedIdea);
      setResponseEffort('auto');
      setAttachments([]);
      setReading(null);
      setAttachmentError(null);
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
      if (!mountedRef.current || request !== openRequestRef.current) return;
      pendingDraftRef.current = null;
      if (consumesDraft && contentVersionRef.current === submittedVersion) setContent(draft);
      else setFailedDraft(draft);
      setOptimisticUser(null);
      setSending(false);
      setError(nextError);
    }
  };

  const autoSent = useRef(false);
  useEffect(() => {
    if (!autoSend || autoSent.current || loading || !initialDraft) return;
    autoSent.current = true;
    void send(initialDraft);
    // send reads the latest render's state; it only needs to fire once loading settles.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoSend, loading, initialDraft]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    if (!needle) return conversations;
    return conversations.filter((conversation) =>
      `${conversation.title} ${conversation.last_message_preview}`
        .toLocaleLowerCase()
        .includes(needle),
    );
  }, [conversations, query]);
  const groupedConversations = useMemo(() => groupByRecency(filtered), [filtered]);

  const observedRuns = useMemo(() => {
    const candidates = run ? mergeRun(runs, run) : runs;
    return candidates.map((candidate) => {
      if (candidate.id !== run?.id) return candidate;
      return { ...candidate, events: mergeStreamEvents(candidate.events, stream.events) };
    });
  }, [runs, run, stream.events]);
  // Replay and live events share one trace; root row counts cannot measure nested progress.
  const timelinesByRun = useMemo(() => {
    const timelines = new Map<string, TurnTimeline>();
    for (const candidate of observedRuns) {
      timelines.set(
        candidate.id,
        buildTurnTimeline(candidate.events, { settled: isTerminalRun(candidate) }),
      );
    }
    return timelines;
  }, [observedRuns]);
  const timelineFor = (candidate: Run): TurnTimeline => timelinesByRun.get(candidate.id) ?? emptyTurnTimeline;
  const liveTimeline = run ? timelineFor(run) : emptyTurnTimeline;
  const activityTimelines = observedRuns
    .map((candidate, index) => ({
      id: candidate.id,
      label: `Run ${index + 1}`,
      timeline: timelineFor(candidate),
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
  const sessionSummary = useMemo(() => buildSessionObservability(observedRuns), [observedRuns]);
  const stagesByRun = useMemo(
    () => new Map(observedRuns.map((candidate) => [candidate.id, buildRunStages(candidate)])),
    [observedRuns],
  );
  const renderActivity = (candidate: Run) => (
    <RunActivity
      stages={stagesByRun.get(candidate.id) ?? []}
      liveThought={runActive && candidate.id === run?.id ? latestReasoning(liveTimeline) : null}
      workPlan={sessionSummary.runs.find((summary) => summary.id === candidate.id)?.workPlan}
      onOpenActivity={() => setActivityOpen(true)}
    />
  );

  // Every run is anchored, so a turn without tools still keeps its trace in place.
  const reasoningAnchors = useMemo(
    () => anchorRunsToItems(current?.items ?? [], runs),
    [current?.items, runs],
  );
  const recoveredRunResponses = useMemo(
    () => missingRunResponsesByUserIndex(current?.items ?? [], runs),
    [current?.items, runs],
  );

  const retryLoading = () => setLoadAttempt((attempt) => attempt + 1);
  if (loading) return <Loading label="Loading chats…" />;
  if (!settings) return (
    <section className="chat-page">
      <ErrorNotice error={loadError ?? new Error('Chat settings could not be loaded.')} />
      <button type="button" className="button" onClick={retryLoading}>Retry loading chats</button>
      <Link className="button" to="/settings">Open settings</Link>
    </section>
  );

  const hasTranscript = Boolean(current?.items.length || run || optimisticUser);

  /** Re-asking is the only way to redo a turn, since the transcript itself is append-only. */
  const retry = (prompt: string) => {
    if (sending || !prompt.trim()) return;
    void send(prompt);
  };

  return (
    <div
      ref={pageRef}
      className={`chat-page${focusMode ? ' focus-mode' : ''}${embedded ? ' chat-embedded' : ''}`}
      onKeyDown={(event) => {
        if (event.key !== 'Escape') return;
        const popover = event.currentTarget.querySelector<HTMLDetailsElement>('.composer-options[open]');
        if (popover) {
          popover.open = false;
          popover.querySelector('summary')?.focus();
        } else if (activityOpen) {
          closeActivity();
        } else if (showSidebar && window.innerWidth <= OVERLAY_WIDTH) {
          setSidebarOpen(false);
        }
      }}
    >
      {error ? <ErrorNotice error={error} /> : null}
      {failedDraft ? (
        <details className="notice">
          <summary>Your earlier message was not sent. Your newer draft is unchanged.</summary>
          <pre>{failedDraft}</pre>
          <button type="button" className="button" onClick={() => {
            setContent(content ? `${failedDraft}\n\n${content}` : failedDraft);
            setFailedDraft(null);
          }}>Add unsent message to draft</button>
        </details>
      ) : null}
      {loadError ? (
        <div>
          <ErrorNotice error={loadError} />
          <button type="button" className="button" onClick={retryLoading}>Retry loading chats</button>
        </div>
      ) : null}
      <div
        className={`chat-layout${showSidebar ? '' : ' collapsed'}${activityOpen ? ' activity-open' : ''}${resizing ? ' resizing' : ''}`}
        style={{
          '--chat-list-width': `${listWidth}px`,
          '--chat-activity-width': `${activityWidth}px`,
        } as CSSProperties}
      >
        {showSidebar || activityOpen ? (
          <button
            type="button"
            className="chat-pane-backdrop"
            aria-label="Close side panels"
            onClick={() => {
              setSidebarOpen(false);
              closeActivity();
            }}
          />
        ) : null}
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
            {groupedConversations.map((group) => (
              <section className="conversation-group" key={group.label} aria-label={group.label}>
                <h2 className="conversation-group-label">{group.label}</h2>
                {group.items.map((conversation) => (
                  <div
                    className={current?.id === conversation.id ? 'conversation active' : 'conversation'}
                    key={conversation.id}
                  >
                    <button
                      type="button"
                      className="conversation-open"
                      aria-current={current?.id === conversation.id ? 'page' : undefined}
                      title={conversation.title}
                      onClick={() => {
                        void open(conversation.id).catch(setError);
                        if (window.innerWidth <= OVERLAY_WIDTH) setSidebarOpen(false);
                      }}
                    >
                      <strong>{conversation.title}</strong>
                      <span className="conversation-time">{relativeTime(conversation.updated_at)}</span>
                    </button>
                    <IconButton
                      icon="trash"
                      tone="danger"
                      rowAction
                      className="conversation-delete"
                      label={`Delete ${conversation.title}`}
                      disabled={current?.id === conversation.id && sending}
                      onClick={() => void remove(conversation.id)}
                    />
                  </div>
                ))}
              </section>
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
              aria-label={showSidebar ? 'Hide chat list' : 'Show chat list'}
              aria-expanded={showSidebar}
              title={showSidebar ? 'Hide chat list' : 'Show chat list'}
              onClick={() => {
                setSidebarOpen(!showSidebar);
                if (focusMode) setFocusMode(false);
              }}
            >
              <Icon name="sidebar" size={16} />
            </button>
            {!showSidebar ? (
              <button type="button" className="chat-list-toggle" aria-label="Start new chat" title="New chat" onClick={create}>
                <Icon name="plus" size={16} />
              </button>
            ) : null}
            <h1>{current?.title ?? 'New chat'}</h1>
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
            <details className="chat-more" ref={moreMenuRef}>
              <summary aria-label="More chat options" title="More options">
                <Icon name="more" size={16} />
              </summary>
              <div className="chat-more-menu">
                <TrackChatSelector conversationId={current?.id} trackId={trackId} disabled={sending || historyState === 'loading'}
                  onError={setError} onChange={(id) => {
                    setTrackId(id);
                    setCurrent((value) => value ? { ...value, track_id: id || null } : value);
                    void refreshList();
                  }} />
                <Link className="chat-more-item" to={`/deep-work?${new URLSearchParams({
                  ...(current ? { conversation: current.id } : {}),
                  ...(trackId ? { track: trackId } : {}),
                })}`}><Icon name="runs" size={15} />Open in Deep Work</Link>
                <button
                  type="button"
                  className={`chat-more-item focus-toggle${focusMode ? ' active' : ''}`}
                  aria-label={focusMode ? 'Exit focus mode' : 'Enter focus mode'}
                  aria-pressed={focusMode}
                  title="Focus mode (Alt+Shift+F)"
                  onClick={(event) => {
                    event.currentTarget.closest('details')?.removeAttribute('open');
                    setFocusMode((value) => !value);
                    setActivityOpen(false);
                    composerRef.current?.focus();
                  }}
                >
                  <Icon name="expand" size={15} />
                  <span>{focusMode ? 'Exit focus mode' : 'Focus mode'}</span>
                  <kbd>Alt+Shift+F</kbd>
                </button>
              </div>
            </details>
            <button
              ref={activityToggleRef}
              type="button"
              className={`activity-toggle${activityOpen ? ' active' : ''}`}
              aria-label={activityOpen ? 'Hide run activity' : 'Show run activity'}
              aria-expanded={activityOpen}
              title="Session usage, workers, and full trace (Alt+Shift+A)"
              onClick={() => setActivityOpen((value) => !value)}
            >
              <Icon name="runs" size={15} />
              <span>Observe</span>
              {activityCount ? <span className="count-badge">{activityCount}</span> : null}
            </button>
          </header>
          {historyState !== 'ready' ? (
            <div className="session-status-strip" role="status">
              {historyState === 'loading' ? 'Loading session activity...' : 'Session metrics unavailable. History could not be loaded.'}
              {historyState === 'unavailable' && current ? (
                <button type="button" className="session-status-open" onClick={() => {
                  setError(null);
                  void open(current.id).catch(setError);
                }}>Retry history</button>
              ) : null}
            </div>
          ) : null}

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
                  <span className="chat-welcome-mark" aria-hidden="true">
                    <Icon name="sparkle" size={26} />
                  </span>
                  <h2>What would you like to explore?</h2>
                  <p>
                    Chat, think through ideas, or look things up in your local files and on the web.
                    Ask when you want papers or notes saved.
                  </p>
                  <div className="suggestion-grid">
                    {SUGGESTIONS.map((suggestion, index) => (
                      <button
                        type="button"
                        className="suggestion"
                        key={suggestion}
                        disabled={sending || uploading}
                        onClick={() => {
                          setContent(suggestion);
                          composerRef.current?.focus();
                        }}
                      >
                        <Icon name={SUGGESTION_ICONS[index] ?? 'sparkle'} size={17} className="suggestion-icon" />
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
                      canRetry={!sending && !uploading}
                    />
                    {recoveredRun ? renderActivity(recoveredRun) : null}
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
                        canRetry={!sending && !uploading}
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
              {run && !reasoningAnchors.anchored.has(run.id) ? renderActivity(run) : null}
              {streamingTimeline ? (
                <details className="research-disclosure live-reasoning">
                  <summary>Live reasoning</summary>
                  <TurnTimelineView timeline={streamingTimeline} />
                </details>
              ) : null}
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
              {run?.error ? (
                <div className="notice error chat-run-error" role="alert">
                  <strong>This run needs attention.</strong>
                  <span>Your conversation is preserved. Review the failure before continuing.</span>
                  <details>
                    <summary>Failure details</summary>
                    <pre>{run.error}</pre>
                  </details>
                </div>
              ) : null}
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
                {liveActivity ? (
                  <LiveActivityBar
                    key={run?.id ?? 'pending-run'}
                    activity={liveActivity}
                    startedAt={run?.created_at}
                    headlines={traceSteps(liveTimeline).filter((step) => step.kind !== 'reasoning').slice(-3)
                      .map((step) => step.kind === 'tool' ? step.label : step.kind === 'agent' ? step.name : '')}
                    onOpenActivity={activityOpen ? undefined : () => setActivityOpen(true)}
                  />
                ) : null}
                {runActive && reading ? (
                  <p className="composer-reading" role="status">
                    Paper context cannot be added to ongoing work. Wait for this run to finish before sending the paper with your message.
                  </p>
                ) : null}
                {/* One control surface: what you type, what answers, and how you send it. */}
                <div className="composer-box">
                  {reading ? (
                    <div className="composer-reading" aria-label="Reading context">
                      <Icon name="file" size={13} />
                      <span title={reading.title}>{reading.title}{reading.page ? ` ? p. ${reading.page}` : ''}</span>
                      <button type="button" aria-label="Don't use this paper as context" title="Don't use this paper as context" disabled={sending} onClick={() => setReading(null)}>
                        <Icon name="close" size={12} />
                      </button>
                    </div>
                  ) : null}
                  {attachments.length ? (
                    <ul className="composer-attachments" aria-label="Attached files">
                      {attachments.map((attachment, index) => (
                        <li key={`${attachment.path}-${index}`} className="composer-attachment">
                          <Icon name="file" size={14} />
                          <span title={attachment.name}>{attachment.name}</span>
                          <button
                            type="button"
                            aria-label={`Remove ${attachment.name}`}
                            title="Detach from this message; the uploaded file stays in your workspace"
                            disabled={sending}
                            onClick={() => setAttachments((previous) => previous.filter((_, itemIndex) => itemIndex !== index))}
                          >
                            <Icon name="close" size={13} />
                          </button>
                        </li>
                      ))}
                    </ul>
                  ) : null}
                  {uploading ? (
                    <div className="composer-upload-status" role="status">
                      <span className="spinner tiny" aria-hidden="true" />
                      Uploading / preparing: {uploadingNames.join(', ')}. PDFs may need text extraction or OCR.
                    </div>
                  ) : null}
                  {attachmentError ? <ErrorNotice error={attachmentError} /> : null}
                  <textarea
                    ref={composerRef}
                    aria-label={sending ? 'Guide ongoing work' : 'Message'}
                    title="Enter to send; Shift+Enter for a new line"
                    rows={1}
                    value={content}
                    disabled={historyState === 'loading'}
                    onChange={(event) => setContent(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                        event.preventDefault();
                        void send();
                      }
                    }}
                    placeholder={
                      sending
                        ? 'Add a constraint, change direction, or ask it to focus...'
                        : attachments.length
                          ? 'Type what you want to do with these files...'
                          : 'Ask a question, explore an idea, or look something up...'
                    }
                  />
                  <div className="composer-bar">
                    <div className="composer-controls" role="group" aria-label="Message controls">
                      <input
                        ref={attachmentInputRef}
                        type="file"
                        hidden
                        accept=".md,.txt,.pdf"
                        multiple
                        aria-label="Choose attachments"
                        disabled={runActive || uploading || historyState === 'loading' || attachments.length >= MAX_ATTACHMENTS}
                        onChange={(event) => {
                          const files = Array.from(event.currentTarget.files ?? []);
                          event.currentTarget.value = '';
                          void uploadAttachments(files);
                        }}
                      />
                      <button
                        className="composer-icon"
                        type="button"
                        aria-label="Attach file"
                        title={runActive ? 'Attachments are available after this run finishes; steering accepts text only' : 'Attach .md, .txt or .pdf (up to 10 files)'}
                        disabled={runActive || uploading || historyState === 'loading' || attachments.length >= MAX_ATTACHMENTS}
                        onClick={() => attachmentInputRef.current?.click()}
                      >
                        <Icon name="upload" size={16} />
                      </button>
                      <ChatModelPicker
                        providers={providers}
                        settings={settings}
                        value={modelReference}
                        disabled={sending || uploading || historyState === 'loading'}
                        onChange={selectModel}
                      />
                      <label className="response-effort-control">
                        <span>Effort</span>
                        <select
                          aria-label="Response effort"
                          value={responseEffort}
                          disabled={sending || historyState === 'loading'}
                          title={RESPONSE_EFFORTS.find((effort) => effort.value === responseEffort)?.description}
                          onChange={(event) => {
                            const selected = RESPONSE_EFFORTS.find(
                              (effort) => effort.value === event.target.value,
                            );
                            if (selected) setResponseEffort(selected.value);
                          }}
                        >
                          {RESPONSE_EFFORTS.map((effort) => (
                            <option key={effort.value} value={effort.value}>{effort.label}</option>
                          ))}
                        </select>
                      </label>
                      <button
                        className={`composer-capability${webEnabled ? ' active' : ''}`}
                        type="button"
                        aria-label="Toggle web access"
                        aria-pressed={webEnabled}
                        title={webEnabled ? 'Web access enabled' : 'Web access disabled'}
                        disabled={sending}
                        onClick={() => setWebEnabled((enabled) => !enabled)}
                      >
                        <Icon name="globe" size={13} />
                        Web
                      </button>
                      <details className="composer-options" ref={optionsRef}>
                        <summary title="Chat mode, idea tools, reasoning effort and context size">
                          <Icon name="settings" size={13} />
                          Options
                        </summary>
                        <div className="composer-options-popover">
                          <label className="composer-context">
                            <span>Mode</span>
                            <select aria-label="Conversation intent" value={turnIntent} disabled={runActive}
                              onChange={(event) => {
                                setTurnIntent(event.target.value as 'chat' | 'ideation');
                                optionsRef.current?.removeAttribute('open');
                                composerRef.current?.focus();
                              }}>
                              <option value="chat">Chat</option>
                              <option value="ideation">Explore ideas</option>
                            </select>
                          </label>
                          {turnIntent === 'ideation' ? (
                            <div className="composer-ideation-hint" role="note">
                              <strong>Shape a direction</strong>
                              <span>Compare premise, supporting observations, speculation and unknowns. Abstracts and excerpts are not full-paper evidence. Novelty is unverified.</span>
                              <label>Selected idea
                                <textarea rows={2} maxLength={2400} value={selectedIdea} onChange={(event) => {
                                  setSelectedIdea(event.target.value);
                                  if (current) saveIdeaDraft(current.id, event.target.value);
                                }}
                                  placeholder="Paste or describe the idea you want to work on" />
                              </label>
                              <label>Another idea to combine (optional)
                                <input maxLength={2400} value={otherIdea} onChange={(event) => setOtherIdea(event.target.value)}
                                  placeholder="Describe the second direction" />
                              </label>
                              {IDEA_ACTIONS.map((action) => <button key={action}
                                type="button" disabled={!selectedIdea.trim() || sending || (action === 'Combine' && !otherIdea.trim())}
                                onClick={() => {
                                  setContent(ideaActionDraft(action, selectedIdea, otherIdea));
                                  optionsRef.current?.removeAttribute('open');
                                  composerRef.current?.focus();
                                }}>
                                {action}
                              </button>)}
                              {selectedIdea.trim() ? <Link to={checkIdeaAgainstLiteratureHref(selectedIdea,
                                ideaObjective || initialDraft || current?.items.find((item) => item.role === 'user')?.text || '',
                                ideaContext || ideaReadingEvidence)}>Check against literature</Link> : null}
                              <span>Actions prepare editable drafts; Send adds them to this conversation. Literature checking opens a new editable review, never starts one.</span>
                            </div>
                          ) : null}
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
                          <p className="composer-options-help">
                            This chat's context size controls compaction, including its helper model,
                            and overrides provider metadata. Larger context uses more memory; your
                            model server must support the selected size.
                          </p>
                        </div>
                      </details>
                    </div>
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
                          disabled={queueingSteering || uploading || attachments.length > 0 || reading !== null || !content.trim()}
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
                        disabled={sending || uploading || historyState === 'loading' || !content.trim()}
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
          overview={historyState === 'ready'
            ? <SessionOverview summary={sessionSummary} />
            : <p className="session-observability-note">Session history is not available yet.</p>}
          onClose={closeActivity}
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

/** The server appends reader context to the stored turn; show only the question and the paper. */
export function splitReadingContext(text: string): { body: string; readingTitle: string | null } {
  const start = text.indexOf('\n\n<reading-context>');
  if (start < 0) return { body: text, readingTitle: null };
  const title = /\ntitle: (.*)\n/.exec(text.slice(start))?.[1] ?? null;
  return { body: text.slice(0, start), readingTitle: title };
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
  const { body, readingTitle } = splitReadingContext(text);
  // No avatar, no role caption: the shape of the turn already says who is speaking.
  return (
    <article className={`message role-${role} ${className}`.trim()}>
      <div className="message-body">
        {readingTitle ? <small className="message-reading"><Icon name="file" size={12} />{readingTitle}</small> : null}
        <MarkdownViewer content={body} />
        {streaming ? <i className="stream-cursor" aria-hidden="true" /> : null}
        {isAssistant && sources?.length ? <SourceImages sources={sources} /> : null}
        {isAssistant && sources?.length ? <SourceChips sources={sources} /> : null}
      </div>
      <MessageActions
        content={body}
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

/** Most recent reasoning text so a waiting user can follow what the model is weighing. */
function latestReasoning(timeline: TurnTimeline): string | null {
  for (let index = timeline.steps.length - 1; index >= 0; index -= 1) {
    const step = timeline.steps[index];
    if (step.kind === 'reasoning' && step.text.trim()) return step.text.trim();
  }
  return null;
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
    <details className="research-disclosure turn-performance">
      <summary>Performance</summary>
      <div
        className="turn-metadata"
        title={'Overall run usage across MAIN, delegates, retries, and compaction; delegated usage is already included. '
          + 'Speeds are server active-time weighted averages over timed tokens, not wall-clock rates. '
          + (estimated ? 'Token counts prefixed with ~ are historical estimates.' : '')}
        aria-label={`Turn metadata: ${details.join(', ')}`}
      >
        {details.map((detail) => <span key={detail}>{detail}</span>)}
      </div>
    </details>
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

const RECENCY_DAY_MS = 86_400_000;

/** Buckets an already-sorted chat list under Today / Yesterday / ... headings, keeping its order. */
export function groupByRecency<T extends { updated_at: string }>(
  items: T[],
  now: Date = new Date(),
): { label: string; items: T[] }[] {
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const labelFor = (value: string) => {
    const time = new Date(value).getTime();
    if (Number.isNaN(time)) return 'Older';
    if (time >= today) return 'Today';
    if (time >= today - RECENCY_DAY_MS) return 'Yesterday';
    if (time >= today - 7 * RECENCY_DAY_MS) return 'Previous 7 days';
    if (time >= today - 30 * RECENCY_DAY_MS) return 'Previous 30 days';
    return 'Older';
  };
  const groups: { label: string; items: T[] }[] = [];
  for (const item of items) {
    const label = labelFor(item.updated_at);
    const existing = groups.find((group) => group.label === label);
    if (existing) existing.items.push(item);
    else groups.push({ label, items: [item] });
  }
  return groups;
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
