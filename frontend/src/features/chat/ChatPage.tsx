import {
  Fragment,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { subscribeToRun, type RunStreamEvent } from '../../api/events';
import { useLocation, useNavigate } from '../../app/router';
import { copyToClipboard } from '../../shared/clipboard';
import { Icon } from '../../shared/components/Icons';
import { MarkdownViewer } from '../../shared/components/MarkdownViewer';
import { ModelSelect } from '../../shared/components/ModelSelect';
import { EmptyState, ErrorNotice, Loading, StatusPill } from '../../shared/components/Ui';
import { capabilityOptions } from '../providers/ModelDefaultsPanel';
import {
  providersApi,
  type BuiltInSpeechStatus,
  type Provider,
  type Settings,
} from '../providers/api';
import {
  chatApi,
  notifyChatConversationsChanged,
  type Conversation,
  type ConversationDetail,
  type ModelReference,
  type Run,
  type WorkBudget,
  type WorkMode,
} from './api';
import {
  ChatModelPicker,
  modelReferenceLabel,
  preferredChatModel,
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
  emptyTurnTimeline,
  type TimelineSource,
  type TurnTimeline,
} from './chatTimeline';
import { SourceChips, SourceImages, TurnTimelineView } from './TurnTimeline';
import { RunInsightsPanel } from './RunInsightsPanel';
import './chat.css';

const SUGGESTIONS = [
  'Summarise the key findings across the papers in my library.',
  'Compare the methods used in the two most recent papers I added.',
  'What open research questions remain in this area?',
  'Draft research notes with citations for my current topic.',
];
const WORK_MODE_STORAGE_KEY = 'scholarweave:chat-work-mode';
const WORK_BUDGET_STORAGE_KEY = 'scholarweave:chat-work-budget';
const WORK_BUDGET_DETAILS: Record<WorkBudget, string> = {
  low: 'Prioritize about 3 tasks and 3 sources at a time; normal lookups are uncapped.',
  medium: 'Prioritize about 6 tasks and 5 sources at a time; normal lookups are uncapped.',
  high: 'Prioritize about 10 tasks and 8 sources at a time; normal lookups are uncapped.',
};

export const PROVIDER_TRANSCRIPTION_INTERVAL_MS = 750;

interface BuiltInSpeechMessage {
  type: 'ready' | 'partial' | 'final' | 'error';
  text?: string;
  message?: string;
}

export default function ChatPage() {
  const { search } = useLocation();
  const navigate = useNavigate();
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [current, setCurrent] = useState<ConversationDetail | null>(null);
  const [modelReference, setModelReference] = useState<ModelReference>({});
  const [preferredModelReference, setPreferredModelReference] = useState<ModelReference>({});
  const [speechModelReference, setSpeechModelReference] = useState<ModelReference>({});
  const [speechMode, setSpeechMode] = useState<'builtin' | 'provider'>('builtin');
  const [reasoningEffort, setReasoningEffort] = useState<ReasoningEffort | null>(
    readStoredReasoningEffort,
  );
  const [workMode, setWorkMode] = useState<WorkMode>(readStoredWorkMode);
  const [workBudget, setWorkBudget] = useState<WorkBudget>(readStoredWorkBudget);
  const [builtInSpeech, setBuiltInSpeech] = useState<BuiltInSpeechStatus | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [stream, setStream] = useState<ChatStreamState>(emptyChatStream);
  const [optimisticUser, setOptimisticUser] = useState<string | null>(null);
  const [content, setContent] = useState('');
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [recording, setRecording] = useState(false);
  const [startingRecording, setStartingRecording] = useState(false);
  const [transcribing, setTranscribing] = useState(false);
  const [speechPreview, setSpeechPreview] = useState<string | null>(null);
  const [speechFinalFailed, setSpeechFinalFailed] = useState(false);
  const [pinnedToBottom, setPinnedToBottom] = useState(true);
  const [settingsOpen, setSettingsOpen] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const messageListRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const microphoneStreamRef = useRef<MediaStream | null>(null);
  const audioContextRef = useRef<AudioContext | null>(null);
  const audioSourceRef = useRef<MediaStreamAudioSourceNode | null>(null);
  const audioProcessorRef = useRef<ScriptProcessorNode | null>(null);
  const audioChunksRef = useRef<Float32Array[]>([]);
  const speechPreviewTimerRef = useRef<number | null>(null);
  const speechSocketRef = useRef<WebSocket | null>(null);
  const partialTranscriptionBusyRef = useRef(false);
  const transcriptionRequestRef = useRef(0);
  const recordingStartPendingRef = useRef(false);
  const speechMountedRef = useRef(false);
  const openRequestRef = useRef(0);
  const handledRouteRef = useRef<string | null>(null);
  const supportedReasoningEfforts = useMemo(
    () => reasoningEffortsForModel(providers, modelReference),
    [providers, modelReference.provider_profile_id, modelReference.model],
  );

  useEffect(() => {
    if (!settingsOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setSettingsOpen(false);
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [settingsOpen]);

  useEffect(() => {
    if (run?.id) setSettingsOpen(false);
  }, [run?.id]);

  useEffect(() => {
    if (
      reasoningEffort
      && modelReference.provider_profile_id
      && modelReference.model
      && !supportedReasoningEfforts?.includes(reasoningEffort)
    ) {
      setReasoningEffort(null);
      storeReasoningEffort(null);
    }
  }, [reasoningEffort, supportedReasoningEfforts]);

  const refreshList = () =>
    chatApi.list().then((items) => {
      setConversations(items);
      notifyChatConversationsChanged();
      return items;
    });
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
      providersApi.builtInSpeechStatus(),
    ])
      .then(([items, nextProviders, nextSettings, speechStatus]) => {
        setConversations(items);
        setProviders(nextProviders);
        setSettings(nextSettings);
        const preferredModel = preferredChatModel(nextSettings);
        setModelReference(preferredModel);
        setPreferredModelReference(preferredModel);
        setSpeechModelReference(nextSettings.default_model_references.speech ?? {});
        if (
          nextSettings.default_model_references.speech?.provider_profile_id
          && nextSettings.default_model_references.speech?.model
        ) {
          setSpeechMode('provider');
        }
        setBuiltInSpeech(speechStatus);
      })
      .catch(setError)
      .finally(() => setLoading(false));
  }, []);

  const route = new URLSearchParams(search);
  const newChatRequested = route.has('new');
  const requestedConversationId = route.get('conversation');
  const targetConversationId = requestedConversationId ?? conversations[0]?.id;

  useEffect(() => {
    if (loading) return;
    if (newChatRequested) {
      openRequestRef.current += 1;
      setCurrent(null);
      setRun(null);
      setRuns([]);
      setStream(emptyChatStream);
      setOptimisticUser(null);
      setSending(false);
      setPinnedToBottom(true);
      setModelReference(preferredModelReference);
      return;
    }
    if (!targetConversationId || handledRouteRef.current === targetConversationId) {
      handledRouteRef.current = null;
      return;
    }
    if (current?.id !== targetConversationId) {
      void open(targetConversationId).catch(setError);
    }
  }, [loading, newChatRequested, targetConversationId, preferredModelReference]);

  useEffect(() => {
    if (builtInSpeech?.state !== 'installing') return;
    const timer = window.setInterval(() => {
      void providersApi.builtInSpeechStatus()
        .then(setBuiltInSpeech)
        .catch(setError);
    }, 750);
    return () => window.clearInterval(timer);
  }, [builtInSpeech?.state]);

  useEffect(() => {
    speechMountedRef.current = true;
    return () => {
      speechMountedRef.current = false;
      if (speechPreviewTimerRef.current !== null) {
        window.clearInterval(speechPreviewTimerRef.current);
      }
      audioProcessorRef.current?.disconnect();
      audioSourceRef.current?.disconnect();
      speechSocketRef.current?.close();
      void audioContextRef.current?.close();
      microphoneStreamRef.current?.getTracks().forEach((track) => track.stop());
    };
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

  useEffect(() => {
    if (!run || isTerminalRun(run)) setStopping(false);
  }, [run?.id, run?.status]);

  const resetThread = () => {
    openRequestRef.current += 1;
    setRun(null);
    setRuns([]);
    setStream(emptyChatStream);
    setOptimisticUser(null);
    setSending(false);
    setPinnedToBottom(true);
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
      navigate('/?new=1');
    }
  };

  const selectReasoningEffort = (effort: ReasoningEffort | null) => {
    setReasoningEffort(effort);
    storeReasoningEffort(effort);
  };

  const send = async (override?: string) => {
    const submitted = (override ?? content).trim();
    if (!submitted || sending || recording || startingRecording || transcribing || speechPreview !== null) return;
    const request = openRequestRef.current;
    setSending(true);
    setStopping(false);
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
        handledRouteRef.current = conversation.id;
        navigate(`/?conversation=${encodeURIComponent(conversation.id)}`, { replace: true });
        void refreshList();
      }
      const response = await chatApi.send(
        conversation.id,
        submitted,
        reasoningEffort,
        workMode,
        workBudget,
      );
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

  const stopRun = async () => {
    if (!run || isTerminalRun(run) || stopping) return;
    setStopping(true);
    setError(null);
    try {
      const nextRun = await chatApi.cancelRun(run.id);
      setRun(nextRun);
      setRuns((previous) => mergeRun(previous, nextRun));
      if (isTerminalRun(nextRun)) setSending(false);
    } catch (nextError) {
      setStopping(false);
      setError(nextError);
    }
  };

  const transcribeRecording = async (audio: Blob, final: boolean) => {
    if (!final && partialTranscriptionBusyRef.current) return;
    const request = ++transcriptionRequestRef.current;
    if (final) {
      setTranscribing(true);
      setSpeechFinalFailed(false);
      setError(null);
    } else {
      partialTranscriptionBusyRef.current = true;
    }
    try {
      const result = await providersApi.transcribe(audio, speechModelReference);
      if (request === transcriptionRequestRef.current) {
        setSpeechPreview(result.text.trim());
      }
    } catch (nextError) {
      if (final) setSpeechFinalFailed(true);
      setError(nextError);
    } finally {
      if (final) setTranscribing(false);
      else partialTranscriptionBusyRef.current = false;
    }
  };

  const stopRecording = () => {
    if (speechPreviewTimerRef.current !== null) {
      window.clearInterval(speechPreviewTimerRef.current);
      speechPreviewTimerRef.current = null;
    }
    audioProcessorRef.current?.disconnect();
    audioSourceRef.current?.disconnect();
    microphoneStreamRef.current?.getTracks().forEach((track) => track.stop());
    void audioContextRef.current?.close();
    const chunks = audioChunksRef.current;
    const sampleRate = audioContextRef.current?.sampleRate ?? 16_000;
    const speechSocket = speechSocketRef.current;
    audioProcessorRef.current = null;
    audioSourceRef.current = null;
    microphoneStreamRef.current = null;
    audioContextRef.current = null;
    audioChunksRef.current = [];
    setRecording(false);
    if (speechMode === 'builtin' && speechSocket?.readyState === WebSocket.OPEN) {
      try {
        speechSocket.send('finish');
        setTranscribing(true);
      } catch (nextError) {
        setTranscribing(false);
        setSpeechFinalFailed(true);
        setError(nextError);
      }
    } else if (chunks.length) {
      void transcribeRecording(encodeWav(chunks, sampleRate), true);
    }
  };

  const toggleRecording = async () => {
    if (recording) {
      stopRecording();
      return;
    }
    if (recordingStartPendingRef.current) return;
    recordingStartPendingRef.current = true;
    setStartingRecording(true);
    let acquiredStream: MediaStream | null = null;
    let acquiredContext: AudioContext | null = null;
    try {
      setError(null);
      setSpeechPreview('');
      setSpeechFinalFailed(false);
      const microphone = navigator.mediaDevices.getUserMedia({ audio: true });
      const speechWarmup = speechMode === 'builtin'
        ? providersApi.startBuiltInSpeech()
        : Promise.resolve(null);
      const [microphoneResult, warmupResult] = await Promise.allSettled([
        microphone,
        speechWarmup,
      ]);
      if (microphoneResult.status !== 'fulfilled') throw microphoneResult.reason;
      if (warmupResult.status === 'rejected') throw warmupResult.reason;
      const activeStream = microphoneResult.value;
      acquiredStream = activeStream;
      const speechStatus = warmupResult.value;
      if (speechStatus) setBuiltInSpeech(speechStatus);
      if (speechMode === 'builtin') {
        const socket = providersApi.builtInSpeechSocket();
        speechSocketRef.current = socket;
        await new Promise<void>((resolve, reject) => {
          socket.onopen = () => resolve();
          socket.onerror = () => reject(
            new Error('Could not connect to the local Nemotron speech stream.'),
          );
        });
        socket.onmessage = (event) => {
          const message = JSON.parse(String(event.data)) as BuiltInSpeechMessage;
          if (message.type === 'partial' || message.type === 'final') {
            setSpeechPreview(message.text?.trim() ?? '');
          }
          if (message.type === 'final') {
            setTranscribing(false);
            setSpeechFinalFailed(!message.text?.trim());
            socket.close();
          } else if (message.type === 'error') {
            setTranscribing(false);
            setSpeechFinalFailed(true);
            setError(new Error(message.message || 'Nemotron transcription failed.'));
          }
        };
        socket.onclose = () => {
          if (speechSocketRef.current === socket) speechSocketRef.current = null;
        };
      }
      if (!speechMountedRef.current) {
        activeStream.getTracks().forEach((track) => track.stop());
        return;
      }
      const context = new AudioContext({ sampleRate: 16_000 });
      acquiredContext = context;
      const source = context.createMediaStreamSource(activeStream);
      const processor = context.createScriptProcessor(4096, 1, 1);
      microphoneStreamRef.current = activeStream;
      audioContextRef.current = acquiredContext;
      audioSourceRef.current = source;
      audioProcessorRef.current = processor;
      audioChunksRef.current = [];
      processor.onaudioprocess = (event) => {
        const samples = new Float32Array(event.inputBuffer.getChannelData(0));
        const socket = speechSocketRef.current;
        if (speechMode === 'builtin' && socket?.readyState === WebSocket.OPEN) {
          socket.send(encodePcm16(samples, context.sampleRate));
        } else {
          audioChunksRef.current.push(samples);
        }
      };
      source.connect(processor);
      processor.connect(context.destination);
      setRecording(true);
      if (speechMode === 'provider') {
        speechPreviewTimerRef.current = window.setInterval(() => {
          if (!audioChunksRef.current.length) return;
          void transcribeRecording(
            encodeWav(audioChunksRef.current, context.sampleRate),
            false,
          );
        }, PROVIDER_TRANSCRIPTION_INTERVAL_MS);
      }
    } catch (nextError) {
      acquiredStream?.getTracks().forEach((track) => track.stop());
      void acquiredContext?.close();
      microphoneStreamRef.current?.getTracks().forEach((track) => track.stop());
      microphoneStreamRef.current = null;
      audioProcessorRef.current?.disconnect();
      audioSourceRef.current?.disconnect();
      void audioContextRef.current?.close();
      audioProcessorRef.current = null;
      audioSourceRef.current = null;
      audioContextRef.current = null;
      speechSocketRef.current?.close();
      speechSocketRef.current = null;
      audioChunksRef.current = [];
      setRecording(false);
      setSpeechPreview(null);
      setError(nextError);
    } finally {
      recordingStartPendingRef.current = false;
      if (speechMountedRef.current) setStartingRecording(false);
    }
  };

  const acceptSpeechPreview = () => {
    const transcript = speechPreview?.trim();
    if (!transcript) return;
    setContent((currentContent) =>
      [currentContent.trim(), transcript].filter(Boolean).join(' '),
    );
    setSpeechPreview(null);
    window.setTimeout(() => composerRef.current?.focus(), 0);
  };

  const installBuiltInSpeech = async () => {
    setError(null);
    try {
      setBuiltInSpeech(await providersApi.installBuiltInSpeech());
    } catch (nextError) {
      setError(nextError);
    }
  };

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
  const speechOptions = capabilityOptions(providers, 'speech');
  const builtInSpeechReady =
    builtInSpeech?.state === 'ready' || builtInSpeech?.state === 'running';
  const speechModelConfigured = speechMode === 'builtin'
    ? builtInSpeechReady
    : Boolean(speechModelReference.provider_profile_id && speechModelReference.model);
  const speechInstallProgress = builtInSpeech?.total_bytes
    ? Math.min(100, Math.round(100 * builtInSpeech.downloaded_bytes / builtInSpeech.total_bytes))
    : 0;
  const hasTranscript = Boolean(current?.items.length || run || optimisticUser);
  const activeEvents = run && stream.events.length >= run.events.length ? stream.events : run?.events ?? [];
  const activeMetrics = run ? turnMetrics(run, activeEvents) : null;
  const activeTimeline = run ? timelineFor(run) : emptyTurnTimeline;

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
      <div className="chat-layout agent-chat-layout">
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
                {run && !isTerminalRun(run) ? (
                  <button
                    type="button"
                    className="button danger small chat-stop"
                    disabled={stopping}
                    onClick={() => void stopRun()}
                  >
                    {stopping
                      ? <span className="spinner tiny" aria-hidden="true" />
                      : <Icon name="stop" size={13} />}
                    {stopping ? 'Stopping…' : 'Stop'}
                  </button>
                ) : null}
                {activeMetrics?.inputTokens != null ? (
                  <span
                    className="run-context-usage"
                    title="Context tokens consumed by the latest request"
                  >
                    <Icon name="runs" size={13} />
                    {activeMetrics.inputEstimated ? '~' : ''}
                    {formatMetric(activeMetrics.inputTokens)} context
                  </span>
                ) : null}
                {run ? <StatusPill value={run.status} /> : null}
                <button
                  type="button"
                  className="button small chat-new"
                  title="Start a new chat"
                  disabled={!hasTranscript && !current}
                  onClick={() => {
                    setCurrent(null);
                    resetThread();
                    navigate('/?new=1');
                  }}
                >
                  <Icon name="plus" size={14} />
                  <span className="chat-new-label">New chat</span>
                </button>
                <button
                  type="button"
                  className={`button secondary small chat-settings-toggle${settingsOpen ? ' active' : ''}`}
                  aria-expanded={settingsOpen}
                  aria-controls="chat-settings-panel"
                  title="Model and speech settings"
                  onClick={() => setSettingsOpen((value) => !value)}
                >
                  <Icon name="sliders" size={14} />
                  <span className="chat-settings-toggle-label">Settings</span>
                </button>
              </div>
            </div>
          </header>
          <div className="chat-workspace has-side-panel">
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
                          disabled={sending || speechPreview !== null}
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
                    metrics={activeMetrics}
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
                    <div className="composer-actions">
                      <button
                        className={`button secondary icon composer-record${recording ? ' recording' : ''}`}
                        type="button"
                        aria-label={recording ? 'Stop recording and transcribe' : 'Record speech'}
                        title={
                          speechModelConfigured
                            ? recording ? 'Stop and transcribe' : 'Record speech'
                            : 'Choose a speech recognition model'
                        }
                        disabled={sending || startingRecording || transcribing || !speechModelConfigured}
                        onClick={() => void toggleRecording()}
                      >
                        {startingRecording || transcribing
                          ? <span className="spinner tiny" aria-hidden="true" />
                          : <Icon name={recording ? 'stop' : 'microphone'} size={16} />}
                      </button>
                      <button
                        className="button icon composer-send"
                        type="button"
                        aria-label="Send message"
                        disabled={sending || transcribing || speechPreview !== null || !content.trim()}
                        onClick={() => void send()}
                      >
                        {sending
                          ? <span className="spinner tiny" aria-hidden="true" />
                          : <Icon name="arrowRight" size={16} />}
                      </button>
                    </div>
                  </div>
                  {speechPreview !== null ? (
                    <section className="speech-review" aria-live="polite">
                      <div className="speech-review-head">
                        <strong>
                          {recording
                            ? 'Live transcript'
                            : transcribing
                              ? 'Finishing transcript…'
                              : speechFinalFailed ? 'Transcription incomplete' : 'Review transcript'}
                        </strong>
                        {recording ? <span className="recording-dot">Listening</span> : null}
                      </div>
                      <textarea
                        value={speechPreview}
                        rows={3}
                        disabled={recording || transcribing}
                        aria-label="Speech transcript preview"
                        placeholder={recording ? 'Start speaking…' : 'Waiting for transcription…'}
                        onChange={(event) => setSpeechPreview(event.target.value)}
                      />
                      {speechFinalFailed ? (
                        <small className="speech-review-error">
                          The final pass failed, so this partial transcript cannot be accepted. Retry the recording or discard it.
                        </small>
                      ) : null}
                      <div className="button-row speech-review-actions">
                        {recording ? (
                          <button className="button small" type="button" onClick={stopRecording}>
                            <Icon name="stop" size={14} />
                            Stop
                          </button>
                        ) : (
                          <>
                            <button
                              className="button small"
                              type="button"
                              disabled={transcribing || speechFinalFailed || !speechPreview.trim()}
                              onClick={acceptSpeechPreview}
                            >
                              <Icon name="check" size={14} />
                              Use transcript
                            </button>
                            <button
                              className="button secondary small"
                              type="button"
                              disabled={transcribing}
                              onClick={() => {
                                setSpeechPreview(null);
                                void toggleRecording();
                              }}
                            >
                              <Icon name="refresh" size={14} />
                              Retry
                            </button>
                            <button
                              className="button ghost small"
                              type="button"
                              disabled={transcribing}
                              onClick={() => setSpeechPreview(null)}
                            >
                              Discard
                            </button>
                          </>
                        )}
                      </div>
                    </section>
                  ) : null}
                </div>
              </div>
            </div>
            {run ? (
              <RunInsightsPanel
                status={run.status}
                events={activeEvents}
                timeline={activeTimeline}
                metrics={activeMetrics}
                hidden={settingsOpen}
              />
            ) : null}
            <aside
              className="chat-settings"
              id="chat-settings-panel"
              aria-label="Chat model settings"
              hidden={!settingsOpen}
            >
              {settingsOpen ? (
                <div className="chat-settings-content">
                  <header className="chat-settings-head">
                    <div className="chat-settings-heading">
                      <span className="eyebrow">Run configuration</span>
                      <strong>Chat settings</strong>
                    </div>
                    <button
                      type="button"
                      className="button ghost icon chat-settings-close"
                      aria-label="Close chat settings"
                      title="Close"
                      onClick={() => setSettingsOpen(false)}
                    >
                      <Icon name="close" size={15} />
                    </button>
                  </header>
                  <label className="field">
                    Model
                    <ChatModelPicker
                      providers={providers}
                      settings={settings}
                      value={modelReference}
                      disabled={sending}
                      onChange={selectModel}
                    />
                  </label>
                  <ReasoningEffortSelect
                    value={reasoningEffort}
                    supportedEfforts={supportedReasoningEfforts}
                    disabled={sending}
                    onChange={selectReasoningEffort}
                  />
                  <label className="field">
                    Work mode
                    <select
                      value={workMode}
                      disabled={sending}
                      onChange={(event) => {
                        const mode = event.target.value as WorkMode;
                        setWorkMode(mode);
                        window.localStorage.setItem(WORK_MODE_STORAGE_KEY, mode);
                      }}
                    >
                      <option value="direct">Direct</option>
                      <option value="extended">Extended work (sequential)</option>
                    </select>
                    <small>
                      Extended work plans focused tasks, runs each in a fresh context, and combines
                      their stored findings.
                    </small>
                  </label>
                  {workMode === 'extended' ? (
                    <label className="field">
                      Research scope
                      <select
                        aria-label="Extended research scope"
                        value={workBudget}
                        disabled={sending}
                        onChange={(event) => {
                          const budget = event.target.value as WorkBudget;
                          setWorkBudget(budget);
                          window.localStorage.setItem(WORK_BUDGET_STORAGE_KEY, budget);
                        }}
                      >
                        <option value="low">Low</option>
                        <option value="medium">Medium</option>
                        <option value="high">High</option>
                      </select>
                      <small>{WORK_BUDGET_DETAILS[workBudget]}</small>
                    </label>
                  ) : null}
                  <hr />
                  <label className="field">
                    Speech recognition
                    <select
                      className="speech-source-select"
                      aria-label="Speech recognition source"
                      value={speechMode}
                      disabled={recording || startingRecording || transcribing}
                      onChange={(event) => setSpeechMode(event.target.value as 'builtin' | 'provider')}
                    >
                      <option value="builtin">Nemotron English local</option>
                      <option value="provider">Provider model</option>
                    </select>
                  </label>
                  {speechMode === 'builtin' ? (
                    <div className="chat-settings-status">
                      {!builtInSpeech?.available ? (
                        <span title={builtInSpeech?.error ?? undefined}>Unavailable</span>
                      ) : builtInSpeech.state === 'error' ? (
                        <button
                          className="button secondary small"
                          type="button"
                          onClick={() => void installBuiltInSpeech()}
                        >
                          Retry install
                        </button>
                      ) : builtInSpeechReady ? (
                        <span className="speech-ready">Ready</span>
                      ) : builtInSpeech?.state === 'installing' ? (
                        <span>Downloading {speechInstallProgress}%</span>
                      ) : (
                        <button
                          className="button secondary small"
                          type="button"
                          onClick={() => void installBuiltInSpeech()}
                        >
                          Install model
                        </button>
                      )}
                    </div>
                  ) : (
                    <ModelSelect
                      options={speechOptions}
                      value={speechModelReference}
                      disabled={recording || startingRecording || transcribing}
                      placeholder={speechOptions.length ? 'Speech model' : 'No speech models'}
                      onChange={(reference) => setSpeechModelReference(reference)}
                    />
                  )}
                  {startingRecording ? <small>Opening microphone…</small> : null}
                  {recording ? <small>Listening…</small> : null}
                  {transcribing ? <small>Transcribing…</small> : null}
                </div>
              ) : null}
            </aside>
            {!run && !settingsOpen ? (
              <aside className="chat-side-panel-placeholder" aria-hidden="true" />
            ) : null}
          </div>
        </section>
      </div>
    </div>
  );
}

export function encodePcm16(
  samples: Float32Array,
  inputSampleRate: number,
  outputSampleRate = 16_000,
): ArrayBuffer {
  const outputLength = Math.max(
    1,
    Math.round(samples.length * outputSampleRate / inputSampleRate),
  );
  const pcm = new Int16Array(outputLength);
  const ratio = inputSampleRate / outputSampleRate;
  for (let index = 0; index < outputLength; index += 1) {
    const sourceIndex = Math.min(samples.length - 1, Math.floor(index * ratio));
    const sample = Math.max(-1, Math.min(1, samples[sourceIndex]));
    pcm[index] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
  }
  return pcm.buffer;
}

export function encodeWav(chunks: Float32Array[], sampleRate: number): Blob {
  const sampleCount = chunks.reduce((total, chunk) => total + chunk.length, 0);
  const buffer = new ArrayBuffer(44 + sampleCount * 2);
  const view = new DataView(buffer);
  const writeText = (offset: number, value: string) => {
    for (let index = 0; index < value.length; index += 1) {
      view.setUint8(offset + index, value.charCodeAt(index));
    }
  };
  writeText(0, 'RIFF');
  view.setUint32(4, 36 + sampleCount * 2, true);
  writeText(8, 'WAVE');
  writeText(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeText(36, 'data');
  view.setUint32(40, sampleCount * 2, true);
  let offset = 44;
  for (const chunk of chunks) {
    for (const sample of chunk) {
      const clamped = Math.max(-1, Math.min(1, sample));
      view.setInt16(offset, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true);
      offset += 2;
    }
  }
  return new Blob([buffer], { type: 'audio/wav' });
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
        {isAssistant && sources?.length ? <SourceImages sources={sources} /> : null}
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

function readStoredWorkMode(): WorkMode {
  const stored = window.localStorage.getItem(WORK_MODE_STORAGE_KEY);
  return stored === 'extended' ? 'extended' : 'direct';
}

function readStoredWorkBudget(): WorkBudget {
  const stored = window.localStorage.getItem(WORK_BUDGET_STORAGE_KEY);
  return stored === 'low' || stored === 'high' ? stored : 'medium';
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

export function turnMetrics(
  run: Run,
  events: readonly RunStreamEvent[] = [],
): TurnMetrics | null {
  const usage = latestUsageUpdate(events) ?? run.usage as Record<string, unknown>;
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
