import { useEffect, useRef, useState } from 'react';
import { useAsk } from '../../app/ask';
import { Icon } from '../../shared/components/Icons';

type Paper = { id: string; title: string };

/** A quiet bubble over the reader: ask about what you are reading without leaving the page. */
export function ReaderAsk({ paper, page, folder }: {
  paper: Paper;
  page: number | null;
  folder: { id: string; name: string } | null;
}) {
  const ask = useAsk();
  const [open, setOpen] = useState(false);
  const [question, setQuestion] = useState('');
  const [selection, setSelection] = useState('');
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => { if (open) inputRef.current?.focus(); }, [open]);

  const expand = () => {
    setSelection((window.getSelection()?.toString() ?? '').trim().slice(0, 2000));
    setOpen(true);
  };
  const submit = () => {
    if (!question.trim()) return;
    ask.ask(question.trim(), { send: true, context: { document_id: paper.id, title: paper.title, page, selection: selection || null } });
    setQuestion('');
    setOpen(false);
  };

  if (!open) {
    return (
      <button
        type="button"
        className="reader-ask-bubble"
        title="Ask about this paper"
        aria-label="Ask about this paper"
        onMouseDown={(event) => event.preventDefault()}
        onClick={expand}
      >
        <Icon name="sparkle" size={16} />
      </button>
    );
  }
  return (
    <form
      className="reader-ask-card"
      aria-label="Ask about this paper"
      onSubmit={(event) => { event.preventDefault(); submit(); }}
      onKeyDown={(event) => {
        if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); setOpen(false); }
      }}
    >
      {selection ? <blockquote title={selection}>{selection}</blockquote> : null}
      <textarea
        ref={inputRef}
        aria-label="Question about this paper"
        rows={2}
        placeholder={`Ask about this paper${folder ? ` or "${folder.name}"` : ''}…`}
        value={question}
        onChange={(event) => setQuestion(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); submit(); }
        }}
      />
      <div className="reader-ask-actions">
        <small className="muted">Uses this paper{folder ? `, then ${folder.name}` : ''}, then your library</small>
        <button type="button" className="button ghost small" onClick={() => setOpen(false)}>Cancel</button>
        <button type="submit" className="button small" disabled={!question.trim()}>Ask</button>
      </div>
    </form>
  );
}
