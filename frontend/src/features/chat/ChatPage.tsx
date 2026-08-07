import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { subscribeToRun } from '../../api/events';
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
  const [stream, setStream] = useState<ChatStreamState>(emptyChatStream);
  const [optimisticUser, setOptimisticUser] = useState<string | null>(null);
  const [content, setContent] = useState('');
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [pinnedToBottom, setPinnedToBottom] = useState(true);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [activityOpen, setActivityOpen] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const messageListRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const autoOpenedActivityRunRef = useRef<string | null>(null);

  const refreshList = () => chatApi.list().then(setConversations);
  const open = async (id: string) => {
    const conversation = await chatApi.get(id);
    setCurrent(conversation);
    setModelReference(conversation.model_reference);
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
    if (!run || ['completed', 'failed', 'cancelled', 'paused'].includes(run.status)) return;
    const lastSequence = run.events.reduce((max, event) => Math.max(max, event.sequence), -1);
    return subscribeToRun(
      run.id,
      lastSequence,
      (event) => {
        setStream((currentStream) => applyChatStreamEvent(currentStream, event));
        if (['run.completed', 'run.failed', 'run.cancelled', 'run.paused'].includes(event.event_type)) {
          Promise.all([
            chatApi.run(run.id),
            current ? chatApi.get(current.id) : Promise.resolve(null),
          ]).then(([nextRun, nextConversation]) => {
            setRun(nextRun);
            if (nextConversation) setCurrent(nextConversation);
            setOptimisticUser(null);
            setStream((currentStream) => ({
              ...currentStream,
              assistant: '',
              tools: [],
            }));
            void refreshList();
            setSending(false);
          }).catch(setError);
        }
      },
      () => undefined,
    );
  }, [run?.id, run?.status, current?.id]);

  const resetThread = () => {
    setRun(null);
    setStream(emptyChatStream);
    setOptimisticUser(null);
    setPinnedToBottom(true);
    setActivityOpen(false);
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
        conversation = await chatApi.get(created.id);
        setCurrent(conversation);
        setModelReference(conversation.model_reference);
        void refreshList();
      }
      const response = await chatApi.send(conversation.id, submitted);
      setRun(response.run);
    } catch (nextError) {
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
  const activityCount = stream.tools.length || persistedActivityCount;
  const hasActivity = activityCount > 0;

  useEffect(() => {
    if (!run || !hasActivity || autoOpenedActivityRunRef.current === run.id) return;
    autoOpenedActivityRunRef.current = run.id;
    setActivityOpen(true);
  }, [run?.id, hasActivity]);

  if (loading || !settings) return <Loading label="Loading agent conversations…" />;

  const activeModelLabel =
    modelReferenceLabel(modelReference, providers)
    || modelReferenceLabel(settings.default_model_references.chat ?? {}, providers)
    || 'No model configured';
  const hasTranscript = Boolean(current?.items.length || run || optimisticUser);
  const answerReady = Boolean(stream.assistant) || run?.status === 'completed';
  const reasoningActive = run?.status === 'pending' || run?.status === 'running';
  const reasoningBeforeIndex = stream.reasoning && run?.status === 'completed'
    ? findLastAssistantIndex(current?.items ?? [])
    : -1;

  return (
    <div className="page page-wide chat-page">
      {error ? <ErrorNotice error={error} /> : null}
      <div className={`chat-layout${sidebarOpen ? '' : ' collapsed'}`}>
        <aside className="conversation-list panel" aria-label="Conversations">
          <div className="conversation-list-head">
            <button className="button block" type="button" disabled={sending} onClick={create}>
              <Icon name="plus" size={15} />
              New chat
            </button>
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
                  disabled={sending}
                  className="conversation-open"
                  onClick={() => {
                    void open(conversation.id).catch(setError);
                    resetThread();
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
              <button
                type="button"
                className="button ghost icon chat-sidebar-toggle"
                aria-label={sidebarOpen ? 'Hide chat list' : 'Show chat list'}
                onClick={() => setSidebarOpen((value) => !value)}
              >
                <Icon name="sidebar" size={16} />
              </button>
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
                    <strong>{activityCount}</strong>
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
                {current?.items.map((item, index) =>
                  item.text ? (
                    <Fragment key={index}>
                      {index === reasoningBeforeIndex ? (
                        <ReasoningDisclosure
                          content={stream.reasoning}
                          active={reasoningActive}
                          answerReady={answerReady}
                        />
                      ) : null}
                      <Message role={item.role ?? item.type} text={item.text} />
                    </Fragment>
                  ) : null,
                )}
                {optimisticUser ? (
                  <Message role="user" text={optimisticUser} className="optimistic" />
                ) : null}
                {stream.reasoning && reasoningBeforeIndex < 0 ? (
                  <ReasoningDisclosure
                    content={stream.reasoning}
                    active={reasoningActive}
                    answerReady={answerReady}
                  />
                ) : null}
                {stream.assistant ? (
                  <Message role="assistant" text={stream.assistant} className="streaming" streaming />
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
            {activityOpen && hasActivity && run ? (
              <>
                <button
                  type="button"
                  className="tool-panel-scrim"
                  aria-label="Hide tool activity"
                  onClick={() => setActivityOpen(false)}
                />
                <ToolActivityPanel
                  run={run}
                  tools={stream.tools}
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

function ReasoningDisclosure({
  content,
  active,
  answerReady,
}: {
  content: string;
  active: boolean;
  answerReady: boolean;
}) {
  const [open, setOpen] = useState(!answerReady);
  const answerWasReadyRef = useRef(answerReady);

  useLayoutEffect(() => {
    if (answerReady && !answerWasReadyRef.current) setOpen(false);
    answerWasReadyRef.current = answerReady;
  }, [answerReady]);

  return (
    <details
      className={`reasoning-card${active && !answerReady ? ' live' : ''}`}
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <span className="reasoning-card-icon">
          {active && !answerReady
            ? <span className="spinner tiny" aria-hidden="true" />
            : <Icon name="sparkle" size={14} />}
        </span>
        <span className="reasoning-card-label">
          <strong>{active && !answerReady ? 'Thinking' : 'Reasoning'}</strong>
          <small>{open ? "Following the agent's approach" : 'Expand to review'}</small>
        </span>
        {active && !answerReady ? <span className="activity-live">Live</span> : null}
        <Icon className="activity-chevron" name="arrowRight" size={13} />
      </summary>
      <div className="reasoning-content">
        <MarkdownViewer content={content} />
        {active && !answerReady ? <i className="stream-cursor" aria-hidden="true" /> : null}
      </div>
    </details>
  );
}

function ToolActivityPanel({
  run,
  tools,
  onClose,
}: {
  run: Run;
  tools: ChatStreamState['tools'];
  onClose: () => void;
}) {
  const items = run.items.filter(isVisibleActivityItem);
  const count = tools.length || items.length;

  return (
    <aside className="tool-panel" id="chat-tool-activity" aria-label="Tool activity">
      <header className="tool-panel-header">
        <span className="tool-panel-title-icon"><Icon name="tools" size={15} /></span>
        <span className="tool-panel-title">
          <strong>Tool activity</strong>
          <small>{runActivityTitle(run.status)} · {count} {count === 1 ? 'step' : 'steps'}</small>
        </span>
        <button
          type="button"
          className="button ghost icon"
          aria-label="Hide tool activity"
          onClick={onClose}
        >
          <Icon name="close" size={15} />
        </button>
      </header>
      <div className="tool-panel-scroll">
        {tools.length ? (
          <section className="activity-section">
            <h3>Live calls</h3>
            <div className="live-tools" aria-live="polite">
              {tools.map((tool) => (
                <div className="live-tool" key={tool.sequence}>
                  <span className="activity-icon tool"><Icon name="tools" size={14} /></span>
                  <span className="live-tool-body">
                    <strong>{humanize(tool.toolName)}</strong>
                    <small>{tool.status === 'running' ? 'Using tool…' : 'Tool finished'}</small>
                  </span>
                  <span className={`tool-status ${tool.status}`}>
                    {tool.status === 'running'
                      ? <span className="spinner tiny" aria-hidden="true" />
                      : <Icon name="check" size={13} />}
                    {tool.status === 'running' ? 'Running' : 'Done'}
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
}: {
  role: string;
  text: string;
  className?: string;
  streaming?: boolean;
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

function findLastAssistantIndex(items: ConversationDetail['items']): number {
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (item.text && (item.role ?? item.type) === 'assistant') return index;
  }
  return -1;
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
