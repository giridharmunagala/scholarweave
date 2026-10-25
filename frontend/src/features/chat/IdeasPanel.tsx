import { ResearchChatPage } from './ChatPage';
import './chat.css';

export interface IdeasPanelProps {
  objective: string;
  /** Human-readable evidence with its source and coverage; never opaque paper IDs. */
  context?: string;
  trackId?: string;
}

/** The existing chat engine, with conceptual intent and an editable, unsent question. */
export function IdeasPanel({ objective, context, trackId }: IdeasPanelProps) {
  const question = objective.trim().slice(0, 2400);
  const evidence = context?.trim().slice(0, 3000) ?? '';
  const draft = [
    question,
    evidence && `Supplied evidence context (not independently verified):\n${evidence}`,
  ].filter(Boolean).join('\n\n');
  return <section className="ideas-panel" aria-label="Ideas workspace">
    <header>
      <h2>Explore ideas</h2>
      <p>Start with a question, compare directions, and select an idea to develop.
        No literature search or downloads start until you ask.</p>
    </header>
    <ResearchChatPage key={`${trackId ?? ''}:${draft}`} embedded draft={draft} intent="ideation" initialTrack={trackId}
      ideaObjective={question} ideaContext={evidence} />
  </section>;
}
