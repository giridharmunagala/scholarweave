import { createContext, useCallback, useContext, type ReactNode } from 'react';
import { useNavigate } from './router';

/*
 * AI is a tool you summon, not a place you go. Any surface can hand the assistant
 * a draft (a quote, a paper, a note) and the shell decides how to present it.
 */
/** The paper open in the reader. Only ids travel; the server builds the model's context. */
export interface AskContext {
  document_id: string;
  title: string;
  page?: number | null;
  selection?: string | null;
}

export interface AskApi {
  /** `send` submits the draft immediately instead of leaving it in the composer. */
  ask: (draft?: string, options?: AskOptions) => void;
  close: () => void;
  open: boolean;
}

export interface AskOptions {
  send?: boolean;
  context?: AskContext;
  intent?: 'chat' | 'ideation';
}

/** A draft-only handoff: evidence can be included in the question, without creating a review. */
export function exploreIdeasHref(question?: string, context?: AskContext): string {
  const params = new URLSearchParams({ intent: 'ideation' });
  if (question) params.set('research', question.trim().slice(0, 6000));
  if (context) params.set('reading', JSON.stringify({ ...context, selection: context.selection?.slice(0, 3000) }));
  return `/chat?${params}`;
}

/** Opens setup only. Supplied observations are claims to check, not verified novelty. */
export function checkIdeaAgainstLiteratureHref(idea: string, objective = '', context = ''): string {
  const question = [
    `Check this idea against the literature:\n${idea.trim().slice(0, 2400)}`,
    objective.trim() && `Research question:\n${objective.trim().slice(0, 1600)}`,
    context.trim() && `Supplied context (coverage and claims need checking):\n${context.trim().slice(0, 1800)}`,
    'Assess supporting and conflicting evidence, prior work, and unknowns. Novelty is unverified.',
  ].filter(Boolean).join('\n\n');
  return `/deep-work?${new URLSearchParams({ view: 'reviews', new: '1', objective: question })}`;
}

const AskContext = createContext<AskApi | null>(null);

export function AskContextProvider({ value, children }: { value: AskApi; children: ReactNode }) {
  return <AskContext.Provider value={value}>{children}</AskContext.Provider>;
}

/** Outside the shell (isolated tests, embedded pages) the full chat page is the fallback. */
export function useAsk(): AskApi {
  const context = useContext(AskContext);
  const navigate = useNavigate();
  const ask = useCallback((draft?: string, options?: AskOptions) => {
    const params = new URLSearchParams();
    if (draft) params.set('research', draft);
    if (options?.context) params.set('reading', JSON.stringify(options.context));
    if (options?.intent) params.set('intent', options.intent);
    navigate(params.size ? `/chat?${params}` : '/chat');
  }, [navigate]);
  return context ?? { ask, close: () => undefined, open: false };
}
