import { useEffect, useRef, useState } from 'react';
import { request } from '../../api/client';
import type { components } from '../../api/schema.generated';
import type { AskContext } from '../../app/ask';
import { Icon } from '../../shared/components/Icons';
import { ErrorNotice, Loading } from '../../shared/components/Ui';

type Paper = components['schemas']['DocumentSummaryResponse'];

export function WorkspacePaperPicker({ disabled, selectedId, onSelect }: {
  disabled: boolean;
  selectedId?: string;
  onSelect: (paper: AskContext) => void;
}) {
  const [open, setOpen] = useState(false);
  const [papers, setPapers] = useState<Paper[]>([]);
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [attempt, setAttempt] = useState(0);
  const container = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const search = useRef<HTMLInputElement>(null);
  const close = () => { setOpen(false); trigger.current?.focus(); };

  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);

  useEffect(() => {
    if (!open) return;
    search.current?.focus();
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    request<Paper[]>('/documents', { signal: controller.signal })
      .then((items) => { if (!controller.signal.aborted) setPapers(items); })
      .catch((reason) => { if (!controller.signal.aborted) setError(reason); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    const dismiss = (event: PointerEvent) => {
      if (event.target instanceof Node && !container.current?.contains(event.target)) setOpen(false);
    };
    document.addEventListener('pointerdown', dismiss);
    return () => { controller.abort(); document.removeEventListener('pointerdown', dismiss); };
  }, [open, attempt]);

  const needle = query.trim().toLocaleLowerCase();
  const filtered = papers.filter((paper) =>
    `${paper.title} ${paper.source_filename}`.toLocaleLowerCase().includes(needle));

  return (
    <div className="composer-paper-picker" ref={container}
      onKeyDown={(event) => {
        if (open && event.key === 'Escape') {
          event.preventDefault();
          event.stopPropagation();
          close();
        }
      }}>
      <button ref={trigger} type="button" className="composer-capability" disabled={disabled}
        aria-label="Add workspace paper" aria-expanded={open} aria-haspopup="dialog"
        title="Use a paper already in your workspace without uploading another copy"
        onClick={() => setOpen((value) => !value)}>
        <Icon name="papers" size={15} />Paper
      </button>
      {open ? (
        <div className="composer-paper-popover" role="dialog" aria-label="Choose a workspace paper">
          <div className="composer-paper-heading">
            <strong>Workspace papers</strong>
            <button type="button" className="icon-button" aria-label="Close paper picker" onClick={close}>
              <Icon name="close" size={14} />
            </button>
          </div>
          <input ref={search} type="search" aria-label="Search workspace papers"
            placeholder="Search by title or filename" value={query} onChange={(event) => setQuery(event.target.value)} />
          <p className="muted">Use one paper as context for your next message. Your current chat stays open.</p>
          {loading ? <Loading label="Loading workspace papers…" /> : error ? (
            <>
              <ErrorNotice error={error} />
              <button type="button" className="button secondary small" onClick={() => setAttempt((value) => value + 1)}>Retry loading papers</button>
            </>
          ) : (
            <ul className="composer-paper-list">
              {filtered.map((paper) => (
                <li key={paper.id}>
                  <button type="button" disabled={paper.status !== 'ready'} aria-pressed={selectedId === paper.id}
                    onClick={() => { close(); onSelect({ document_id: paper.id, title: paper.title }); }}>
                    <span>{paper.title}</span>
                    <small>{paper.status === 'ready' ? paper.source_filename : 'Prepare this paper in Papers before adding it.'}</small>
                  </button>
                </li>
              ))}
              {!filtered.length ? <li className="muted">{papers.length ? 'No papers match your search.' : 'No workspace papers yet. Add a paper in Papers first.'}</li> : null}
            </ul>
          )}
        </div>
      ) : null}
    </div>
  );
}
