import { describe, expect, it } from 'vitest';
import { checkIdeaAgainstLiteratureHref, exploreIdeasHref } from './ask';

describe('exploreIdeasHref', () => {
  it('works independently of a review or paper', () => {
    expect(exploreIdeasHref()).toBe('/chat?intent=ideation');
  });

  describe('literature check handoff', () => {
    it('opens a new editable review using bounded readable text, not a JSON payload', () => {
      const url = new URL(checkIdeaAgainstLiteratureHref('An idea', 'Why?', 'Paper A: abstract only'), 'http://localhost');
      expect(url.pathname).toBe('/deep-work');
      expect(url.searchParams.get('view')).toBe('reviews');
      expect(url.searchParams.get('new')).toBe('1');
      expect(url.searchParams.get('objective')).toContain('An idea');
      expect(url.searchParams.get('objective')).toContain('Paper A: abstract only');
      expect(url.searchParams.has('send')).toBe(false);
      expect(url.searchParams.has('question')).toBe(false);
      const bounded = new URL(checkIdeaAgainstLiteratureHref('x'.repeat(10000), 'y'.repeat(10000), 'z'.repeat(10000)), 'http://localhost');
      expect(bounded.searchParams.get('objective')!.length).toBeLessThan(6200);
    });
  });

  it('carries a question, evidence and optional reader context without auto-sending', () => {
    const context = { document_id: 'paper-1', title: 'A paper', page: 3, selection: 'An observation' };
    const url = new URL(exploreIdeasHref('Why? Evidence: A & B', context), 'http://localhost');
    expect(url.searchParams.get('research')).toBe('Why? Evidence: A & B');
    expect(JSON.parse(url.searchParams.get('reading')!)).toEqual(context);
    expect(url.searchParams.get('intent')).toBe('ideation');
    expect(url.searchParams.has('send')).toBe(false);
  });
});
