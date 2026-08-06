import { useEffect, useRef, useState } from 'react';
import { subscribeToRun } from '../../api/events';
import { Link } from '../../app/router';
import { Icon } from '../../shared/components/Icons';
import { EmptyState, ErrorNotice, Loading, PageHeader, StatusPill } from '../../shared/components/Ui';
import { providersApi, type Provider, type Settings } from '../providers/api';
import {
  chatApi,
  type Conversation,
  type ConversationDetail,
  type ModelReference,
  type Run,
} from './api';
import { ChatModelPicker } from './ChatModelPicker';
import {
  applyChatStreamEvent,
  emptyChatStream,
  type ChatStreamState,
} from './chatStream';
import './chat.css';

export default function ChatPage() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [current, setCurrent] = useState<ConversationDetail | null>(null);
  const [modelReference, setModelReference] = useState<ModelReference>({});
  const [run, setRun] = useState<Run | null>(null);
  const [liveTodos, setLiveTodos] = useState<BuilderTodo[]>([]);
  const [stream, setStream] = useState<ChatStreamState>(emptyChatStream);
  const [optimisticUser, setOptimisticUser] = useState<string | null>(null);
  const [content, setContent] = useState('');
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const messageListRef = useRef<HTMLDivElement>(null);

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

  useEffect(() => {
    const list = messageListRef.current;
    if (!list) return;
    list.scrollTo({ top: list.scrollHeight, behavior: 'smooth' });
  }, [
    current?.items.length,
    optimisticUser,
    stream.reasoning,
    stream.assistant,
    stream.tools.length,
    liveTodos,
  ]);

  useEffect(() => {
    if (!run || ['completed', 'failed', 'cancelled', 'paused'].includes(run.status)) return;
    const lastSequence = run.events.reduce((max, event) => Math.max(max, event.sequence), -1);
    return subscribeToRun(
      run.id,
      lastSequence,
      (event) => {
        setStream((currentStream) => applyChatStreamEvent(currentStream, event));
        if (event.event_type === 'builder.todos.updated') {
          setLiveTodos(parseBuilderTodos(event.payload.todos));
        }
        if (['run.completed', 'run.failed', 'run.cancelled', 'run.paused'].includes(event.event_type)) {
          Promise.all([
            chatApi.run(run.id),
            current ? chatApi.get(current.id) : Promise.resolve(null),
          ]).then(([nextRun, nextConversation]) => {
            setRun(nextRun);
            setLiveTodos(builderTodos(nextRun));
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

  const create = () => {
    setCurrent(null);
    if (settings) setModelReference(defaultBuilderModel(settings));
    setRun(null);
    setLiveTodos([]);
    setStream(emptyChatStream);
    setOptimisticUser(null);
  };

  const send = async () => {
    const submitted = content.trim();
    if (!submitted || sending) return;
    setSending(true);
    setError(null);
    setContent('');
    setOptimisticUser(submitted);
    setRun(null);
    setLiveTodos([]);
    setStream(emptyChatStream);
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

  if (loading || !settings) return <Loading label="Loading builder conversations…" />;
  const displayedTodos = run ? (liveTodos.length ? liveTodos : builderTodos(run)) : [];
  return (
    <div className="page page-wide">
      <PageHeader
        eyebrow="SDK-native builder"
        title="Builder"
        description="The builder is an ordinary SDK Agent. Validation and saves happen through FunctionTool calls; conversation history lives in an SDK Session."
        actions={
          <button className="button" type="button" disabled={sending} onClick={create}>
            <Icon name="plus" size={16} />
            New chat
          </button>
        }
      />
      {error ? <ErrorNotice error={error} /> : null}
      <div className="chat-layout">
        <aside className="conversation-list panel" aria-label="Conversations">
          {conversations.map((conversation) => (
            <button
              type="button"
              disabled={sending}
              key={conversation.id}
              className={current?.id === conversation.id ? 'conversation active' : 'conversation'}
              onClick={() => {
                void open(conversation.id).catch(setError);
                setRun(null);
                setLiveTodos([]);
                setStream(emptyChatStream);
                setOptimisticUser(null);
              }}
            >
              <strong>{conversation.title}</strong>
              <small>{conversation.last_message_preview || 'No messages yet'}</small>
            </button>
          ))}
          {!conversations.length ? <p className="conversation-empty">No builder chats yet.</p> : null}
        </aside>
        <section className="chat-surface panel">
          <div className="message-list" ref={messageListRef}>
            {!current?.items.length && !run && !optimisticUser ? (
              <EmptyState
                icon="builder"
                title="Describe what you want to build"
                description="Ask for an agent, a function tool or a handoff. The builder drafts a validated SDK blueprint you can open on the canvas."
              />
            ) : null}
            {current?.items.map((item, index) =>
              item.is_compaction ? (
                <div className="compaction-marker" key={index}>Earlier SDK session history was compacted and replaced.</div>
              ) : item.text ? (
                <article className={`message role-${item.role ?? 'activity'}`} key={index}>
                  <span>{item.role ?? item.type}</span>
                  <p>{item.text}</p>
                </article>
              ) : null,
            )}
            {optimisticUser ? (
              <article className="message role-user optimistic">
                <span>user</span>
                <p>{optimisticUser}</p>
              </article>
            ) : null}
            {stream.assistant ? (
              <article className="message role-assistant streaming">
                <span>assistant · streaming</span>
                <p>{stream.assistant}<i className="stream-cursor" aria-hidden="true" /></p>
              </article>
            ) : null}
            {run ? (
              <div className="run-activity">
                <div className="row-between">
                  <strong>Run activity</strong> <StatusPill value={run.status} />
                </div>
                {displayedTodos.length ? (
                  <ol className="builder-todos">
                    {displayedTodos.map((todo) => (
                      <li className={`todo-${todo.status}`} key={todo.id}>
                        <span aria-hidden="true">{todo.status === 'completed' ? '✓' : todo.status === 'in_progress' ? '→' : '·'}</span>
                        <div>
                          <strong>{todo.title}</strong>
                          {todo.note ? <small>{todo.note}</small> : null}
                        </div>
                      </li>
                    ))}
                  </ol>
                ) : null}
                {stream.reasoning ? (
                  <details className="reasoning-stream" open={!['completed', 'failed', 'cancelled'].includes(run.status)}>
                    <summary>Reasoning</summary>
                    <p>{stream.reasoning}</p>
                  </details>
                ) : null}
                {stream.tools.length ? (
                  <div className="live-tools">
                    {stream.tools.map((tool) => (
                      <div className="live-tool" key={tool.sequence}>
                        <span className={`tool-dot ${tool.status}`} aria-hidden="true" />
                        <code>{tool.toolName}</code>
                        <small>{tool.status}</small>
                      </div>
                    ))}
                  </div>
                ) : null}
                {run.items.map((item, index) => (
                  <div className="activity-item" key={index}>
                    <span>{item.type.split('_').join(' ')}</span>
                    <strong>{item.agent_name}</strong>
                    {'content' in item ? <p>{String(item.content)}</p> : null}
                    {'output' in item ? <pre>{format(item.output)}</pre> : null}
                    {savedAgentId(item) ? (
                      <Link className="button small" to={`/agents/${savedAgentId(item)}`}>
                        Open saved agent
                      </Link>
                    ) : null}
                    {item.type === 'handoff_output_item' ? <p>{item.source_agent} → {item.target_agent}</p> : null}
                  </div>
                ))}
                {run.error ? <div className="notice error">{run.error}</div> : null}
              </div>
            ) : null}
          </div>
          <div className="composer">
            <ChatModelPicker
              providers={providers}
              settings={settings}
              value={modelReference}
              disabled={sending}
              onChange={(reference) => {
                setModelReference(reference);
                if (current) {
                  setCurrent(null);
                  setRun(null);
                  setLiveTodos([]);
                  setStream(emptyChatStream);
                  setOptimisticUser(null);
                }
              }}
            />
            <textarea
              disabled={sending}
              value={content}
              onChange={(event) => setContent(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !event.shiftKey) {
                  event.preventDefault();
                  void send();
                }
              }}
              placeholder="Describe the SDK agent or function tool you want to build…"
            />
            <button className="button" type="button" disabled={sending || !content.trim()} onClick={() => void send()}>
              {sending ? 'Running…' : 'Send'}
            </button>
            <p className="composer-hint">
              <kbd>Enter</kbd> to send · <kbd>Shift</kbd> + <kbd>Enter</kbd> for a new line
            </p>
          </div>
        </section>
      </div>
    </div>
  );
}

function format(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value, null, 2);
}

interface BuilderTodo {
  id: string;
  title: string;
  status: 'pending' | 'in_progress' | 'completed' | 'blocked';
  note?: string | null;
}

function builderTodos(run: Run): BuilderTodo[] {
  const event = [...run.events].reverse().find((item) => item.event_type === 'builder.todos.updated');
  return parseBuilderTodos(event?.payload.todos);
}

function parseBuilderTodos(todos: unknown): BuilderTodo[] {
  if (!Array.isArray(todos)) return [];
  return todos.filter(
    (todo): todo is BuilderTodo =>
      typeof todo === 'object'
      && todo !== null
      && typeof todo.id === 'string'
      && typeof todo.title === 'string'
      && ['pending', 'in_progress', 'completed', 'blocked'].includes(String(todo.status)),
  );
}

function savedAgentId(item: Run['items'][number]): string | null {
  if (item.type !== 'tool_call_output_item' || typeof item.output !== 'object' || item.output === null) return null;
  const agentId = (item.output as Record<string, unknown>).agent_id;
  return typeof agentId === 'string' ? agentId : null;
}

function defaultBuilderModel(settings: Settings): ModelReference {
  return settings.default_model_references.tools
    ?? settings.default_model_references.chat
    ?? {};
}
