import { useEffect, useRef, useState } from 'react';
import { subscribeToRun } from '../../api/events';
import { Icon } from '../../shared/components/Icons';
import { MarkdownViewer } from '../../shared/components/MarkdownViewer';
import {
  EmptyState,
  ErrorNotice,
  Loading,
  PageHeader,
  StatusPill,
} from '../../shared/components/Ui';
import { ChatModelPicker } from '../chat/ChatModelPicker';
import {
  applyChatStreamEvent,
  emptyChatStream,
  type ChatStreamState,
} from '../chat/chatStream';
import { providersApi, type Provider, type Settings } from '../providers/api';
import {
  directAgentsApi,
  type DirectAgent,
  type DirectConversation,
  type DirectConversationDetail,
  type Document,
  type ModelReference,
  type Run,
} from './api';
import '../chat/chat.css';
import './direct-agents.css';

export default function DirectAgentsPage() {
  const [agents, setAgents] = useState<DirectAgent[]>([]);
  const [conversations, setConversations] = useState<DirectConversation[]>([]);
  const [documents, setDocuments] = useState<Document[]>([]);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [selectedAgentKey, setSelectedAgentKey] = useState<DirectAgent['key']>('summary');
  const [selectedDocumentId, setSelectedDocumentId] = useState('');
  const [modelReference, setModelReference] = useState<ModelReference>({});
  const [current, setCurrent] = useState<DirectConversationDetail | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const [stream, setStream] = useState<ChatStreamState>(emptyChatStream);
  const [optimisticUser, setOptimisticUser] = useState<string | null>(null);
  const [content, setContent] = useState('');
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const messageListRef = useRef<HTMLDivElement>(null);

  const readyDocuments = documents.filter((document) => document.status === 'ready');
  const selectedAgent = agents.find((agent) => agent.key === selectedAgentKey);

  const refreshConversations = () =>
    directAgentsApi.conversations().then(setConversations);

  const open = async (id: string) => {
    const conversation = await directAgentsApi.get(id);
    setCurrent(conversation);
    setSelectedAgentKey(conversation.agent_key);
    setSelectedDocumentId(conversation.document_ids[0] ?? '');
    setModelReference(conversation.model_reference);
  };

  useEffect(() => {
    Promise.all([
      directAgentsApi.agents(),
      directAgentsApi.conversations(),
      directAgentsApi.documents(),
      providersApi.list(),
      providersApi.settings(),
    ])
      .then(async ([nextAgents, nextConversations, nextDocuments, nextProviders, nextSettings]) => {
        setAgents(nextAgents);
        setConversations(nextConversations);
        setDocuments(nextDocuments);
        setProviders(nextProviders);
        setSettings(nextSettings);
        setModelReference(defaultAgentModel(nextSettings));
        const firstReady = nextDocuments.find((document) => document.status === 'ready');
        setSelectedDocumentId(firstReady?.id ?? '');
        if (nextConversations[0]) await open(nextConversations[0].id);
      })
      .catch(setError)
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    const list = messageListRef.current;
    if (list) list.scrollTo({ top: list.scrollHeight, behavior: 'smooth' });
  }, [current?.items.length, optimisticUser, stream.assistant, stream.reasoning, stream.tools.length]);

  useEffect(() => {
    if (!run || ['completed', 'failed', 'cancelled', 'paused'].includes(run.status)) return;
    const lastSequence = run.events.reduce((max, event) => Math.max(max, event.sequence), -1);
    return subscribeToRun(
      run.id,
      lastSequence,
      (event) => {
        setStream((value) => applyChatStreamEvent(value, event));
        if (['run.completed', 'run.failed', 'run.cancelled', 'run.paused'].includes(event.event_type)) {
          Promise.all([
            directAgentsApi.run(run.id),
            current ? directAgentsApi.get(current.id) : Promise.resolve(null),
          ])
            .then(([nextRun, nextConversation]) => {
              setRun(nextRun);
              if (nextConversation) setCurrent(nextConversation);
              setStream(emptyChatStream);
              void refreshConversations();
            })
            .catch(setError)
            .finally(() => {
              setOptimisticUser(null);
              setSending(false);
            });
        }
      },
      () => undefined,
    );
  }, [run?.id, run?.status, current?.id]);

  const newChat = () => {
    setCurrent(null);
    setRun(null);
    setStream(emptyChatStream);
    setOptimisticUser(null);
    if (settings) setModelReference(defaultAgentModel(settings));
  };

  const selectAgent = (key: DirectAgent['key']) => {
    if (sending) return;
    setSelectedAgentKey(key);
    newChat();
  };

  const send = async () => {
    const submitted = content.trim();
    if (!submitted || sending || !selectedAgent) return;
    if (selectedAgent.requires_document && !selectedDocumentId) {
      setError(new Error('Select an ingested paper before starting this agent.'));
      return;
    }
    setSending(true);
    setError(null);
    setContent('');
    setOptimisticUser(submitted);
    setRun(null);
    setStream(emptyChatStream);
    try {
      let conversation = current;
      if (!conversation) {
        const created = await directAgentsApi.create(
          selectedAgent.key,
          selectedAgent.requires_document ? [selectedDocumentId] : [],
          submitted.slice(0, 50),
          modelReference,
        );
        conversation = await directAgentsApi.get(created.id);
        setCurrent(conversation);
        setModelReference(conversation.model_reference);
        void refreshConversations();
      }
      const response = await directAgentsApi.send(conversation.id, submitted);
      setRun(response.run);
    } catch (nextError) {
      setContent(submitted);
      setOptimisticUser(null);
      setSending(false);
      setError(nextError);
    }
  };

  if (loading) return <Loading label="Loading research agents…" />;
  if (!settings) {
    return error
      ? <div className="page"><ErrorNotice error={error} /></div>
      : <Loading label="Loading research agents…" />;
  }

  return (
    <div className="page page-wide direct-agents-page">
      <PageHeader
        eyebrow="Code-defined agents"
        title="Research agent chat"
        description="Talk directly with four fixed research agents. These agents are not created or modified through the builder."
        actions={
          <button className="button" type="button" disabled={sending} onClick={newChat}>
            <Icon name="plus" size={16} />
            New chat
          </button>
        }
      />
      {error ? <ErrorNotice error={error} /> : null}
      <div className="direct-agent-grid" aria-label="Research agents">
        {agents.map((agent) => (
          <button
            type="button"
            className={agent.key === selectedAgentKey ? 'card direct-agent-card active' : 'card direct-agent-card'}
            aria-pressed={agent.key === selectedAgentKey}
            disabled={sending}
            key={agent.key}
            onClick={() => selectAgent(agent.key)}
          >
            <span className="direct-agent-icon"><Icon name={agentIcon(agent.key)} /></span>
            <strong>{agent.name}</strong>
            <small>{agent.description}</small>
          </button>
        ))}
      </div>
      <div className="chat-layout">
        <aside className="conversation-list panel" aria-label="Agent conversations">
          <div className="conversation-scroll">
            {conversations.map((conversation) => (
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
                    setRun(null);
                    setStream(emptyChatStream);
                    setOptimisticUser(null);
                  }}
                >
                  <strong>{conversation.title}</strong>
                  <small>{agentName(agents, conversation.agent_key)} · {conversation.last_message_preview || 'No messages yet'}</small>
                </button>
              </div>
            ))}
            {!conversations.length ? <p className="conversation-empty">No research agent chats yet.</p> : null}
          </div>
        </aside>
        <section className="chat-surface panel">
          <div className="direct-agent-context">
            <strong>{selectedAgent?.name}</strong>
            {selectedAgent?.requires_document ? (
              <label>
                <span>Paper</span>
                <select
                  value={selectedDocumentId}
                  disabled={sending || Boolean(current)}
                  onChange={(event) => setSelectedDocumentId(event.target.value)}
                >
                  <option value="">Select an ingested paper</option>
                  {readyDocuments.map((document) => (
                    <option value={document.id} key={document.id}>{document.title}</option>
                  ))}
                </select>
              </label>
            ) : (
              <small>Uses all summaries saved by the Summary agent.</small>
            )}
          </div>
          <div className="message-list" ref={messageListRef}>
            {!current?.items.length && !run && !optimisticUser ? (
              <EmptyState
                icon="agents"
                title={selectedAgent?.name ?? 'Choose an agent'}
                description={emptyDescription(selectedAgentKey)}
              />
            ) : null}
            {current?.items.map((item, index) =>
              item.text ? (
                <article className={`message role-${item.role ?? 'activity'}`} key={index}>
                  <span className="message-avatar" aria-hidden="true">
                    <Icon name={item.role === 'user' ? 'user' : 'sparkle'} size={14} />
                  </span>
                  <div className="message-body">
                    <div className="message-header"><span>{item.role ?? item.type}</span></div>
                    <MarkdownViewer content={item.text} />
                  </div>
                </article>
              ) : null,
            )}
            {optimisticUser ? (
              <article className="message role-user optimistic">
                <span className="message-avatar" aria-hidden="true">
                  <Icon name="user" size={14} />
                </span>
                <div className="message-body">
                  <div className="message-header"><span>user</span></div>
                  <p>{optimisticUser}</p>
                </div>
              </article>
            ) : null}
            {stream.assistant ? (
              <article className="message role-assistant streaming">
                <span className="message-avatar" aria-hidden="true">
                  <Icon name="sparkle" size={14} />
                </span>
                <div className="message-body">
                  <div className="message-header"><span>assistant · streaming</span></div>
                  <MarkdownViewer content={stream.assistant} />
                  <i className="stream-cursor" aria-hidden="true" />
                </div>
              </article>
            ) : null}
            {run ? (
              <div className="run-activity static">
                <div className="row-between run-activity-head">
                  <strong>Run activity</strong>
                  <StatusPill value={run.status} />
                </div>
                <div className="run-activity-content">
                  {stream.tools.map((tool) => (
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
                  {stream.reasoning ? (
                    <details className="reasoning-stream">
                      <summary>
                        <span className="activity-icon reasoning"><Icon name="sparkle" size={14} /></span>
                        <span>
                          <strong>Reasoning</strong>
                          <small>Following the agent's approach</small>
                        </span>
                        <Icon className="activity-chevron" name="arrowRight" size={13} />
                      </summary>
                      <div className="reasoning-content">{stream.reasoning}</div>
                    </details>
                  ) : null}
                  {run.error ? <div className="notice error">{run.error}</div> : null}
                </div>
              </div>
            ) : null}
          </div>
          <div className="composer">
            <div className="composer-box">
              <textarea
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
                placeholder={composerPlaceholder(selectedAgentKey)}
              />
              <div className="composer-actions">
                <ChatModelPicker
                  providers={providers}
                  settings={settings}
                  value={modelReference}
                  disabled={sending || Boolean(current)}
                  onChange={setModelReference}
                />
                <span className="composer-hint">
                  <kbd>Enter</kbd> send · <kbd>Shift</kbd>+<kbd>Enter</kbd> newline
                </span>
                <button
                  className="button icon composer-send"
                  type="button"
                  aria-label="Send message"
                  disabled={
                    sending
                    || !content.trim()
                    || Boolean(selectedAgent?.requires_document && !selectedDocumentId)
                  }
                  onClick={() => void send()}
                >
                  {sending ? <span className="spinner tiny" aria-hidden="true" /> : <Icon name="arrowRight" size={16} />}
                </button>
              </div>
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}

function defaultAgentModel(settings: Settings): ModelReference {
  return settings.default_model_references.chat ?? {};
}

function agentName(agents: DirectAgent[], key: DirectAgent['key']): string {
  return agents.find((agent) => agent.key === key)?.name ?? key;
}

function agentIcon(key: DirectAgent['key']): 'papers' | 'search' | 'builder' | 'scan' {
  if (key === 'summary') return 'papers';
  if (key === 'open_areas') return 'search';
  if (key === 'qa') return 'builder';
  return 'scan';
}

function emptyDescription(key: DirectAgent['key']): string {
  if (key === 'summary') return 'Ask the agent to produce the fixed four-part summary for the selected paper.';
  if (key === 'open_areas') return 'Analyze explicitly stated future research directions across saved paper summaries.';
  if (key === 'qa') return 'Ask a question that should be answered only from the selected paper.';
  return 'Start a full page-by-page keep/no-keep review of the selected paper.';
}

function composerPlaceholder(key: DirectAgent['key']): string {
  if (key === 'summary') return 'Summarize this paper using the four required questions…';
  if (key === 'open_areas') return 'Find open research areas across my paper summaries…';
  if (key === 'qa') return 'Ask a question about this paper…';
  return 'Review every page and flag it keep or no keep…';
}

function humanize(value: string): string {
  const words = value.replace(/[_-]+/g, ' ').trim();
  return words ? words.charAt(0).toLocaleUpperCase() + words.slice(1) : 'Tool';
}
