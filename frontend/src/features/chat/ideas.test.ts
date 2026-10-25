import { describe, expect, it } from 'vitest';
import { ideaActionDraft, recalledIdea } from './ideas';

describe('conceptual idea actions', () => {
  it('preserves the selected direction in ordinary conversation messages', () => {
    const idea = 'A premise\n\nWith a qualification';
    const draft = ideaActionDraft('Combine', idea, 'A second direction');
    expect(draft).toContain('Other idea to combine:\nA second direction');
    expect(draft).toContain('Novelty is unverified');
    expect(recalledIdea([{ role: 'user', text: draft }])).toBe(idea);
    expect(recalledIdea([{ role: 'assistant', text: draft }])).toBe('');
  });
});
