import { apiUrl, json, request } from '../../api/client';
import type { components } from '../../api/schema.generated';

export type Review = components['schemas']['ReviewResponse'];
export type ReviewCreate = components['schemas']['ReviewCreateRequest'];
export type ReviewControl = components['schemas']['ReviewControlRequest'];
export type CandidatePage = components['schemas']['CandidatePage'];
export type Candidate = components['schemas']['Candidate'];
export type PaperReview = components['schemas']['PaperReview'];
export type ReviewReference = components['schemas']['ReviewReference'];
export type LocalPaper = components['schemas']['DocumentSearchResponse'];
export type CandidateSort = 'relevance' | 'year' | 'title';
export const REVIEW_STAGES = ['scope', 'discovery', 'screening', 'reading_plan', 'reading', 'references', 'reference_screening', 'reference_collection', 'synthesis', 'publication'] as const;

const path = (id: string) => `/literature-reviews/${encodeURIComponent(id)}`;
export const reviewsApi = {
  get: (id: string) => request<Review>(path(id)),
  list: (conversationId?: string, trackId?: string) => {
    const query = new URLSearchParams();
    if (trackId) query.set('track_id', trackId);
    else if (conversationId) query.set('conversation_id', conversationId);
    return request<Review[]>(`/literature-reviews?${query}`);
  },
  create: (payload: ReviewCreate) => request<Review>('/literature-reviews', json('POST', payload)),
  control: (id: string, payload: ReviewControl) => request<Review>(`${path(id)}/control`, json('POST', payload)),
  clear: (id: string, revision: number) => request<components['schemas']['ReviewClearResponse']>(
    `${path(id)}/clear`, json('POST', { expected_revision: revision }),
  ),
  candidates: (id: string, offset: number, sort: CandidateSort) =>
    request<CandidatePage>(`${path(id)}/candidates?limit=25&offset=${offset}&sort=${sort}`),
  allCandidates: async (id: string, sort: CandidateSort): Promise<CandidatePage> => {
    const items: Candidate[] = [];
    let offset = 0;
    while (true) {
      const page = await request<CandidatePage>(`${path(id)}/candidates?limit=100&offset=${offset}&sort=${sort}`);
      items.push(...page.items);
      if (page.next_offset == null) return { ...page, items, next_offset: null };
      if (page.next_offset <= offset) throw new Error('Candidate pagination did not advance. Reload the review.');
      offset = page.next_offset;
    }
  },
  paper: (id: string, candidateId: string) =>
    request<PaperReview>(`${path(id)}/papers/${encodeURIComponent(candidateId)}`),
  papers: (id: string) => request<PaperReview[]>(`${path(id)}/papers`),
  references: (id: string) => request<ReviewReference[]>(`${path(id)}/references`),
  referencesExportUrl: (id: string) => apiUrl(`${path(id)}/references.csv`),
  localPapers: (query: string) => request<LocalPaper[]>(`/documents/search?query=${encodeURIComponent(query)}&limit=25`),
  exportUrl: (id: string) => apiUrl(`${path(id)}/export`),
};
