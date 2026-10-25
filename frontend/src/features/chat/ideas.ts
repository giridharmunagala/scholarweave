export const IDEA_ACTIONS = ['Develop', 'Challenge', 'Combine', 'Simplify'] as const;
export type IdeaAction = typeof IDEA_ACTIONS[number];

export function ideaActionDraft(action: IdeaAction, idea: string, otherIdea = ''): string {
  return [
    `${action} this selected idea:\n${idea.trim().slice(0, 2400)}`,
    action === 'Combine' && `Other idea to combine:\n${otherIdea.trim().slice(0, 2400)}`,
    'Keep premise, supporting observations (with source coverage), speculation, and unknowns distinct. Novelty is unverified. Use the context already discussed; conceptual work only. Do not run experiments, acquire papers, or save artifacts.',
  ].filter(Boolean).join('\n\n');
}

export function recalledIdea(items: { role?: string | null; text?: string | null }[]): string {
  for (const item of [...items].reverse()) {
    if (item.role !== 'user') continue;
    const match = /^(?:Develop|Challenge|Combine|Simplify) this selected idea:\n([\s\S]*?)(?:\n\n(?:Other idea to combine:|Keep premise,)|$)/.exec(item.text ?? '');
    if (match) return match[1].slice(0, 2400);
  }
  return '';
}

export function readIdeaDraft(conversationId: string): string | null {
  try { return window.localStorage.getItem(`scholarweave.idea.${conversationId}`); }
  catch { return null; }
}

export function saveIdeaDraft(conversationId: string, idea: string) {
  try { window.localStorage.setItem(`scholarweave.idea.${conversationId}`, idea); }
  catch { /* Sending an action still persists the selection in conversation history. */ }
}
