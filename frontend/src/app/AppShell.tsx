import { useEffect, useState, type ReactNode } from 'react';
import {
  CHAT_CONVERSATIONS_CHANGED,
  chatApi,
  type Conversation,
} from '../features/chat/api';
import { CommandPalette, useCommandPalette } from '../shared/components/CommandPalette';
import { Icon, type IconName } from '../shared/components/Icons';
import { ThemeSwitcher } from '../shared/components/ThemeSwitcher';
import { Link, NavLink, useLocation, useNavigate } from './router';

interface NavEntry {
  to: string;
  label: string;
  icon: IconName;
  /** Icon tint, so each destination is recognisable by colour as well as glyph. */
  tone: 'agent' | 'papers' | 'tools' | 'files' | 'runs' | 'settings';
}

const NAV_GROUPS: Array<{ title: string; items: NavEntry[] }> = [
  {
    title: 'Research',
    items: [
      { to: '/', label: 'Agent', icon: 'agents', tone: 'agent' },
      { to: '/papers', label: 'Papers', icon: 'papers', tone: 'papers' },
      { to: '/tools', label: 'Tools', icon: 'tools', tone: 'tools' },
      { to: '/workspace', label: 'Files', icon: 'workspace', tone: 'files' },
    ],
  },
  {
    title: 'Operate',
    items: [
      { to: '/runs', label: 'Runs', icon: 'runs', tone: 'runs' },
      { to: '/settings', label: 'Settings', icon: 'settings', tone: 'settings' },
    ],
  },
];

const COMPACT_KEY = 'scholarweave-sidebar-compact';

const TITLES: Array<[string, string]> = [
  ['/', 'Research agent'],
  ['/tools', 'Tools'],
  ['/runs', 'Runs'],
  ['/papers', 'Papers'],
  ['/workspace', 'Files'],
  ['/settings', 'Settings'],
];

function currentTitle(pathname: string): string {
  const match = TITLES.find(([prefix]) => pathname === prefix || pathname.startsWith(`${prefix}/`));
  return match ? match[1] : 'Research agent';
}

export function AppShell({ children }: { children: ReactNode }) {
  const { pathname, search } = useLocation();
  const navigate = useNavigate();
  const palette = useCommandPalette();
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [conversationError, setConversationError] = useState<string | null>(null);
  const [compact, setCompact] = useState(() => {
    try {
      return localStorage.getItem(COMPACT_KEY) === 'true';
    } catch {
      return false;
    }
  });
  const [drawer, setDrawer] = useState(false);
  const chatRoute = new URLSearchParams(search);
  const activeConversation = chatRoute.has('new')
    ? null
    : chatRoute.get('conversation') ?? conversations[0]?.id ?? null;

  useEffect(() => {
    try {
      localStorage.setItem(COMPACT_KEY, String(compact));
    } catch {
      /* Persisting the layout preference is best effort. */
    }
  }, [compact]);

  // Route changes should never leave the mobile drawer covering the page.
  useEffect(() => setDrawer(false), [pathname]);

  useEffect(() => {
    if (pathname !== '/') return;
    let active = true;
    const refresh = () => {
      void chatApi.list()
        .then((items) => {
          if (active) {
            setConversations(items);
            setConversationError(null);
          }
        })
        .catch((error: unknown) => {
          if (active) {
            setConversations([]);
            setConversationError(
              error instanceof Error ? error.message : 'Could not load previous chats.',
            );
          }
        });
    };
    refresh();
    window.addEventListener(CHAT_CONVERSATIONS_CHANGED, refresh);
    return () => {
      active = false;
      window.removeEventListener(CHAT_CONVERSATIONS_CHANGED, refresh);
    };
  }, [pathname]);

  const removeConversation = async (conversation: Conversation) => {
    try {
      await chatApi.remove(conversation.id);
      setConversations((items) => items.filter((item) => item.id !== conversation.id));
      setConversationError(null);
      if (activeConversation === conversation.id) navigate('/?new=1', { replace: true });
    } catch (error) {
      setConversationError(
        error instanceof Error ? error.message : 'Could not delete the chat.',
      );
    }
  };

  const title = currentTitle(pathname);
  const detail = pathname.split('/').filter(Boolean)[1];
  const isChat = pathname === '/';

  return (
    <div className="app-shell" data-compact={compact} data-drawer={drawer}>
      <aside className="sidebar">
        <Link className="brand" to="/" aria-label="ScholarWeave home">
          <span className="brand-mark">SW</span>
          <span className="brand-text">
            <strong>ScholarWeave</strong>
            <small>Agents SDK workspace</small>
          </span>
        </Link>
        <nav aria-label="Primary">
          {NAV_GROUPS.map((group) => (
            <div className="nav-section" key={group.title}>
              <div className="nav-group">{group.title}</div>
              {group.items.map((item) => {
                const link = (
                  <NavLink key={item.to} to={item.to} title={item.label}>
                    <Icon name={item.icon} className={`nav-icon tone-${item.tone}`} />
                    <span className="nav-label">{item.label}</span>
                  </NavLink>
                );
                if (item.to !== '/') return link;
                return (
                  <div className="nav-agent-branch" key={item.to}>
                    {link}
                    {pathname === '/' ? (
                      <div className="nav-chat-tree" aria-label="Previous agent chats">
                        <Link
                          className={`nav-chat-new${new URLSearchParams(search).has('new') ? ' active' : ''}`}
                          to="/?new=1"
                          title="Start a new chat"
                        >
                          <Icon name="plus" size={14} />
                          <span>New chat</span>
                        </Link>
                        <div className="nav-chat-caption">
                          <span>Recent</span>
                          {conversations.length ? (
                            <span className="nav-chat-count">{conversations.length}</span>
                          ) : null}
                        </div>
                        <div className="nav-chat-sessions">
                          {conversations.map((conversation) => (
                            <div
                              className={`nav-chat-session${activeConversation === conversation.id ? ' active' : ''}`}
                              key={conversation.id}
                            >
                              <Link
                                to={`/?conversation=${encodeURIComponent(conversation.id)}`}
                                title={conversation.title}
                              >
                                <span className="nav-chat-title">{conversation.title}</span>
                                <small className="nav-chat-preview">
                                  {conversation.last_message_preview || 'No messages yet'}
                                </small>
                              </Link>
                              <button
                                type="button"
                                className="nav-chat-delete"
                                aria-label={`Delete ${conversation.title}`}
                                title="Delete chat"
                                onClick={() => void removeConversation(conversation)}
                              >
                                <Icon name="trash" size={13} />
                              </button>
                            </div>
                          ))}
                          {conversationError ? (
                            <small className="nav-chat-error">{conversationError}</small>
                          ) : !conversations.length ? (
                            <small className="nav-chat-empty">No previous chats yet</small>
                          ) : null}
                        </div>
                      </div>
                    ) : null}
                  </div>
                );
              })}
            </div>
          ))}
        </nav>
        <div className="sidebar-foot">
          <button
            type="button"
            className="button secondary small sidebar-toggle"
            aria-label={compact ? 'Expand sidebar' : 'Collapse sidebar'}
            aria-pressed={compact}
            title={compact ? 'Expand sidebar' : 'Collapse sidebar'}
            onClick={() => setCompact((value) => !value)}
          >
            <Icon name="sidebar" size={15} />
            <span className="sidebar-toggle-label">{compact ? 'Expand' : 'Collapse'}</span>
          </button>
          <div className="sdk-status" title="OpenAI Agents SDK 0.19.4">
            <span className="sdk-dot" />
            <span>OpenAI Agents SDK 0.19.4</span>
          </div>
        </div>
      </aside>

      <div className="sidebar-scrim" role="presentation" onClick={() => setDrawer(false)} />

      <div className="app-body" data-chat={isChat}>
        <header className="topbar">
          <button
            type="button"
            className="button ghost icon drawer-toggle"
            aria-label="Open navigation"
            onClick={() => setDrawer((value) => !value)}
          >
            <Icon name="menu" />
          </button>
          <nav className="breadcrumb" aria-label="Breadcrumb">
            <strong>{title}</strong>
            {detail ? (
              <>
                <span className="sep">/</span>
                <span className="truncate">{detail === 'new' ? 'New' : detail}</span>
              </>
            ) : null}
          </nav>
          <span className="spacer" />
          <button
            type="button"
            className="search-trigger"
            aria-label="Search"
            title="Search"
            onClick={() => palette.setOpen(true)}
          >
            <Icon name="search" size={16} />
            <span className="search-label">Search…</span>
            <kbd>⌘K</kbd>
          </button>
          <ThemeSwitcher />
        </header>
        <main className={`app-main${isChat ? ' chat-main' : ''}`}>{children}</main>
      </div>

      <CommandPalette open={palette.open} onClose={palette.close} />
    </div>
  );
}
