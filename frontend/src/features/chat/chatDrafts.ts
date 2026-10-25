import type { AskContext } from '../../app/ask';
import type { ConversationAttachment, ModelReference, ResponseEffort } from './api';

export interface ChatDraft {
  conversationId: string | null;
  content: string;
  attachments: ConversationAttachment[];
  reading: AskContext | null;
  selectedIdea: string;
  otherIdea: string;
  intent: 'chat' | 'ideation';
  trackId: string;
  modelReference: ModelReference;
  responseEffort: ResponseEffort;
  webEnabled: boolean;
  failedDraft?: string | null;
  contentVersion?: number;
}

const PREFIX = 'scholarweave.chat.draft.';
type ChatSurface = 'assistant' | 'full';
const fallback = new Map<string, string>();
let warningInstalled = false;

function readStored(key: string): string | null {
  if (fallback.has(key)) return fallback.get(key)!;
  try {
    return window.sessionStorage.getItem(PREFIX + key);
  } catch {
    return null;
  }
}

function store(key: string, value: string) {
  try {
    window.sessionStorage.setItem(PREFIX + key, value);
    fallback.delete(key);
  } catch {
    fallback.set(key, value);
    if (!warningInstalled) {
      warningInstalled = true;
      window.addEventListener('beforeunload', (event) => {
        if (!fallback.size) return;
        event.preventDefault();
        event.returnValue = '';
      });
    }
  }
}

export function conversationDraftKey(id: string) {
  return `conversation:${id}`;
}

export function contextualDraftKey(input: unknown) {
  const text = JSON.stringify(input);
  let left = 0x811c9dc5;
  let right = 0x9e3779b9;
  for (let index = 0; index < text.length; index += 1) {
    left = Math.imul(left ^ text.charCodeAt(index), 0x01000193);
    right = Math.imul(right ^ text.charCodeAt(index), 0x85ebca6b);
  }
  return `context:${(left >>> 0).toString(16).padStart(8, '0')}${(right >>> 0).toString(16).padStart(8, '0')}`;
}

export function readChatDraft(key: string | null): ChatDraft | null {
  if (!key) return null;
  try {
    const draft = JSON.parse(readStored(key) ?? 'null') as ChatDraft | null;
    return draft && typeof draft.content === 'string' && Array.isArray(draft.attachments) ? draft : null;
  } catch {
    return null;
  }
}

export function lastChatDraftKey(surface: ChatSurface): string | null {
  return readStored(surface);
}

export function saveChatDraft(key: string, draft: ChatDraft, surface?: ChatSurface) {
  store(key, JSON.stringify(draft));
  if (surface) store(surface, key);
}

export function forgetChatDraft(key: string) {
  fallback.delete(key);
  try {
    window.sessionStorage.removeItem(PREFIX + key);
  } catch {
    // Deleting a conversation must not depend on browser storage.
  }
}

export function fullChatDraftHref(key: string, conversationId?: string | null) {
  const params = new URLSearchParams({ draft: key });
  if (conversationId) params.set('conversation', conversationId);
  return `/chat?${params}`;
}
