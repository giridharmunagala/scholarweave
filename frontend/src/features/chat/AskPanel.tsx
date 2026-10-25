import { useState } from 'react';
import { Link } from '../../app/router';
import { type AskOptions } from '../../app/ask';
import { Icon } from '../../shared/components/Icons';
import { ResearchChatPage } from './ChatPage';
import './chat.css';

/* The assistant, borrowed for a moment: the same chat, sized to sit beside your reading. */
export default function AskPanel({ draft, autoSend, context, intent, onClose }: AskOptions & { draft?: string; autoSend?: boolean; onClose: () => void }) {
  const [fullChatHref, setFullChatHref] = useState<string | null>(null);
  return (
    <div className="ask-panel-inner">
      <header className="ask-panel-head">
        <Icon name="sparkle" size={15} />
        <strong>{intent === 'ideation' ? 'Explore ideas' : 'Assistant'}</strong>
        <span className="ask-panel-hint">Esc returns to your work</span>
        <span className="row-spacer" />
        {fullChatHref ? <Link className="icon-button" to={fullChatHref} title="Continue in full chat" aria-label="Open full chat history" onClick={onClose}>
          <Icon name="expand" size={14} />
        </Link> : null}
        <button type="button" className="icon-button" aria-label="Close assistant" title="Close (Esc)" onClick={onClose}>
          <Icon name="close" size={14} />
        </button>
      </header>
      <ResearchChatPage embedded draft={draft} autoSend={autoSend} readingContext={context} intent={intent} onHandoffChange={setFullChatHref} />
    </div>
  );
}
