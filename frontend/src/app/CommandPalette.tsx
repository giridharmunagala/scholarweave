import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { request } from '../api/client';
import type { components } from '../api/schema.generated';
import { Icon, type IconName } from '../shared/components/Icons';
import { useNavigate } from './router';

type Paper = components['schemas']['DocumentSummaryResponse'];
type WorkspaceHit = components['schemas']['WorkspaceSearchResponse'];
type Track = components['schemas']['TrackDetailResponse'];
type Review = components['schemas']['ReviewResponse'];
type Conversation = components['schemas']['ConversationResponse'];
type PaperHit = components['schemas']['DocumentSearchResponse'];
type SearchFailure = { source: string; message: string };

interface Command {
  id: string;
  group: string;
  label: string;
  detail?: string;
  icon: IconName;
  run: () => void;
}

const DESTINATIONS: { label: string; to: string; icon: IconName }[] = [
  { label: 'Papers', to: '/', icon: 'papers' },
  { label: 'Notes', to: '/library/notes', icon: 'file' },
  { label: 'Research tracks', to: '/tracks', icon: 'workspace' },
  { label: 'Deep Work reviews', to: '/deep-work', icon: 'runs' },
  { label: 'Batch runs', to: '/runs', icon: 'runs' },
  { label: 'Morning brief', to: '/brief', icon: 'bulb' },
  { label: 'Chat history', to: '/chat', icon: 'chat' },
  { label: 'Settings', to: '/settings', icon: 'settings' },
];

/*
 * One search for everything you have collected. Papers, notes and tracks come
 * first; asking the assistant is a single row, never the default.
 */
export function CommandPalette({ onClose, onAsk }: { onClose: () => void; onAsk: (draft?: string) => void }) {
  const navigate = useNavigate();
  const [query, setQuery] = useState('');
  const [papers, setPapers] = useState<Paper[]>([]);
  const [tracks, setTracks] = useState<Track[]>([]);
  const [reviews, setReviews] = useState<Review[]>([]);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [tasks, setTasks] = useState<Conversation[]>([]);
  const [notes, setNotes] = useState<WorkspaceHit[]>([]);
  const [paperHits, setPaperHits] = useState<PaperHit[]>([]);
  const [catalogLoading, setCatalogLoading] = useState(true);
  const [searchLoading, setSearchLoading] = useState(false);
  const [catalogErrors, setCatalogErrors] = useState<SearchFailure[]>([]);
  const [searchErrors, setSearchErrors] = useState<SearchFailure[]>([]);
  const [refresh, setRefresh] = useState(0);
  const [activeId, setActiveId] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const backdropRef = useRef<HTMLDivElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const resultId = useId();
  const descriptionId = useId();

  useEffect(() => {
    const previousFocus = document.activeElement;
    const backdrop = backdropRef.current;
    const siblings = [...(backdrop?.parentElement?.children ?? [])]
      .filter((element) => element !== backdrop && !element.hasAttribute('inert'));
    siblings.forEach((element) => element.setAttribute('inert', ''));
    inputRef.current?.focus();
    const containFocus = (event: FocusEvent) => {
      if (event.target instanceof Node && !dialogRef.current?.contains(event.target)) inputRef.current?.focus();
    };
    document.addEventListener('focusin', containFocus);
    return () => {
      document.removeEventListener('focusin', containFocus);
      siblings.forEach((element) => element.removeAttribute('inert'));
      if (previousFocus instanceof HTMLElement && previousFocus.isConnected) previousFocus.focus();
    };
  }, []);

  useEffect(() => {
    let live = true;
    const controller = new AbortController();
    let remaining = 5;
    setCatalogLoading(true); setCatalogErrors([]);
    const load = <T,>(source: string, path: string, update: (value: T) => void) => {
      void request<T>(path, { signal: controller.signal }).then((items) => { if (live) update(items); })
        .catch((error: unknown) => {
          if (live) setCatalogErrors((items) => [...items, { source, message: error instanceof Error ? error.message : String(error) }]);
        }).finally(() => { if (live && --remaining === 0) setCatalogLoading(false); });
    };
    load<Paper[]>('Paper titles', '/documents', setPapers);
    load<Track[]>('Tracks', '/tracks', setTracks);
    load<Review[]>('Reviews', '/literature-reviews', setReviews);
    load<Conversation[]>('Chat history', '/agent/conversations', setConversations);
    load<Conversation[]>('Research history', '/deep-work/conversations', setTasks);
    return () => { live = false; controller.abort(); };
  }, [refresh]);

  useEffect(() => {
    const term = query.trim();
    setNotes([]); setPaperHits([]); setSearchErrors([]);
    setSearchLoading(term.length >= 2);
    if (term.length < 2) return;
    let live = true;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      const params = new URLSearchParams({ query: term, limit: '6' });
      ['note', 'paper_notes', 'paper_summary', 'track_section'].forEach((kind) => params.append('kinds', kind));
      let remaining = 2;
      const load = <T,>(source: string, path: string, update: (value: T) => void) => {
        void request<T>(path, { signal: controller.signal })
          .then((items) => { if (live) update(items); })
          .catch((error: unknown) => {
            if (live) setSearchErrors((items) => [...items, { source, message: error instanceof Error ? error.message : String(error) }]);
          }).finally(() => { if (live && --remaining === 0) setSearchLoading(false); });
      };
      load<WorkspaceHit[]>('Notes search', `/workspace/search?${params}`, setNotes);
      load<PaperHit[]>('Paper text search', `/documents/search?${new URLSearchParams({ query: term, limit: '8' })}`, setPaperHits);
    }, 150);
    return () => { live = false; controller.abort(); window.clearTimeout(timer); };
  }, [query, refresh]);

  const commands = useMemo<Command[]>(() => {
    const term = query.trim().toLowerCase();
    const matches = (text: string) => !term || text.toLowerCase().includes(term);
    const go = (to: string) => () => { if (navigate(to)) onClose(); };
    const paperRows = papers.filter((paper) => matches(`${paper.title} ${paper.source_filename}`) && !paperHits.some((hit) => hit.id === paper.id))
      .slice(0, term ? 8 : 5)
      .map<Command>((paper) => ({
        id: `paper:${paper.id}`, group: 'Papers', label: paper.title, detail: paper.source_filename,
        icon: 'papers', run: go(`/?paper=${encodeURIComponent(paper.id)}`),
      }));
    const contentRows = paperHits.map<Command>((paper) => ({
      id: `paper:${paper.id}`, group: 'Papers', label: paper.title, detail: paper.excerpt,
      icon: 'papers', run: go(`/?${new URLSearchParams({
        paper: paper.id, ...(paper.page_start ? { page: String(paper.page_start) } : {}),
      })}`),
    }));
    const noteRows = notes.map<Command>((hit) => ({
      id: `note:${hit.path}`, group: 'Notes', label: hit.note_name ?? hit.paper_name ?? hit.name,
      detail: hit.excerpt ?? undefined, icon: 'file',
      run: go(`/library/notes?${new URLSearchParams({ path: hit.path })}`),
    }));
    const trackRows = tracks.filter((track) => matches(track.title)).slice(0, 4).map<Command>((track) => ({
      id: `track:${track.id}`, group: 'Tracks', label: track.title, icon: 'workspace',
      run: go(`/tracks?id=${encodeURIComponent(track.id)}`),
    }));
    const reviewRows = reviews.filter((review) => matches(`${review.title} ${review.objective}`)).slice(0, 4).map<Command>((review) => ({
      id: `review:${review.id}`, group: 'Reviews', label: review.title, detail: review.objective,
      icon: 'runs', run: go(`/deep-work?${new URLSearchParams({ review: review.id })}`),
    }));
    const conversationRows = [...conversations, ...tasks.filter((task) => !conversations.some((chat) => chat.id === task.id))]
      .filter((chat) => matches(`${chat.title} ${chat.last_message_preview}`)).slice(0, 6).map<Command>((chat) => ({
        id: `chat:${chat.id}`, group: 'Conversations', label: chat.title, detail: chat.last_message_preview,
        icon: 'chat', run: go(`${chat.kind === 'deep_work' ? '/deep-work' : '/chat'}?${new URLSearchParams({ conversation: chat.id })}`),
      }));
    const actions = ([
      { id: 'add-paper', group: 'Actions', label: 'Add a paper', icon: 'upload', run: go('/?add=1') },
      ...DESTINATIONS.map<Command>((item) => ({
        id: `go:${item.to}`, group: 'Actions', label: `Go to ${item.label}`, icon: item.icon, run: go(item.to),
      })),
    ] satisfies Command[] as Command[]).filter((command) => matches(command.label));
    const askRow: Command = {
      id: 'ask', group: 'Assistant', label: term ? `Ask the assistant: “${query.trim()}”` : 'Ask the assistant',
      icon: 'sparkle', run: () => { onClose(); onAsk(query.trim() || undefined); },
    };
    return [...contentRows, ...paperRows, ...noteRows, ...trackRows, ...reviewRows, ...conversationRows, ...actions, askRow];
  }, [query, papers, paperHits, notes, tracks, reviews, conversations, tasks, navigate, onClose, onAsk]);

  const active = Math.max(0, commands.findIndex((command) => command.id === activeId));
  const failures = [...catalogErrors, ...searchErrors];
  const loading = catalogLoading || searchLoading;
  const resultCount = commands.filter((command) => !['Actions', 'Assistant'].includes(command.group)).length;
  useEffect(() => {
    listRef.current?.querySelector<HTMLElement>(`[data-index="${active}"]`)?.scrollIntoView?.({ block: 'nearest' });
  }, [active]);

  let lastGroup = '';
  return (
    <div ref={backdropRef} className="palette-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div ref={dialogRef} className="palette" role="dialog" aria-modal="true" aria-label="Search everything" aria-describedby={descriptionId}
        onKeyDown={(event) => {
          if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); onClose(); }
          if (event.key === 'Tab') {
            const controls = [...(dialogRef.current?.querySelectorAll<HTMLElement>('input, button, [tabindex]') ?? [])]
              .filter((element) => element.tabIndex >= 0 && !element.hasAttribute('disabled'));
            const first = controls[0];
            const last = controls[controls.length - 1];
            if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
            else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
          }
        }}>
        <div className="palette-input">
          <Icon name="search" size={17} />
          <input
            ref={inputRef}
            value={query}
            placeholder="Search your library and research history..."
            aria-label="Search everything"
            role="combobox"
            aria-expanded="true"
            aria-autocomplete="list"
            aria-controls={resultId}
            aria-activedescendant={`${resultId}-${active}`}
            onChange={(event) => { setQuery(event.target.value); setActiveId(''); }}
            onKeyDown={(event) => {
              if (event.key === 'ArrowDown') { event.preventDefault(); setActiveId(commands[Math.min(active + 1, commands.length - 1)].id); }
              else if (event.key === 'ArrowUp') { event.preventDefault(); setActiveId(commands[Math.max(active - 1, 0)].id); }
              else if (event.key === 'Enter') { event.preventDefault(); commands[active]?.run(); }
            }}
          />
          <button className="icon-button" type="button" aria-label="Close search" onClick={onClose}><Icon name="close" size={16} /></button>
        </div>
        <p className="palette-help field-hint" id={descriptionId}>Search paper text and notes, review questions, track titles, and chat titles or latest messages. Type at least two characters for text search.</p>
        <div className="palette-status" role="status">{loading ? 'Searching local library and history...' :
          failures.length ? 'Some sources are unavailable. Results may be incomplete.' :
          query.trim() && !resultCount ? 'No matching local items. Try different words or open a section to browse.' :
          `${resultCount} local result${resultCount === 1 ? '' : 's'}.`}</div>
        {failures.length ? <div className="palette-errors" role="alert">
          {failures.map((failure) => <p key={failure.source}>{failure.source} unavailable: {failure.message}</p>)}
          <button className="button secondary small" onClick={() => setRefresh((value) => value + 1)}>Retry search</button>
        </div> : null}
        <div className="palette-list" ref={listRef} id={resultId} role="listbox" aria-label="Results" aria-busy={loading}>
          {commands.map((command, index) => {
            const header = command.group !== lastGroup ? command.group : null;
            lastGroup = command.group;
            return (
              <div key={command.id}>
                {header ? <div className="palette-group">{header}</div> : null}
                <button
                  type="button"
                  role="option"
                  id={`${resultId}-${index}`}
                  tabIndex={-1}
                  data-index={index}
                  aria-selected={index === active}
                  className={`palette-item${command.group === 'Assistant' ? ' assistant' : ''}`}
                  data-active={index === active}
                  onMouseDown={(event) => event.preventDefault()}
                  onMouseMove={() => setActiveId(command.id)}
                  onClick={command.run}
                >
                  <Icon name={command.icon} size={15} />
                  <span className="palette-label">{command.label}</span>
                  {command.detail ? <small>{command.detail}</small> : null}
                </button>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
